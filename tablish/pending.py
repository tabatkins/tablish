"""Persistence and validation for interrupted-release resume state."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import git, t
from .config import ReleaseError

PENDING_RELEASE_FILENAME: t.Final[str] = "tablish-resume.json"


@dataclass(frozen=True, slots=True)
class PendingRelease:
    """State saved once a PyPI upload partially or fully succeeds until git steps finish."""

    previousVersion: str
    newVersion: str
    targetRepo: str
    commitFiles: tuple[str, ...]
    commitMessage: str
    tagName: str | None
    tagMessage: str | None
    stage: t.ResumeStage = "git"
    repositoryUrl: str | None = None
    uploadedFiles: tuple[str, ...] = ()
    remainingFiles: tuple[str, ...] = ()

    def toJsonDict(self) -> dict[str, object]:
        """Serialize this pending release into a JSON-compatible dictionary."""
        return {
            "stage": self.stage,
            "previousVersion": self.previousVersion,
            "newVersion": self.newVersion,
            "targetRepo": self.targetRepo,
            "repositoryUrl": self.repositoryUrl,
            "commitFiles": list(self.commitFiles),
            "commitMessage": self.commitMessage,
            "tagName": self.tagName,
            "tagMessage": self.tagMessage,
            "uploadedFiles": list(self.uploadedFiles),
            "remainingFiles": list(self.remainingFiles),
        }


def _fileCountPhrase(count: int) -> str:
    """Format a file count with singular/plural noun."""
    return f"{count} {'file' if count == 1 else 'files'}"


def describePendingBlock(pending: PendingRelease) -> str:
    """Format the error message blocking a new release while one is pending."""
    if pending.stage == "upload":
        return (
            f"A previous release ({pending.newVersion}) was partially uploaded to {pending.targetRepo} "
            + f"({_fileCountPhrase(len(pending.remainingFiles))} remaining).\n"
            + "Run `tablish resume` to finish uploading and committing/tagging/pushing "
            + "before starting a new release."
        )
    return (
        f"A previous release ({pending.newVersion}) was already uploaded to {pending.targetRepo}, "
        + "but its git steps have not been completed.\n"
        + "Run `tablish resume` to finish committing/tagging/pushing before starting a new release."
    )


def describePendingSummary(pending: PendingRelease) -> str:
    """Format a compact one-line status summary for `tablish info`."""
    if pending.stage == "upload":
        return (
            f"{pending.newVersion} partially uploaded to {pending.targetRepo} "
            + f"({_fileCountPhrase(len(pending.remainingFiles))} remaining; run `tablish resume`)"
        )
    return f"{pending.newVersion} uploaded to {pending.targetRepo} (run `tablish resume`)"


def _requireNonEmptyStr(data: t.Mapping[str, t.Any], field: str, path: Path) -> str:
    """Validate that a required pending-state JSON field is a non-empty string."""
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        msg = f"Invalid pending release state in {path}: '{field}' must be a non-empty string."
        raise ReleaseError(msg)
    return value


def _requireOptionalNonEmptyStr(data: t.Mapping[str, t.Any], field: str, path: Path) -> str | None:
    """Validate that an optional pending-state JSON field is None or a non-empty string."""
    value = data.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        msg = f"Invalid pending release state in {path}: '{field}' must be null or a non-empty string."
        raise ReleaseError(msg)
    return value


def _requireStrList(
    data: t.Mapping[str, t.Any],
    field: str,
    path: Path,
    *,
    allowEmpty: bool = True,
    default: list[str] | None = None,
) -> tuple[str, ...]:
    """Validate that a pending-state JSON field is a list of non-empty strings."""
    raw = data.get(field, default)
    if not isinstance(raw, list) or not all(isinstance(item, str) and item.strip() for item in raw):
        msg = f"Invalid pending release state in {path}: '{field}' must be a list of non-empty strings."
        raise ReleaseError(msg)
    if not allowEmpty and not raw:
        msg = f"Invalid pending release state in {path}: '{field}' must not be empty."
        raise ReleaseError(msg)
    return tuple(raw)


def savePendingRelease(projectRoot: Path, pending: PendingRelease) -> None:
    """Persist pending upload/git release state into the repository's git directory."""
    path = git.getGitPath(projectRoot, PENDING_RELEASE_FILENAME)
    if path is None:
        return
    path.write_text(json.dumps(pending.toJsonDict(), indent=2) + "\n", encoding="utf-8")


def clearPendingRelease(projectRoot: Path) -> None:
    """Remove any saved pending release state file."""
    path = git.getGitPath(projectRoot, PENDING_RELEASE_FILENAME)
    if path is not None:
        path.unlink(missing_ok=True)


def loadPendingRelease(projectRoot: Path) -> PendingRelease | None:
    """Load pending upload/git release state from the repository's git directory, if present."""
    path = git.getGitPath(projectRoot, PENDING_RELEASE_FILENAME)
    if path is None or not path.is_file():
        return None
    try:
        rawData = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"Invalid pending release state in {path}: {exc}"
        raise ReleaseError(msg) from exc
    if not isinstance(rawData, dict):
        msg = f"Invalid pending release state in {path}: expected a JSON object."
        raise ReleaseError(msg)

    rawStage = rawData.get("stage", "git")
    if rawStage == "upload":
        stage: t.ResumeStage = "upload"
    elif rawStage == "git":
        stage = "git"
    else:
        msg = f"Invalid pending release state in {path}: 'stage' must be 'upload' or 'git'."
        raise ReleaseError(msg)

    previousVersion = rawData.get("previousVersion")
    if not isinstance(previousVersion, str):
        msg = f"Invalid pending release state in {path}: 'previousVersion' must be a string."
        raise ReleaseError(msg)

    newVersion = _requireNonEmptyStr(rawData, "newVersion", path)
    targetRepo = _requireNonEmptyStr(rawData, "targetRepo", path)
    repositoryUrl = _requireOptionalNonEmptyStr(rawData, "repositoryUrl", path)
    commitFiles = _requireStrList(rawData, "commitFiles", path, allowEmpty=False)
    commitMessage = _requireNonEmptyStr(rawData, "commitMessage", path)
    tagName = _requireOptionalNonEmptyStr(rawData, "tagName", path)

    tagMessage = rawData.get("tagMessage")
    if tagMessage is not None and not isinstance(tagMessage, str):
        msg = f"Invalid pending release state in {path}: 'tagMessage' must be a string or null."
        raise ReleaseError(msg)
    if tagName is not None and tagMessage is None:
        msg = f"Invalid pending release state in {path}: 'tagMessage' is required when 'tagName' is set."
        raise ReleaseError(msg)

    uploadedFiles = _requireStrList(rawData, "uploadedFiles", path, allowEmpty=True, default=[])
    remainingFiles = _requireStrList(
        rawData,
        "remainingFiles",
        path,
        allowEmpty=(stage != "upload"),
        default=[],
    )

    return PendingRelease(
        previousVersion=previousVersion,
        newVersion=newVersion,
        targetRepo=targetRepo,
        commitFiles=commitFiles,
        commitMessage=commitMessage,
        tagName=tagName,
        tagMessage=tagMessage,
        stage=stage,
        repositoryUrl=repositoryUrl,
        uploadedFiles=uploadedFiles,
        remainingFiles=remainingFiles,
    )
