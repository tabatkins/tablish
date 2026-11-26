"""Git repository inspection and secrets-protection helpers for tablish."""

from __future__ import annotations

import os
import subprocess
import urllib.parse
from pathlib import Path

from . import t
from .config import ReleaseError


def _runGit(projectRoot: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    """Run a read-only git command inside projectRoot and capture its output."""
    return subprocess.run(
        ["git", *args],
        cwd=projectRoot,
        capture_output=True,
        text=True,
        check=check,
    )


def _extractRemoteUrls(remotesOutput: str) -> list[str]:
    """Parse remote URLs from `git remote -v` output."""
    urls: list[str] = []
    for line in remotesOutput.splitlines():
        if "\t" not in line:
            continue
        _, rest = line.split("\t", 1)
        # Strip the trailing `(fetch)` / `(push)` suffix while preserving spaces in local paths.
        url = rest.rsplit(" ", 1)[0].strip()
        if url:
            urls.append(url)
    return urls


def _extractRemoteRepoName(remoteUrl: str) -> str | None:
    """Extract the repository name (without trailing .git) from a git remote URL or path."""
    cleaned = remoteUrl.strip()
    if not cleaned:
        return None
    if "://" in cleaned:
        pathPart = urllib.parse.urlparse(cleaned).path
    else:
        normalized = cleaned.replace("\\", "/")
        # Git treats `host:path` as SCP syntax only when there is no `/` before the first `:` (excluding Windows drives).
        hostPrefix, sep, scpPath = normalized.partition(":")
        isWindowsDrive = len(hostPrefix) == 1 and hostPrefix.isalpha()
        if sep and "/" not in hostPrefix and not isWindowsDrive:
            pathPart = scpPath
        else:
            pathPart = normalized

    segments = [seg for seg in pathPart.split("/") if seg]
    if not segments:
        return None
    if segments[-1] == ".git":
        segments.pop()
        if not segments:
            return None
    repoName = segments[-1].removesuffix(".git")
    return repoName or None


def inProjectRoot(projectRoot: Path, projectName: str | None) -> bool:
    """Check whether projectRoot is the root of the expected git repository."""
    try:
        topLevel = _runGit(projectRoot, "rev-parse", "--show-toplevel", check=True).stdout.strip()
        if not topLevel or Path(topLevel).resolve() != projectRoot.resolve():
            return False
        if projectName is None:
            return True
        remotes = _runGit(projectRoot, "remote", "-v", check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return False
    remoteUrls = _extractRemoteUrls(remotes)
    if remoteUrls:
        return any(_extractRemoteRepoName(url) == projectName for url in remoteUrls)
    return projectRoot.resolve().name == projectName


def ensureInProjectRoot(projectRoot: Path, projectName: str | None) -> None:
    """Raise ReleaseError if projectRoot is not the expected git repository root."""
    if inProjectRoot(projectRoot, projectName):
        return
    if projectName:
        msg = f"Run this command from inside the {projectName} project root folder."
    else:
        msg = f"Run this command from inside the git project root folder ({projectRoot})."
    raise ReleaseError(msg)


def getDirtyFiles(projectRoot: Path, paths: t.Sequence[str] = ()) -> list[str]:
    """Return git status --porcelain lines for any uncommitted or untracked changes."""
    args: list[str] = ["status", "--porcelain"]
    if paths:
        args.extend(["--", *paths])
    result = _runGit(projectRoot, *args, check=True)
    return [line for line in result.stdout.splitlines() if line.strip()]


def gitTagExists(projectRoot: Path, tagName: str) -> bool:
    """Return True if a local git tag already exists."""
    result = _runGit(projectRoot, "rev-parse", "--verify", "--quiet", f"refs/tags/{tagName}")
    return result.returncode == 0


def gitCommitHash(projectRoot: Path, rev: str) -> str | None:
    """Resolve a git revision to its full commit SHA, or None if unavailable."""
    result = _runGit(projectRoot, "rev-parse", "--verify", "--quiet", rev)
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha if sha else None


def getGitPath(projectRoot: Path, filename: str) -> Path | None:
    """Return the path to filename inside the repository's git directory, or None."""
    try:
        result = _runGit(projectRoot, "rev-parse", "--git-path", filename, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    rawPath = result.stdout.strip()
    if not rawPath:
        return None
    path = Path(rawPath)
    return path if path.is_absolute() else (projectRoot / path)


def normalizeInRepoPath(projectRoot: Path, path: Path) -> Path | None:
    """Return path relative to projectRoot if it lies inside the repository, else None."""
    resolvedRoot = projectRoot.resolve()
    fullPath = path if path.is_absolute() else (resolvedRoot / path)
    normalized = Path(os.path.normpath(fullPath))
    # Resolve only the parent so an in-repo symlink pointing outside the repo is still treated as in-repo.
    canonical = normalized.parent.resolve() / normalized.name
    if canonical == resolvedRoot or not canonical.is_relative_to(resolvedRoot):
        return None
    return canonical.relative_to(resolvedRoot)


def isGitTracked(projectRoot: Path, relPath: Path) -> bool:
    """Return True if relPath is currently tracked in the git index."""
    result = _runGit(projectRoot, "ls-files", "--error-unmatch", "--", relPath.as_posix())
    return result.returncode == 0


def isGitIgnored(projectRoot: Path, path: Path) -> bool:
    """Return True if path inside projectRoot is untracked and ignored by git."""
    relPath = normalizeInRepoPath(projectRoot, path)
    if relPath is None:
        return False
    # Without --no-index, `git check-ignore` exits non-zero for tracked files even if matched by .gitignore.
    result = _runGit(projectRoot.resolve(), "check-ignore", "-q", "--", relPath.as_posix())
    return result.returncode == 0


def ensureSecretsGitIgnored(projectRoot: Path, secretsPath: Path) -> None:
    """Ensure an existing in-repo secrets file is untracked and ignored by git."""
    if not secretsPath.is_file():
        return
    relPath = normalizeInRepoPath(projectRoot, secretsPath)
    if relPath is None:
        return
    relDisplay = relPath.as_posix()
    if isGitTracked(projectRoot, relPath):
        msg = (
            f"Refusing to run: {relDisplay} is tracked by git! "
            + f"Remove it from git (`git rm --cached {relDisplay}`), "
            + f"add '{relDisplay}' to .gitignore, and rotate any exposed tokens."
        )
        raise ReleaseError(msg)
    if not isGitIgnored(projectRoot, secretsPath):
        msg = (
            f"Refusing to run: {relDisplay} is inside the repository and is not gitignored. "
            + f"Add '{relDisplay}' to .gitignore (or move secrets-file outside the repository) and try again."
        )
        raise ReleaseError(msg)
