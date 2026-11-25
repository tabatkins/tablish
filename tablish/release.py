"""Core release automation workflow for publishing Python packages to PyPI."""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import t
from .config import (
    DEFAULT_PYPI_SECRET_KEY,
    DEFAULT_TEST_PYPI_SECRET_KEY,
    GeneratedFileConfig,
    ReleaseError,
    TablishConfig,
    loadConfig,
)


@dataclass(frozen=True, slots=True)
class ReleaseOptions:
    """Options controlling a single release invocation."""

    bump: t.BumpType | None = None
    version: t.Semver | None = None
    test: bool = False
    dryRun: bool = False
    skipLint: bool = False
    noTag: bool = False
    verbosePypi: bool = False
    info: bool = False


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


@dataclass(frozen=True, slots=True)
class ReleaseResult:
    """Summary of a completed (or dry-run) release."""

    previousVersion: str
    newVersion: str
    tagName: str | None
    targetRepo: str
    dryRun: bool
    test: bool
    builtFiles: tuple[Path, ...]


def _emit(onMessage: t.MessageCallback | None, message: str) -> None:
    """Forward a status message to the optional output callback."""
    if onMessage is not None:
        onMessage(message)


def resolveSecretsPath(projectRoot: Path, secretsFile: Path) -> Path:
    """Resolve the configured secrets file path (supporting ~ and absolute paths)."""
    expanded = secretsFile.expanduser()
    if expanded.is_absolute():
        return expanded
    return projectRoot / expanded


def _renderGeneratedFiles(
    generatedFiles: t.Sequence[GeneratedFileConfig],
    newVersion: str,
    projectRoot: Path,
) -> list[tuple[GeneratedFileConfig, Path, str]]:
    """Compute target paths and rendered contents for all configured generated files."""
    rendered: list[tuple[GeneratedFileConfig, Path, str]] = []
    for entry in generatedFiles:
        targetPath = projectRoot / entry.path
        if entry.command is not None:
            cmd = [arg.replace("{version}", newVersion) for arg in entry.command]
            try:
                result = subprocess.run(
                    cmd,
                    cwd=projectRoot,
                    capture_output=True,
                    text=True,
                    check=True,
                )
            except (OSError, subprocess.CalledProcessError) as exc:
                msg = f"Failed to generate {entry.path} via command {cmd!r}: {exc}"
                raise ReleaseError(msg) from exc
            content = result.stdout.strip()
        else:
            assert entry.template is not None
            content = entry.template.replace("{version}", newVersion)
        rendered.append((entry, targetPath, content))
    return rendered


def createRelease(
    options: ReleaseOptions,
    *,
    projectRoot: Path | None = None,
    config: TablishConfig | None = None,
    onMessage: t.MessageCallback | None = None,
) -> ReleaseResult:
    """Validate repo state, bump version files, build sdist/wheel, and publish."""
    if options.info:
        msg = "Use collectReleaseMetadata() to inspect publish metadata rather than createRelease()."
        raise ReleaseError(msg)
    if options.bump is None and options.version is None:
        msg = "Either a bump type (break/major, feature/minor, bugfix/patch) or an explicit version is required."
        raise ReleaseError(msg)
    if options.bump is not None and options.version is not None:
        msg = "Cannot specify both a bump type and an explicit version."
        raise ReleaseError(msg)

    root = (projectRoot or Path.cwd()).resolve()
    resolvedConfig = config if config is not None else loadConfig(root)

    if not inProjectRoot(root, resolvedConfig.projectName):
        if resolvedConfig.projectName:
            msg = f"Run this command from inside the {resolvedConfig.projectName} project root folder."
        else:
            msg = f"Run this command from inside the git project root folder ({root})."
        raise ReleaseError(msg)

    semverPath = root / resolvedConfig.semverFile
    secretsPath = resolveSecretsPath(root, resolvedConfig.secretsFile)

    ensureSecretsGitIgnored(root, secretsPath)

    if not semverPath.is_file():
        msg = f"Could not find {resolvedConfig.semverFile} in {root}."
        raise ReleaseError(msg)

    dirtyFiles = getDirtyFiles(root)
    if dirtyFiles:
        dirtySummary = "\n".join(f"  {line}" for line in dirtyFiles)
        if options.dryRun:
            _emit(
                onMessage,
                f"Warning: Working tree is dirty (continuing because --dry-run was passed):\n{dirtySummary}",
            )
        else:
            msg = f"Working tree is dirty. Finish committing files or stash, then try again.\n{dirtySummary}"
            raise ReleaseError(msg)

    originalSemverContent = semverPath.read_text(encoding="utf-8")
    currentVersion = originalSemverContent.strip()
    semver = parseSemver(currentVersion)

    if options.version is not None:
        major, minor, patch = options.version
        if major < 0 or minor < 0 or patch < 0:
            msg = "Invalid version: version components must be non-negative."
            raise ReleaseError(msg)
        newVersion = f"{major}.{minor}.{patch}"
        releaseKind = "explicit"
    else:
        assert options.bump is not None
        newVersion = bumpSemver(semver, options.bump)
        releaseKind = options.bump

    shouldTag = resolvedConfig.tag and not options.noTag
    tagName = resolvedConfig.tagFormat.format(version=newVersion) if shouldTag else None

    if not options.test and not options.dryRun and tagName is not None and gitTagExists(root, tagName):
        msg = f"Git tag {tagName} already exists."
        raise ReleaseError(msg)

    runLint = not options.skipLint and resolvedConfig.lintEnabled
    needCheckManifest = resolvedConfig.checkManifest

    buildCmd = findToolCommand("build", "pyproject-build")
    twineCmd = findToolCommand("twine", "twine")
    checkManifestCmd = findToolCommand("check_manifest", "check-manifest") if needCheckManifest else None

    missingTools: list[str] = []
    if buildCmd is None:
        missingTools.append("build")
    if twineCmd is None:
        missingTools.append("twine")
    if needCheckManifest and checkManifestCmd is None:
        missingTools.append("check-manifest")

    if missingTools and (not options.dryRun or buildCmd is None or (needCheckManifest and checkManifestCmd is None)):
        packages = " ".join(missingTools)
        msg = (
            f"Missing required release tool(s): {', '.join(missingTools)}\n"
            + f"Active Python: {sys.executable}\n"
            + "Install them with:\n"
            + f"  python -m pip install {packages}"
        )
        raise ReleaseError(msg)

    token, tokenFromEnv = loadPyPiToken(
        secretsPath,
        isTest=options.test,
        required=not options.dryRun,
        pypiSecretKey=resolvedConfig.pypiSecretKey,
        testPypiSecretKey=resolvedConfig.testPypiSecretKey,
        onMessage=onMessage,
    )

    if options.test:
        targetRepo = "test.pypi.org"
    elif resolvedConfig.pypiUrl:
        targetRepo = resolvedConfig.pypiUrl
    else:
        targetRepo = "pypi.org"

    modeSuffix = " (dry run)" if options.dryRun else ""
    _emit(onMessage, f"Preparing {releaseKind} release for {targetRepo}{modeSuffix}: {currentVersion} -> {newVersion}")

    if runLint:
        if resolvedConfig.lintCommand is not None:
            lintCmd: list[str] = list(resolvedConfig.lintCommand)
            lintLabel = " ".join(lintCmd)
        else:
            lintScript = root / "lint"
            if not lintScript.is_file():
                msg = "Could not find ./lint script. Pass --skip-lint to bypass or configure lint-command."
                raise ReleaseError(msg)
            lintCmd = ["sh", str(lintScript)]
            lintLabel = "./lint"
        runCommand(lintCmd, f"Running {lintLabel}", cwd=root, onMessage=onMessage)
        if not options.dryRun:
            postLintDirty = getDirtyFiles(root)
            if postLintDirty:
                dirtySummary = "\n".join(f"  {line}" for line in postLintDirty)
                msg = f"{lintLabel} modified the working tree. Commit those changes before releasing:\n{dirtySummary}"
                raise ReleaseError(msg)
    elif needCheckManifest:
        assert checkManifestCmd is not None
        runCommand(
            [*checkManifestCmd, *resolvedConfig.checkManifestArgs],
            "Checking MANIFEST.in",
            cwd=root,
            onMessage=onMessage,
        )

    renderedGenerated = _renderGeneratedFiles(resolvedConfig.generatedFiles, newVersion, root)
    originalGeneratedContents: list[tuple[Path, str | None]] = [
        (targetPath, targetPath.read_text(encoding="utf-8") if targetPath.is_file() else None)
        for _, targetPath, _ in renderedGenerated
    ]

    _emit(onMessage, f"Bumping {resolvedConfig.semverFile} to {newVersion}...")
    for _, targetPath, content in renderedGenerated:
        targetPath.parent.mkdir(parents=True, exist_ok=True)
        targetPath.write_text(content, encoding="utf-8")
    semverPath.write_text(newVersion, encoding="utf-8")

    commitFiles = [str(resolvedConfig.semverFile), *(str(entry.path) for entry, _, _ in renderedGenerated)]
    commitMsg = resolvedConfig.commitMessage.format(version=newVersion)
    distFiles: list[Path] = []
    keepVersionBump = False
    try:
        cleanBuildArtifacts(root, resolvedConfig.buildDirs)
        assert buildCmd is not None
        runCommand(buildCmd, "Building sdist and wheel", cwd=root, onMessage=onMessage)

        distDir = root / "dist"
        distFiles = sorted(distDir.iterdir()) if distDir.is_dir() else []
        if not distFiles:
            msg = "Build succeeded, but no distribution files were created in dist/."
            raise ReleaseError(msg)

        if twineCmd is not None:
            runCommand(
                [*twineCmd, "check", *(str(path) for path in distFiles)],
                "Checking built distributions with twine",
                cwd=root,
                onMessage=onMessage,
            )
        else:
            _emit(onMessage, "Warning: twine is not installed; skipping `twine check`.")

        if options.dryRun:
            _emit(onMessage, "\nDry run succeeded. Built artifacts:")
            for path in distFiles:
                _emit(onMessage, f"  - {path.relative_to(root)}")
            for entry, _, content in renderedGenerated:
                _emit(onMessage, f"Would write {entry.path}: {content}")
            _emit(onMessage, f"Would upload {newVersion} to {targetRepo}.")
            if options.test:
                _emit(onMessage, "Would roll back version files and skip git commit/tag/push (--test).")
            else:
                fileList = ", ".join(commitFiles)
                _emit(onMessage, f"Would commit {fileList} ({commitMsg!r}) and push.")
                if tagName is not None:
                    _emit(onMessage, f"Would create and push annotated tag {tagName}.")
            return ReleaseResult(
                previousVersion=currentVersion,
                newVersion=newVersion,
                tagName=tagName,
                targetRepo=targetRepo,
                dryRun=True,
                test=options.test,
                builtFiles=tuple(path.relative_to(root) for path in distFiles),
            )

        assert twineCmd is not None
        assert token is not None
        uploadCmd = [*twineCmd, "upload"]
        if options.verbosePypi:
            uploadCmd.append("--verbose")
        if options.test:
            uploadCmd.extend(["--repository-url", resolvedConfig.testPypiUrl])
        elif resolvedConfig.pypiUrl is not None:
            uploadCmd.extend(["--repository-url", resolvedConfig.pypiUrl])
        uploadCmd.extend(str(path) for path in distFiles)

        uploadEnv = os.environ.copy()
        if tokenFromEnv:
            uploadEnv.setdefault("TWINE_USERNAME", "__token__")
        else:
            uploadEnv["TWINE_USERNAME"] = "__token__"
        uploadEnv["TWINE_PASSWORD"] = token

        try:
            runCommand(
                uploadCmd,
                f"Uploading {newVersion} to {targetRepo}",
                cwd=root,
                env=uploadEnv,
                onMessage=onMessage,
            )
        except subprocess.CalledProcessError:
            if not options.verbosePypi:
                _emit(onMessage, "Hint: Re-run with --verbose-pypi for full PyPI upload error details.")
            raise

        if options.test:
            _emit(
                onMessage,
                f"Uploaded {newVersion} to {targetRepo}. Rolling back version files and skipping git commit/push.",
            )
            return ReleaseResult(
                previousVersion=currentVersion,
                newVersion=newVersion,
                tagName=None,
                targetRepo=targetRepo,
                dryRun=False,
                test=True,
                builtFiles=tuple(path.relative_to(root) for path in distFiles),
            )

        keepVersionBump = True
    finally:
        cleanBuildArtifacts(root, resolvedConfig.buildDirs)
        if not keepVersionBump:
            semverPath.write_text(originalSemverContent, encoding="utf-8")
            for targetPath, originalContent in originalGeneratedContents:
                if originalContent is not None:
                    targetPath.write_text(originalContent, encoding="utf-8")
                elif targetPath.exists():
                    targetPath.unlink()

    try:
        stageLabel = f"Staging {commitFiles[0]}" if len(commitFiles) == 1 else "Staging version files"
        runCommand(["git", "add", "--", *commitFiles], stageLabel, cwd=root, onMessage=onMessage)
        runCommand(
            ["git", "commit", "-m", commitMsg, "--", *commitFiles],
            "Committing version bump",
            cwd=root,
            onMessage=onMessage,
        )
        if tagName is not None:
            tagMsg = resolvedConfig.tagMessage.format(version=newVersion)
            runCommand(
                ["git", "tag", "-a", tagName, "-m", tagMsg],
                f"Tagging {tagName}",
                cwd=root,
                onMessage=onMessage,
            )
        pushCmd = ["git", "push", "--follow-tags"] if tagName is not None else ["git", "push"]
        runCommand(pushCmd, "Pushing release commit", cwd=root, onMessage=onMessage)
    except subprocess.CalledProcessError as exc:
        msg = (
            f"Version {newVersion} was ALREADY uploaded to {targetRepo}, "
            + f"but the git step failed (exit code {exc.returncode}).\n"
            + "Do NOT re-upload; finish committing/tagging/pushing manually."
        )
        raise ReleaseError(msg) from exc

    _emit(onMessage, f"Successfully released {newVersion}!")
    return ReleaseResult(
        previousVersion=currentVersion,
        newVersion=newVersion,
        tagName=tagName,
        targetRepo=targetRepo,
        dryRun=False,
        test=False,
        builtFiles=tuple(path.relative_to(root) for path in distFiles),
    )


def inProjectRoot(projectRoot: Path, projectName: str | None) -> bool:
    """Check whether projectRoot is the root of the expected git repository."""
    try:
        topLevel = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=projectRoot,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if not topLevel or Path(topLevel).resolve() != projectRoot.resolve():
            return False
        if projectName is None:
            return True
        remotes = subprocess.run(
            ["git", "remote", "-v"],
            cwd=projectRoot,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return False
    if projectName in remotes:
        return True
    return not remotes.strip() and projectRoot.resolve().name == projectName


def getDirtyFiles(projectRoot: Path) -> list[str]:
    """Return git status --porcelain lines for any uncommitted or untracked changes."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=projectRoot,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def gitTagExists(projectRoot: Path, tagName: str) -> bool:
    """Return True if a local git tag already exists."""
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{tagName}"],
        cwd=projectRoot,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _normalizeInRepoPath(projectRoot: Path, path: Path) -> Path | None:
    """Return path relative to projectRoot if it lies inside the repository, else None."""
    resolvedRoot = projectRoot.resolve()
    fullPath = path if path.is_absolute() else (resolvedRoot / path)
    normalized = Path(os.path.normpath(fullPath))
    canonical = normalized.parent.resolve() / normalized.name
    if canonical == resolvedRoot or not canonical.is_relative_to(resolvedRoot):
        return None
    return canonical.relative_to(resolvedRoot)


def _isGitTracked(projectRoot: Path, relPath: Path) -> bool:
    """Return True if relPath is currently tracked in the git index."""
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", relPath.as_posix()],
        cwd=projectRoot,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def isGitIgnored(projectRoot: Path, path: Path) -> bool:
    """Return True if path inside projectRoot is untracked and ignored by git."""
    relPath = _normalizeInRepoPath(projectRoot, path)
    if relPath is None:
        return False
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--", relPath.as_posix()],
        cwd=projectRoot.resolve(),
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def ensureSecretsGitIgnored(projectRoot: Path, secretsPath: Path) -> None:
    """Ensure an existing in-repo secrets file is untracked and ignored by git."""
    if not secretsPath.is_file():
        return
    relPath = _normalizeInRepoPath(projectRoot, secretsPath)
    if relPath is None:
        return
    relDisplay = relPath.as_posix()
    if _isGitTracked(projectRoot, relPath):
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


def parseSemver(s: str) -> t.Semver:
    """Parse a MAJOR.MINOR.PATCH version string into a 3-tuple of ints."""
    parts = s.strip().split(".")
    if len(parts) != 3:
        msg = f"Invalid semver '{s.strip()}': expected MAJOR.MINOR.PATCH."
        raise ReleaseError(msg)
    try:
        major, minor, patch = (int(part) for part in parts)
    except ValueError as exc:
        msg = f"Invalid semver '{s.strip()}': version components must be integers."
        raise ReleaseError(msg) from exc
    if major < 0 or minor < 0 or patch < 0:
        msg = f"Invalid semver '{s.strip()}': version components must be non-negative."
        raise ReleaseError(msg)
    return (major, minor, patch)


def bumpSemver(semver: t.Semver, bumpType: t.BumpType) -> str:
    """Return the bumped semantic version string."""
    major, minor, patch = semver
    if bumpType in ("break", "major"):
        return f"{major + 1}.0.0"
    if bumpType in ("feature", "minor"):
        return f"{major}.{minor + 1}.0"
    if bumpType in ("bugfix", "patch"):
        return f"{major}.{minor}.{patch + 1}"
    msg = f"Unknown bump type: {bumpType}"
    raise ReleaseError(msg)


def findToolCommand(moduleName: str, executableName: str) -> list[str] | None:
    """Resolve a Python CLI tool, preferring `sys.executable -m <module>` over PATH shims."""
    if importlib.util.find_spec(moduleName) is not None:
        return [sys.executable, "-m", moduleName]
    exePath = shutil.which(executableName)
    if exePath is None:
        return None
    try:
        result = subprocess.run(
            [exePath, "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode == 0:
        return [exePath]
    return None


def loadPyPiToken(
    secretsPath: Path,
    isTest: bool,
    required: bool,
    *,
    pypiSecretKey: str = DEFAULT_PYPI_SECRET_KEY,
    testPypiSecretKey: str = DEFAULT_TEST_PYPI_SECRET_KEY,
    onMessage: t.MessageCallback | None = None,
) -> tuple[str | None, bool]:
    """Load a PyPI API token from the secrets JSON file or TWINE_PASSWORD."""
    secretKey = testPypiSecretKey if isTest else pypiSecretKey
    if secretsPath.is_file():
        try:
            rawData = json.loads(secretsPath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            msg = f"Error loading {secretsPath.name}: {exc}"
            raise ReleaseError(msg) from exc
        if not isinstance(rawData, dict):
            msg = f"Invalid {secretsPath.name}: expected a JSON object."
            raise ReleaseError(msg)
        token = rawData.get(secretKey)
        if isinstance(token, str) and token.strip():
            return (token.strip(), False)
        if required:
            msg = (
                f"Missing or empty '{secretKey}' in {secretsPath.name}.\n"
                + "Expected format:\n"
                + f'  {{"{pypiSecretKey}": "pypi-...", "{testPypiSecretKey}": "pypi-..."}}'
            )
            raise ReleaseError(msg)
        _emit(onMessage, f"Warning: '{secretKey}' not found in {secretsPath.name} (required for actual upload).")
        return (None, False)

    envToken = os.environ.get("TWINE_PASSWORD", "").strip()
    if envToken:
        return (envToken, True)

    if required:
        msg = (
            f"Could not find {secretsPath.name} (and TWINE_PASSWORD is not set).\n"
            + f"Create {secretsPath.name} with:\n"
            + f'  {{"{secretKey}": "pypi-..."}}'
        )
        raise ReleaseError(msg)
    _emit(
        onMessage,
        f"Warning: {secretsPath.name} not found and TWINE_PASSWORD is not set (required for actual upload).",
    )
    return (None, False)


def cleanBuildArtifacts(projectRoot: Path, buildDirs: t.Sequence[str]) -> None:
    """Remove temporary build/dist directories."""
    for dirName in buildDirs:
        target = projectRoot / dirName
        if target.exists():
            shutil.rmtree(target)


def _inspectAuthToken(secretsPath: Path, secretKey: str) -> tuple[str, t.MetadataStatus]:
    """Inspect PyPI token availability without exposing the token value."""
    if secretsPath.is_file():
        try:
            rawData = json.loads(secretsPath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return (f"error reading {secretsPath.name}: {exc}", "error")
        if not isinstance(rawData, dict):
            return (f"invalid {secretsPath.name} (expected JSON object)", "error")
        token = rawData.get(secretKey)
        if isinstance(token, str) and token.strip():
            return (f"found in {secretsPath.name} ({secretKey!r})", "ok")
        return (f"missing {secretKey!r} in {secretsPath.name}", "warning")

    if os.environ.get("TWINE_PASSWORD", "").strip():
        username = os.environ.get("TWINE_USERNAME") or "__token__"
        return (f"from TWINE_PASSWORD (user: {username})", "ok")

    return (f"missing ({secretsPath.name} not found, TWINE_PASSWORD unset)", "warning")


def collectReleaseMetadata(
    options: ReleaseOptions | None = None,
    *,
    projectRoot: Path | None = None,
    config: TablishConfig | None = None,
) -> ReleaseMetadata:
    """Collect the resolved publish metadata and source provenance for a project."""
    opts = options if options is not None else ReleaseOptions(info=True)
    root = (projectRoot or Path.cwd()).resolve()
    resolvedConfig = config if config is not None else loadConfig(root)

    def _configItem(
        section: str,
        key: str,
        value: str,
        *,
        status: t.MetadataStatus | None = None,
        details: tuple[str, ...] = (),
    ) -> MetadataItem:
        return MetadataItem(
            section=section,
            key=key,
            value=value,
            source=resolvedConfig.sourceOf(key),
            sourceDetail=resolvedConfig.inferredOrigin(key),
            status=status,
            details=details,
        )

    items: list[MetadataItem] = []

    # Project section
    if projectRoot is not None:
        items.append(MetadataItem("Project", "project-root", str(root), "cli", "-C"))
    else:
        items.append(MetadataItem("Project", "project-root", str(root), "default", "cwd"))

    items.append(
        MetadataItem(
            "Project",
            "pyproject.toml",
            "[tool.tablish] present" if resolvedConfig.hasTablishTable else "no [tool.tablish] table",
            "explicit" if resolvedConfig.hasTablishTable else "default",
        ),
    )

    if resolvedConfig.projectName is not None:
        projectNameDisplay = resolvedConfig.projectName
    elif "project-name" in resolvedConfig.explicitKeys:
        projectNameDisplay = "(disabled)"
    else:
        projectNameDisplay = "(none)"
    items.append(_configItem("Project", "project-name", projectNameDisplay))
    items.append(_configItem("Project", "semver-file", resolvedConfig.semverFile.as_posix()))

    semverPath = root / resolvedConfig.semverFile
    if semverPath.is_file():
        rawVersion = semverPath.read_text(encoding="utf-8").strip()
        try:
            major, minor, patch = parseSemver(rawVersion)
            items.append(
                MetadataItem(
                    "Project",
                    "current-version",
                    f"{major}.{minor}.{patch}",
                    "derived",
                    f"from {resolvedConfig.semverFile.as_posix()}",
                ),
            )
        except ReleaseError:
            items.append(
                MetadataItem(
                    "Project",
                    "current-version",
                    f"invalid ({rawVersion!r})",
                    "derived",
                    f"from {resolvedConfig.semverFile.as_posix()}",
                    status="error",
                ),
            )
    else:
        items.append(
            MetadataItem(
                "Project",
                "current-version",
                f"missing ({resolvedConfig.semverFile.as_posix()})",
                "derived",
                status="error",
            ),
        )

    isValidRepo = inProjectRoot(root, resolvedConfig.projectName)
    if isValidRepo:
        dirtyFiles = getDirtyFiles(root)
        if dirtyFiles:
            count = len(dirtyFiles)
            noun = "file" if count == 1 else "files"
            items.append(
                MetadataItem(
                    "Project",
                    "git-repo",
                    f"dirty ({count} uncommitted/untracked {noun})",
                    "derived",
                    status="warning",
                ),
            )
        else:
            items.append(MetadataItem("Project", "git-repo", "clean project root", "derived", status="ok"))
    elif resolvedConfig.projectName:
        items.append(
            MetadataItem(
                "Project",
                "git-repo",
                f"not project root for {resolvedConfig.projectName}",
                "derived",
                status="error",
            ),
        )
    else:
        items.append(MetadataItem("Project", "git-repo", "not a git repository root", "derived", status="error"))

    # PyPI & Credentials section
    if opts.test:
        items.append(MetadataItem("PyPI & Credentials", "target-repo", "test.pypi.org", "cli", "--test"))
    elif resolvedConfig.pypiUrl is not None:
        items.append(MetadataItem("PyPI & Credentials", "target-repo", resolvedConfig.pypiUrl, "explicit"))
    else:
        items.append(MetadataItem("PyPI & Credentials", "target-repo", "pypi.org", "default"))

    pypiUrlDisplay = (
        resolvedConfig.pypiUrl
        if resolvedConfig.pypiUrl is not None
        else "https://upload.pypi.org/legacy/ (twine default)"
    )
    items.append(_configItem("PyPI & Credentials", "pypi-url", pypiUrlDisplay))
    items.append(_configItem("PyPI & Credentials", "test-pypi-url", resolvedConfig.testPypiUrl))

    secretsPath = resolveSecretsPath(root, resolvedConfig.secretsFile)
    secretsDisplayBase = str(resolvedConfig.secretsFile)
    secretsStatus: t.MetadataStatus | None = None
    if not secretsPath.is_file():
        secretsDisplay = f"{secretsDisplayBase} (not found)"
    else:
        relSecrets = _normalizeInRepoPath(root, secretsPath)
        if relSecrets is None:
            secretsDisplay = f"{secretsDisplayBase} (exists, outside repo)"
            secretsStatus = "ok"
        elif not isValidRepo:
            secretsDisplay = f"{secretsDisplayBase} (exists, in repo)"
        elif _isGitTracked(root, relSecrets):
            secretsDisplay = f"{secretsDisplayBase} (TRACKED BY GIT!)"
            secretsStatus = "error"
        elif isGitIgnored(root, secretsPath):
            secretsDisplay = f"{secretsDisplayBase} (exists, gitignored)"
            secretsStatus = "ok"
        else:
            secretsDisplay = f"{secretsDisplayBase} (NOT gitignored!)"
            secretsStatus = "error"
    items.append(_configItem("PyPI & Credentials", "secrets-file", secretsDisplay, status=secretsStatus))
    items.append(_configItem("PyPI & Credentials", "pypi-secret-key", resolvedConfig.pypiSecretKey))
    items.append(_configItem("PyPI & Credentials", "test-pypi-secret-key", resolvedConfig.testPypiSecretKey))

    activeSecretKey = resolvedConfig.testPypiSecretKey if opts.test else resolvedConfig.pypiSecretKey
    tokenSummary, tokenStatus = _inspectAuthToken(secretsPath, activeSecretKey)
    items.append(MetadataItem("PyPI & Credentials", "auth-token", tokenSummary, "derived", status=tokenStatus))

    if opts.verbosePypi:
        items.append(MetadataItem("PyPI & Credentials", "verbose-pypi", "true", "cli", "--verbose-pypi"))
    else:
        items.append(MetadataItem("PyPI & Credentials", "verbose-pypi", "false", "default"))

    # Checks & Build section
    if opts.skipLint:
        items.append(MetadataItem("Checks & Build", "lint-command", "disabled", "cli", "--skip-lint"))
    elif not resolvedConfig.lintEnabled:
        items.append(_configItem("Checks & Build", "lint-command", "disabled"))
    elif resolvedConfig.lintCommand is not None:
        items.append(_configItem("Checks & Build", "lint-command", shlex.join(resolvedConfig.lintCommand)))
    else:
        lintScriptExists = (root / "lint").is_file()
        lintDisplay = "./lint" if lintScriptExists else "./lint (not found)"
        lintStatus: t.MetadataStatus | None = None if lintScriptExists else "warning"
        items.append(_configItem("Checks & Build", "lint-command", lintDisplay, status=lintStatus))

    items.append(
        _configItem("Checks & Build", "check-manifest", "true" if resolvedConfig.checkManifest else "false"),
    )
    checkManifestArgsDisplay = (
        shlex.join(resolvedConfig.checkManifestArgs) if resolvedConfig.checkManifestArgs else "(none)"
    )
    items.append(_configItem("Checks & Build", "check-manifest-args", checkManifestArgsDisplay))
    buildDirsDisplay = ", ".join(resolvedConfig.buildDirs) if resolvedConfig.buildDirs else "(none)"
    items.append(_configItem("Checks & Build", "build-dirs", buildDirsDisplay))

    toolChecks: list[tuple[str, bool]] = [
        ("build", findToolCommand("build", "pyproject-build") is not None),
        ("twine", findToolCommand("twine", "twine") is not None),
    ]
    if resolvedConfig.checkManifest:
        toolChecks.append(("check-manifest", findToolCommand("check_manifest", "check-manifest") is not None))
    toolsSummary = ", ".join(f"{name}: {'ok' if found else 'MISSING'}" for name, found in toolChecks)
    toolsStatus: t.MetadataStatus = "ok" if all(found for _, found in toolChecks) else "error"
    items.append(MetadataItem("Checks & Build", "tools", toolsSummary, "derived", status=toolsStatus))

    # Git & Release section
    if opts.noTag:
        items.append(MetadataItem("Git & Release", "tag", "false", "cli", "--no-tag"))
    else:
        items.append(_configItem("Git & Release", "tag", "true" if resolvedConfig.tag else "false"))
    items.append(_configItem("Git & Release", "tag-format", resolvedConfig.tagFormat))
    items.append(_configItem("Git & Release", "tag-message", resolvedConfig.tagMessage))
    items.append(_configItem("Git & Release", "commit-message", resolvedConfig.commitMessage))

    if resolvedConfig.generatedFiles:
        fileCount = len(resolvedConfig.generatedFiles)
        fileNoun = "file" if fileCount == 1 else "files"
        genDetails = tuple(
            (
                f"{entry.path.as_posix()} (command: {shlex.join(entry.command)})"
                if entry.command is not None
                else f"{entry.path.as_posix()} (template: {entry.template!r})"
            )
            for entry in resolvedConfig.generatedFiles
        )
        items.append(_configItem("Git & Release", "generated-files", f"{fileCount} {fileNoun}", details=genDetails))
    else:
        items.append(_configItem("Git & Release", "generated-files", "(none)"))

    return ReleaseMetadata(projectRoot=root, config=resolvedConfig, items=tuple(items))


def runCommand(
    cmd: t.Sequence[str],
    description: str,
    *,
    cwd: Path,
    env: t.Mapping[str, str] | None = None,
    onMessage: t.MessageCallback | None = None,
) -> None:
    """Run a subprocess command with a progress header."""
    _emit(onMessage, f"==> {description}...")
    subprocess.run(cmd, cwd=cwd, check=True, env=env)
