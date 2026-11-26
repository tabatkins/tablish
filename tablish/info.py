"""Read-only publish metadata inspection for `tablish info`."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

from . import git, pending, release, t
from .config import TABLISH_KDL_FILENAME, ReleaseError, TablishConfig, loadConfig


@dataclass(frozen=True, slots=True)
class MetadataItem:
    """A single publish metadata entry with its value and provenance."""

    section: str
    key: str
    value: str
    source: t.MetadataSource
    sourceDetail: str | None = None
    status: t.MetadataStatus | None = None
    details: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReleaseMetadata:
    """Resolved publish metadata and provenance collected without mutating the repo."""

    projectRoot: Path
    config: TablishConfig
    items: tuple[MetadataItem, ...]


def _configItem(
    config: TablishConfig,
    section: str,
    key: str,
    value: str,
    *,
    status: t.MetadataStatus | None = None,
    details: tuple[str, ...] = (),
) -> MetadataItem:
    """Build a MetadataItem whose provenance comes from TablishConfig."""
    return MetadataItem(
        section=section,
        key=key,
        value=value,
        source=config.sourceOf(key),
        sourceDetail=config.originOf(key),
        status=status,
        details=details,
    )


def _describeCurrentVersion(root: Path, semverFile: Path) -> MetadataItem:
    """Inspect the current version file without raising on missing or invalid content."""
    semverDisplay = semverFile.as_posix()
    semverPath = root / semverFile
    if not semverPath.is_file():
        return MetadataItem("Project", "current-version", f"missing ({semverDisplay})", "derived", status="error")

    rawVersion = semverPath.read_text(encoding="utf-8").strip()
    try:
        major, minor, patch = release.parseSemver(rawVersion)
    except ReleaseError:
        return MetadataItem(
            "Project",
            "current-version",
            f"invalid ({rawVersion!r})",
            "derived",
            f"from {semverDisplay}",
            status="error",
        )
    return MetadataItem("Project", "current-version", f"{major}.{minor}.{patch}", "derived", f"from {semverDisplay}")


def _describeGitRepo(root: Path, projectName: str | None, *, isValidRepo: bool) -> MetadataItem:
    """Inspect git repository root validity and working tree cleanliness."""
    if isValidRepo:
        dirtyFiles = git.getDirtyFiles(root)
        if dirtyFiles:
            count = len(dirtyFiles)
            noun = "file" if count == 1 else "files"
            return MetadataItem(
                "Project",
                "git-repo",
                f"dirty ({count} uncommitted/untracked {noun})",
                "derived",
                status="warning",
            )
        return MetadataItem("Project", "git-repo", "clean project root", "derived", status="ok")

    if projectName:
        return MetadataItem("Project", "git-repo", f"not project root for {projectName}", "derived", status="error")
    return MetadataItem("Project", "git-repo", "not a git repository root", "derived", status="error")


def _describeSecretsFile(
    root: Path,
    configuredPath: Path,
    secretsPath: Path,
    *,
    isValidRepo: bool,
) -> tuple[str, t.MetadataStatus | None]:
    """Return (displayText, status) describing secrets file presence and gitignore safety."""
    displayBase = str(configuredPath)
    if not secretsPath.is_file():
        return (f"{displayBase} (not found)", None)

    relSecrets = git.normalizeInRepoPath(root, secretsPath)
    if relSecrets is None:
        return (f"{displayBase} (exists, outside repo)", "ok")
    if not isValidRepo:
        return (f"{displayBase} (exists, in repo)", None)
    if git.isGitTracked(root, relSecrets):
        return (f"{displayBase} (TRACKED BY GIT!)", "error")
    if git.isGitIgnored(root, secretsPath):
        return (f"{displayBase} (exists, gitignored)", "ok")
    return (f"{displayBase} (NOT gitignored!)", "error")


def _collectProjectMetadata(
    root: Path,
    config: TablishConfig,
    *,
    explicitRoot: bool,
) -> tuple[list[MetadataItem], bool]:
    """Collect the 'Project' metadata section and report whether root is a valid git project root."""
    items: list[MetadataItem] = []

    if explicitRoot:
        items.append(MetadataItem("Project", "project-root", str(root), "cli", "-C"))
    else:
        items.append(MetadataItem("Project", "project-root", str(root), "default", "cwd"))

    if not config.hasPyproject:
        pyprojectDisplay = "not found"
    elif config.hasTablishTable:
        pyprojectDisplay = "[tool.tablish] present"
    else:
        pyprojectDisplay = "no [tool.tablish] table"
    items.append(
        MetadataItem(
            "Project",
            "pyproject.toml",
            pyprojectDisplay,
            "explicit" if config.hasTablishTable else "default",
        ),
    )
    items.append(
        MetadataItem(
            "Project",
            TABLISH_KDL_FILENAME,
            "present" if config.hasTablishKdl else "not found",
            "explicit" if config.hasTablishKdl else "default",
        ),
    )

    if config.projectName is not None:
        projectNameDisplay = config.projectName
    elif "project-name" in config.explicitKeys:
        projectNameDisplay = "(disabled)"
    else:
        projectNameDisplay = "(none)"
    items.append(_configItem(config, "Project", "project-name", projectNameDisplay))
    items.append(_configItem(config, "Project", "semver-file", config.semverFile.as_posix()))
    items.append(_describeCurrentVersion(root, config.semverFile))

    isValidRepo = git.inProjectRoot(root, config.projectName)
    items.append(_describeGitRepo(root, config.projectName, isValidRepo=isValidRepo))
    return (items, isValidRepo)


def _collectPypiMetadata(
    root: Path,
    config: TablishConfig,
    opts: release.ReleaseOptions,
    *,
    isValidRepo: bool,
) -> list[MetadataItem]:
    """Collect the 'PyPI & Credentials' metadata section."""
    section = "PyPI & Credentials"
    items: list[MetadataItem] = []

    if opts.test:
        items.append(MetadataItem(section, "target-repo", "test.pypi.org", "cli", "--test"))
    elif config.pypiUrl is not None:
        items.append(
            MetadataItem(section, "target-repo", config.pypiUrl, "explicit", config.explicitOrigin("pypi-url")),
        )
    else:
        items.append(MetadataItem(section, "target-repo", "pypi.org", "default"))

    pypiUrlDisplay = config.pypiUrl if config.pypiUrl is not None else "https://upload.pypi.org/legacy/ (twine default)"
    items.append(_configItem(config, section, "pypi-url", pypiUrlDisplay))
    items.append(_configItem(config, section, "test-pypi-url", config.testPypiUrl))

    secretsPath = release.resolveSecretsPath(root, config.secretsFile)
    secretsDisplay, secretsStatus = _describeSecretsFile(
        root,
        config.secretsFile,
        secretsPath,
        isValidRepo=isValidRepo,
    )
    items.append(_configItem(config, section, "secrets-file", secretsDisplay, status=secretsStatus))
    items.append(_configItem(config, section, "pypi-secret-key", config.pypiSecretKey))
    items.append(_configItem(config, section, "test-pypi-secret-key", config.testPypiSecretKey))

    activeSecretKey = config.testPypiSecretKey if opts.test else config.pypiSecretKey
    tokenSummary, tokenStatus = release.inspectAuthToken(secretsPath, activeSecretKey)
    items.append(MetadataItem(section, "auth-token", tokenSummary, "derived", status=tokenStatus))

    if opts.verbosePypi:
        items.append(MetadataItem(section, "verbose-pypi", "true", "cli", "--verbose-pypi"))
    else:
        items.append(MetadataItem(section, "verbose-pypi", "false", "default"))
    return items


def _collectChecksMetadata(
    root: Path,
    config: TablishConfig,
    opts: release.ReleaseOptions,
) -> list[MetadataItem]:
    """Collect the 'Checks & Build' metadata section."""
    section = "Checks & Build"
    items: list[MetadataItem] = []

    if opts.skipLint:
        items.append(MetadataItem(section, "lint-command", "disabled", "cli", "--skip-lint"))
    elif not config.lintEnabled:
        items.append(_configItem(config, section, "lint-command", "disabled"))
    elif config.lintCommand is not None:
        items.append(_configItem(config, section, "lint-command", shlex.join(config.lintCommand)))
    else:
        lintScriptExists = (root / "lint").is_file()
        lintDisplay = "./lint" if lintScriptExists else "./lint (not found)"
        lintStatus: t.MetadataStatus | None = None if lintScriptExists else "warning"
        items.append(_configItem(config, section, "lint-command", lintDisplay, status=lintStatus))

    items.append(_configItem(config, section, "check-manifest", "true" if config.checkManifest else "false"))
    checkManifestArgsDisplay = shlex.join(config.checkManifestArgs) if config.checkManifestArgs else "(none)"
    items.append(_configItem(config, section, "check-manifest-args", checkManifestArgsDisplay))
    buildDirsDisplay = ", ".join(config.buildDirs) if config.buildDirs else "(none)"
    items.append(_configItem(config, section, "build-dirs", buildDirsDisplay))

    runLint = not opts.skipLint and config.lintEnabled
    needCheckManifest = not runLint and config.checkManifest
    toolChecks: list[tuple[str, bool]] = [
        ("build", release.findToolCommand("build", "pyproject-build") is not None),
        ("twine", release.findToolCommand("twine", "twine") is not None),
    ]
    if needCheckManifest:
        toolChecks.append(("check-manifest", release.findToolCommand("check_manifest", "check-manifest") is not None))
    toolsSummary = ", ".join(f"{name}: {'ok' if found else 'MISSING'}" for name, found in toolChecks)
    toolsStatus: t.MetadataStatus = "ok" if all(found for _, found in toolChecks) else "error"
    items.append(MetadataItem(section, "tools", toolsSummary, "derived", status=toolsStatus))
    return items


def _collectGitReleaseMetadata(
    root: Path,
    config: TablishConfig,
    opts: release.ReleaseOptions,
    *,
    isValidRepo: bool,
) -> list[MetadataItem]:
    """Collect the 'Git & Release' metadata section."""
    section = "Git & Release"
    items: list[MetadataItem] = []

    if opts.noTag:
        items.append(MetadataItem(section, "tag", "false", "cli", "--no-tag"))
    else:
        items.append(_configItem(config, section, "tag", "true" if config.tag else "false"))
    items.append(_configItem(config, section, "tag-format", config.tagFormat))
    items.append(_configItem(config, section, "tag-message", config.tagMessage))
    items.append(_configItem(config, section, "commit-message", config.commitMessage))

    if config.generatedFiles:
        fileCount = len(config.generatedFiles)
        fileNoun = "file" if fileCount == 1 else "files"
        genDetails = tuple(
            (
                f"{entry.path.as_posix()} (command: {shlex.join(entry.command)})"
                if entry.command is not None
                else f"{entry.path.as_posix()} (template: {entry.template!r})"
            )
            for entry in config.generatedFiles
        )
        items.append(_configItem(config, section, "generated-files", f"{fileCount} {fileNoun}", details=genDetails))
    else:
        items.append(_configItem(config, section, "generated-files", "(none)"))

    if isValidRepo:
        try:
            pendingResume = pending.loadPendingRelease(root)
        except ReleaseError:
            items.append(
                MetadataItem(
                    section,
                    "pending-resume",
                    f"invalid {pending.PENDING_RELEASE_FILENAME} in git dir",
                    "derived",
                    status="error",
                ),
            )
        else:
            if pendingResume is not None:
                items.append(
                    MetadataItem(
                        section,
                        "pending-resume",
                        pending.describePendingSummary(pendingResume),
                        "derived",
                        status="warning",
                    ),
                )
    return items


def collectReleaseMetadata(
    options: release.ReleaseOptions | None = None,
    *,
    projectRoot: Path | None = None,
    config: TablishConfig | None = None,
) -> ReleaseMetadata:
    """Collect the resolved publish metadata and source provenance for a project."""
    opts = options if options is not None else release.ReleaseOptions(info=True)
    root = (projectRoot or Path.cwd()).resolve()
    resolvedConfig = config if config is not None else loadConfig(root)

    projectItems, isValidRepo = _collectProjectMetadata(root, resolvedConfig, explicitRoot=projectRoot is not None)
    items: list[MetadataItem] = [
        *projectItems,
        *_collectPypiMetadata(root, resolvedConfig, opts, isValidRepo=isValidRepo),
        *_collectChecksMetadata(root, resolvedConfig, opts),
        *_collectGitReleaseMetadata(root, resolvedConfig, opts, isValidRepo=isValidRepo),
    ]
    return ReleaseMetadata(projectRoot=root, config=resolvedConfig, items=tuple(items))
