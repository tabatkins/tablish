"""Core release automation workflow for publishing Python packages to PyPI."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from . import git, pending as pendingMod, t
from .config import (
    DEFAULT_PYPI_SECRET_KEY,
    DEFAULT_TEST_PYPI_SECRET_KEY,
    GeneratedFileConfig,
    ReleaseError,
    TablishConfig,
    loadConfig,
)
from .git import ensureSecretsGitIgnored, getDirtyFiles, gitTagExists
from .pending import PendingRelease, loadPendingRelease


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
    resume: bool = False


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


@dataclass(frozen=True, slots=True)
class _VersionPlan:
    """Computed version transition and original semver file contents for rollback."""

    originalContent: str
    currentVersion: str
    newVersion: str
    releaseKind: str


@dataclass(frozen=True, slots=True)
class _ReleaseTools:
    """Resolved CLI tool commands required for a release."""

    buildCmd: list[str]
    twineCmd: list[str] | None
    checkManifestCmd: list[str] | None


# ---------------------------------------------------------------------------
# Low-level utilities (semver, credentials, tools, build cleanup, subprocess)
# ---------------------------------------------------------------------------


def _emit(onMessage: t.MessageCallback | None, message: str) -> None:
    """Forward a status message to the optional output callback."""
    if onMessage is not None:
        onMessage(message)


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


def resolveSecretsPath(projectRoot: Path, secretsFile: Path) -> Path:
    """Resolve the configured secrets file path (supporting ~ and absolute paths)."""
    expanded = secretsFile.expanduser()
    if expanded.is_absolute():
        return expanded
    return projectRoot / expanded


def _readSecretsFile(secretsPath: Path) -> dict[str, t.Any]:
    """Read and validate a JSON object from secretsPath."""
    try:
        rawData = json.loads(secretsPath.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"Error loading {secretsPath.name}: {exc}"
        raise ReleaseError(msg) from exc
    if not isinstance(rawData, dict):
        msg = f"Invalid {secretsPath.name}: expected a JSON object."
        raise ReleaseError(msg)
    return rawData


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
        rawData = _readSecretsFile(secretsPath)
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


def inspectAuthToken(secretsPath: Path, secretKey: str) -> tuple[str, t.MetadataStatus]:
    """Inspect PyPI token availability without exposing the token value."""
    if secretsPath.is_file():
        try:
            rawData = _readSecretsFile(secretsPath)
        except ReleaseError as exc:
            return (str(exc), "error")
        token = rawData.get(secretKey)
        if isinstance(token, str) and token.strip():
            return (f"found in {secretsPath.name} ({secretKey!r})", "ok")
        return (f"missing {secretKey!r} in {secretsPath.name}", "warning")

    if os.environ.get("TWINE_PASSWORD", "").strip():
        username = os.environ.get("TWINE_USERNAME") or "__token__"
        return (f"from TWINE_PASSWORD (user: {username})", "ok")

    return (f"missing ({secretsPath.name} not found, TWINE_PASSWORD unset)", "warning")


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


def _formatMissingToolsError(missingTools: t.Sequence[str]) -> str:
    """Format an actionable error message listing missing release dependencies."""
    packages = " ".join(missingTools)
    return (
        f"Missing required release tool(s): {', '.join(missingTools)}\n"
        + f"Active Python: {sys.executable}\n"
        + "Install them with:\n"
        + f"  python -m pip install {packages}"
    )


def cleanBuildArtifacts(projectRoot: Path, buildDirs: t.Sequence[str]) -> None:
    """Remove temporary build/dist directories."""
    for dirName in buildDirs:
        target = projectRoot / dirName
        if target.exists():
            shutil.rmtree(target)


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


# ---------------------------------------------------------------------------
# Twine upload & git finalization helpers
# ---------------------------------------------------------------------------


def _buildTwineUploadCmd(
    twineCmd: t.Sequence[str],
    distFiles: t.Sequence[Path],
    *,
    verbose: bool,
    repositoryUrl: str | None,
    skipExisting: bool,
) -> list[str]:
    """Construct a `twine upload` argv list for one or more distribution files."""
    cmd = [*twineCmd, "upload"]
    if verbose:
        cmd.append("--verbose")
    if skipExisting:
        cmd.append("--skip-existing")
    if repositoryUrl is not None:
        cmd.extend(["--repository-url", repositoryUrl])
    cmd.extend(str(path) for path in distFiles)
    return cmd


def _buildTwineUploadEnv(token: str, *, tokenFromEnv: bool) -> dict[str, str]:
    """Build subprocess environment variables carrying PyPI upload credentials."""
    uploadEnv = os.environ.copy()
    if tokenFromEnv:
        uploadEnv.setdefault("TWINE_USERNAME", "__token__")
    else:
        uploadEnv["TWINE_USERNAME"] = "__token__"
    uploadEnv["TWINE_PASSWORD"] = token
    return uploadEnv


def _uploadDistFiles(
    root: Path,
    basePending: PendingRelease,
    *,
    twineCmd: t.Sequence[str],
    uploadEnv: t.Mapping[str, str],
    verbose: bool,
    isResume: bool,
    onMessage: t.MessageCallback | None,
    onUploadStarted: t.Callable[[], None] | None = None,
) -> tuple[PendingRelease, list[Path]]:
    """Upload remaining dist files one by one, checkpointing resume state after each upload."""
    uploadedSoFar = list(basePending.uploadedFiles)
    remainingList = list(basePending.remainingFiles)
    uploadedNow: list[Path] = []
    verb = "Uploading remaining" if isResume else "Uploading"

    for idx, relPath in enumerate(remainingList):
        distPath = root / relPath
        uploadCmd = _buildTwineUploadCmd(
            twineCmd,
            [distPath],
            verbose=verbose,
            repositoryUrl=basePending.repositoryUrl,
            skipExisting=isResume,
        )
        if len(remainingList) > 1:
            desc = f"{verb} {basePending.newVersion} ({idx + 1}/{len(remainingList)}: {distPath.name}) to {basePending.targetRepo}"
        elif isResume:
            desc = f"{verb} {basePending.newVersion} ({distPath.name}) to {basePending.targetRepo}"
        else:
            desc = f"{verb} {basePending.newVersion} to {basePending.targetRepo}"

        try:
            runCommand(uploadCmd, desc, cwd=root, env=uploadEnv, onMessage=onMessage)
        except subprocess.CalledProcessError as exc:
            if not verbose:
                hintCmd = "`tablish resume --verbose-pypi`" if isResume else "--verbose-pypi"
                _emit(onMessage, f"Hint: Re-run {hintCmd} for full PyPI upload error details.")
            if not isResume and not uploadedSoFar:
                # Nothing reached PyPI yet, so let the caller roll back cleanly.
                raise
            stillRemaining = tuple(remainingList[idx:])
            partialPending = replace(
                basePending,
                stage="upload",
                uploadedFiles=tuple(uploadedSoFar),
                remainingFiles=stillRemaining,
            )
            pendingMod.savePendingRelease(root, partialPending)
            if isResume:
                msg = (
                    f"Uploading {relPath} for {basePending.newVersion} failed (exit code {exc.returncode}); "
                    + f"remaining: {', '.join(stillRemaining)}.\n"
                    + "Resolve the upload issue and run `tablish resume` again."
                )
            else:
                msg = (
                    f"Version {basePending.newVersion} was PARTIALLY uploaded to {basePending.targetRepo} "
                    + f"(uploaded: {', '.join(uploadedSoFar)}; remaining: {', '.join(stillRemaining)}), "
                    + f"but uploading {relPath} failed (exit code {exc.returncode}).\n"
                    + "Do NOT start a new release; resolve the upload issue and run `tablish resume` "
                    + "to upload the remaining file(s) and finish committing/tagging/pushing."
                )
            raise ReleaseError(msg) from exc

        uploadedSoFar.append(relPath)
        uploadedNow.append(Path(relPath))
        if onUploadStarted is not None:
            onUploadStarted()

        remainingAfter = tuple(remainingList[idx + 1 :])
        if remainingAfter:
            checkpoint = replace(
                basePending,
                stage="upload",
                uploadedFiles=tuple(uploadedSoFar),
                remainingFiles=remainingAfter,
            )
            pendingMod.savePendingRelease(root, checkpoint)

    gitPending = replace(
        basePending,
        stage="git",
        uploadedFiles=tuple(uploadedSoFar),
        remainingFiles=(),
    )
    pendingMod.savePendingRelease(root, gitPending)
    return (gitPending, uploadedNow)


def _needsVersionCommit(projectRoot: Path, pending: PendingRelease) -> bool:
    """Return True if any pending commit files still have uncommitted changes."""
    for relFile in pending.commitFiles:
        if not (projectRoot / relFile).is_file():
            msg = f"Expected release file {relFile} is missing. Restore it before running `tablish resume`."
            raise ReleaseError(msg)
    if getDirtyFiles(projectRoot, pending.commitFiles):
        return True

    semverPath = projectRoot / pending.commitFiles[0]
    actualVersion = semverPath.read_text(encoding="utf-8").strip()
    if actualVersion != pending.newVersion:
        msg = (
            f"Expected {pending.commitFiles[0]} to contain {pending.newVersion!r}, "
            + f"but found {actualVersion!r}. Restore the bumped version files before running `tablish resume`."
        )
        raise ReleaseError(msg)
    return False


def _needsReleaseTag(projectRoot: Path, pending: PendingRelease) -> bool:
    """Return True if the release tag still needs to be created, validating any existing tag."""
    if pending.tagName is None:
        return False
    if not gitTagExists(projectRoot, pending.tagName):
        return True
    tagCommit = git.gitCommitHash(projectRoot, f"refs/tags/{pending.tagName}^{{commit}}")
    headCommit = git.gitCommitHash(projectRoot, "HEAD")
    if tagCommit is not None and tagCommit == headCommit:
        return False
    tagLabel = tagCommit[:12] if tagCommit is not None else "unknown"
    headLabel = headCommit[:12] if headCommit is not None else "unknown"
    msg = (
        f"Git tag {pending.tagName} already exists, but points to {tagLabel} instead of HEAD ({headLabel}).\n"
        + f"Fix or delete the tag (e.g. `git tag -d {pending.tagName}`), then run `tablish resume` again."
    )
    raise ReleaseError(msg)


def _finalizeReleaseGit(
    projectRoot: Path,
    pending: PendingRelease,
    *,
    buildDirs: t.Sequence[str] = (),
    onMessage: t.MessageCallback | None = None,
) -> None:
    """Idempotently clean leftover build dirs, commit version files, tag, push, and clear resume state."""
    if buildDirs:
        cleanBuildArtifacts(projectRoot, buildDirs)
    try:
        if _needsVersionCommit(projectRoot, pending):
            stageLabel = (
                f"Staging {pending.commitFiles[0]}" if len(pending.commitFiles) == 1 else "Staging version files"
            )
            runCommand(["git", "add", "--", *pending.commitFiles], stageLabel, cwd=projectRoot, onMessage=onMessage)
            runCommand(
                ["git", "commit", "-m", pending.commitMessage, "--", *pending.commitFiles],
                "Committing version bump",
                cwd=projectRoot,
                onMessage=onMessage,
            )
        else:
            _emit(onMessage, f"==> Version files already committed ({pending.newVersion}); skipping commit.")

        if pending.tagName is not None:
            if _needsReleaseTag(projectRoot, pending):
                assert pending.tagMessage is not None
                runCommand(
                    ["git", "tag", "-a", pending.tagName, "-m", pending.tagMessage],
                    f"Tagging {pending.tagName}",
                    cwd=projectRoot,
                    onMessage=onMessage,
                )
            else:
                _emit(onMessage, f"==> Tag {pending.tagName} already exists on HEAD; skipping tag.")

        pushCmd = ["git", "push", "--follow-tags"] if pending.tagName is not None else ["git", "push"]
        runCommand(pushCmd, "Pushing release commit", cwd=projectRoot, onMessage=onMessage)
    except subprocess.CalledProcessError as exc:
        msg = (
            f"Version {pending.newVersion} was ALREADY uploaded to {pending.targetRepo}, "
            + f"but the git step failed (exit code {exc.returncode}).\n"
            + "Do NOT re-upload; resolve the git issue and run `tablish resume` to finish committing/tagging/pushing."
        )
        raise ReleaseError(msg) from exc

    pendingMod.clearPendingRelease(projectRoot)


# ---------------------------------------------------------------------------
# Release phase helpers
# ---------------------------------------------------------------------------


def _validateReleaseOptions(options: ReleaseOptions) -> None:
    """Validate ReleaseOptions before starting createRelease()."""
    if options.info:
        msg = "Use collectReleaseMetadata() to inspect publish metadata rather than createRelease()."
        raise ReleaseError(msg)
    if options.resume:
        msg = "Use resumeRelease() to finish a pending release rather than createRelease()."
        raise ReleaseError(msg)
    if options.bump is None and options.version is None:
        msg = "Either a bump type (break/major, feature/minor, bugfix/patch) or an explicit version is required."
        raise ReleaseError(msg)
    if options.bump is not None and options.version is not None:
        msg = "Cannot specify both a bump type and an explicit version."
        raise ReleaseError(msg)


def _resolveTargetVersion(semverPath: Path, options: ReleaseOptions) -> _VersionPlan:
    """Read the current semver file and compute the target version for this release."""
    originalContent = semverPath.read_text(encoding="utf-8")
    currentVersion = originalContent.strip()
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

    return _VersionPlan(
        originalContent=originalContent,
        currentVersion=currentVersion,
        newVersion=newVersion,
        releaseKind=releaseKind,
    )


def _resolveReleaseTools(config: TablishConfig, options: ReleaseOptions) -> _ReleaseTools:
    """Locate required build/twine/check-manifest commands and raise if mandatory ones are missing."""
    runLint = not options.skipLint and config.lintEnabled
    needCheckManifest = not runLint and config.checkManifest
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

    # On dry runs twine is optional (`twine check` is skipped with a warning), but build/check-manifest are required.
    missingRequired = (
        buildCmd is None
        or (not options.dryRun and twineCmd is None)
        or (needCheckManifest and checkManifestCmd is None)
    )
    if missingRequired:
        msg = _formatMissingToolsError(missingTools)
        raise ReleaseError(msg)

    assert buildCmd is not None
    return _ReleaseTools(buildCmd=buildCmd, twineCmd=twineCmd, checkManifestCmd=checkManifestCmd)


def _resolveTargetRepo(config: TablishConfig, *, isTest: bool) -> str:
    """Return the user-facing PyPI repository name or URL."""
    if isTest:
        return "test.pypi.org"
    if config.pypiUrl:
        return config.pypiUrl
    return "pypi.org"


def _runPreReleaseChecks(
    root: Path,
    config: TablishConfig,
    tools: _ReleaseTools,
    *,
    options: ReleaseOptions,
    onMessage: t.MessageCallback | None,
) -> None:
    """Run the configured lint command (verifying clean tree) or fall back to check-manifest."""
    runLint = not options.skipLint and config.lintEnabled
    if runLint:
        if config.lintCommand is not None:
            lintCmd: list[str] = list(config.lintCommand)
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
    elif config.checkManifest:
        assert tools.checkManifestCmd is not None
        runCommand(
            [*tools.checkManifestCmd, *config.checkManifestArgs],
            "Checking MANIFEST.in",
            cwd=root,
            onMessage=onMessage,
        )


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


def _rollbackVersionFiles(
    semverPath: Path,
    originalSemverContent: str,
    originalGeneratedContents: t.Sequence[tuple[Path, str | None]],
) -> None:
    """Restore semver and generated files to their pre-release contents (deleting newly created files)."""
    semverPath.write_text(originalSemverContent, encoding="utf-8")
    for targetPath, originalContent in originalGeneratedContents:
        if originalContent is not None:
            targetPath.write_text(originalContent, encoding="utf-8")
        elif targetPath.exists():
            targetPath.unlink()


def _buildAndCheckDist(
    root: Path,
    config: TablishConfig,
    tools: _ReleaseTools,
    *,
    onMessage: t.MessageCallback | None,
) -> list[Path]:
    """Clean build dirs, build sdist/wheel, verify dist/ is non-empty, and run twine check."""
    cleanBuildArtifacts(root, config.buildDirs)
    runCommand(tools.buildCmd, "Building sdist and wheel", cwd=root, onMessage=onMessage)

    distDir = root / "dist"
    distFiles = sorted(distDir.iterdir()) if distDir.is_dir() else []
    if not distFiles:
        msg = "Build succeeded, but no distribution files were created in dist/."
        raise ReleaseError(msg)

    if tools.twineCmd is not None:
        runCommand(
            [*tools.twineCmd, "check", *(str(path) for path in distFiles)],
            "Checking built distributions with twine",
            cwd=root,
            onMessage=onMessage,
        )
    else:
        _emit(onMessage, "Warning: twine is not installed; skipping `twine check`.")
    return distFiles


def _emitDryRunSummary(
    root: Path,
    *,
    newVersion: str,
    targetRepo: str,
    distFiles: t.Sequence[Path],
    renderedGenerated: t.Sequence[tuple[GeneratedFileConfig, Path, str]],
    commitFiles: t.Sequence[str],
    commitMsg: str,
    tagName: str | None,
    isTest: bool,
    onMessage: t.MessageCallback | None,
) -> None:
    """Print the planned release actions for a --dry-run invocation."""
    _emit(onMessage, "\nDry run succeeded. Built artifacts:")
    for path in distFiles:
        _emit(onMessage, f"  - {path.relative_to(root)}")
    for entry, _, content in renderedGenerated:
        _emit(onMessage, f"Would write {entry.path}: {content}")
    _emit(onMessage, f"Would upload {newVersion} to {targetRepo}.")
    if isTest:
        _emit(onMessage, "Would roll back version files and skip git commit/tag/push (--test).")
    else:
        _emit(onMessage, f"Would commit {', '.join(commitFiles)} ({commitMsg!r}) and push.")
        if tagName is not None:
            _emit(onMessage, f"Would create and push annotated tag {tagName}.")


def _uploadToTestPypi(
    root: Path,
    *,
    twineCmd: t.Sequence[str],
    distFiles: t.Sequence[Path],
    uploadEnv: t.Mapping[str, str],
    newVersion: str,
    targetRepo: str,
    testPypiUrl: str,
    verbose: bool,
    onMessage: t.MessageCallback | None,
) -> None:
    """Upload built distribution files to test.pypi.org."""
    uploadCmd = _buildTwineUploadCmd(
        twineCmd,
        distFiles,
        verbose=verbose,
        repositoryUrl=testPypiUrl,
        skipExisting=False,
    )
    try:
        runCommand(uploadCmd, f"Uploading {newVersion} to {targetRepo}", cwd=root, env=uploadEnv, onMessage=onMessage)
    except subprocess.CalledProcessError:
        if not verbose:
            _emit(onMessage, "Hint: Re-run with --verbose-pypi for full PyPI upload error details.")
        raise
    _emit(
        onMessage,
        f"Uploaded {newVersion} to {targetRepo}. Rolling back version files and skipping git commit/push.",
    )


# ---------------------------------------------------------------------------
# Public release & resume entry points
# ---------------------------------------------------------------------------


def createRelease(
    options: ReleaseOptions,
    *,
    projectRoot: Path | None = None,
    config: TablishConfig | None = None,
    onMessage: t.MessageCallback | None = None,
) -> ReleaseResult:
    """Validate repo state, bump version files, build sdist/wheel, and publish."""
    _validateReleaseOptions(options)

    root = (projectRoot or Path.cwd()).resolve()
    resolvedConfig = config if config is not None else loadConfig(root)
    git.ensureInProjectRoot(root, resolvedConfig.projectName)

    if not options.dryRun and not options.test:
        existingPending = loadPendingRelease(root)
        if existingPending is not None:
            msg = pendingMod.describePendingBlock(existingPending)
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

    versionPlan = _resolveTargetVersion(semverPath, options)
    shouldTag = resolvedConfig.tag and not options.noTag
    tagName = resolvedConfig.tagFormat.format(version=versionPlan.newVersion) if shouldTag else None

    if not options.test and not options.dryRun and tagName is not None and gitTagExists(root, tagName):
        msg = f"Git tag {tagName} already exists."
        raise ReleaseError(msg)

    tools = _resolveReleaseTools(resolvedConfig, options)
    token, tokenFromEnv = loadPyPiToken(
        secretsPath,
        isTest=options.test,
        required=not options.dryRun,
        pypiSecretKey=resolvedConfig.pypiSecretKey,
        testPypiSecretKey=resolvedConfig.testPypiSecretKey,
        onMessage=onMessage,
    )

    targetRepo = _resolveTargetRepo(resolvedConfig, isTest=options.test)
    modeSuffix = " (dry run)" if options.dryRun else ""
    _emit(
        onMessage,
        f"Preparing {versionPlan.releaseKind} release for {targetRepo}{modeSuffix}: "
        + f"{versionPlan.currentVersion} -> {versionPlan.newVersion}",
    )

    _runPreReleaseChecks(root, resolvedConfig, tools, options=options, onMessage=onMessage)

    renderedGenerated = _renderGeneratedFiles(resolvedConfig.generatedFiles, versionPlan.newVersion, root)
    originalGeneratedContents: list[tuple[Path, str | None]] = [
        (targetPath, targetPath.read_text(encoding="utf-8") if targetPath.is_file() else None)
        for _, targetPath, _ in renderedGenerated
    ]

    commitFiles = (str(resolvedConfig.semverFile), *(str(entry.path) for entry, _, _ in renderedGenerated))
    commitMsg = resolvedConfig.commitMessage.format(version=versionPlan.newVersion)
    tagMsg = resolvedConfig.tagMessage.format(version=versionPlan.newVersion) if tagName is not None else None
    builtRelPaths: tuple[Path, ...] = ()
    keepVersionBump = False
    keepBuildArtifacts = False
    pendingRelease: PendingRelease | None = None

    def _markUploadStarted() -> None:
        nonlocal keepVersionBump, keepBuildArtifacts
        keepVersionBump = True
        keepBuildArtifacts = True

    try:
        _emit(onMessage, f"Bumping {resolvedConfig.semverFile} to {versionPlan.newVersion}...")
        for _, targetPath, content in renderedGenerated:
            targetPath.parent.mkdir(parents=True, exist_ok=True)
            targetPath.write_text(content, encoding="utf-8")
        semverPath.write_text(versionPlan.newVersion, encoding="utf-8")

        distFiles = _buildAndCheckDist(root, resolvedConfig, tools, onMessage=onMessage)
        builtRelPaths = tuple(path.relative_to(root) for path in distFiles)
        if options.dryRun:
            _emitDryRunSummary(
                root,
                newVersion=versionPlan.newVersion,
                targetRepo=targetRepo,
                distFiles=distFiles,
                renderedGenerated=renderedGenerated,
                commitFiles=commitFiles,
                commitMsg=commitMsg,
                tagName=tagName,
                isTest=options.test,
                onMessage=onMessage,
            )
            return ReleaseResult(
                previousVersion=versionPlan.currentVersion,
                newVersion=versionPlan.newVersion,
                tagName=tagName,
                targetRepo=targetRepo,
                dryRun=True,
                test=options.test,
                builtFiles=builtRelPaths,
            )

        assert tools.twineCmd is not None
        assert token is not None
        uploadEnv = _buildTwineUploadEnv(token, tokenFromEnv=tokenFromEnv)

        if options.test:
            _uploadToTestPypi(
                root,
                twineCmd=tools.twineCmd,
                distFiles=distFiles,
                uploadEnv=uploadEnv,
                newVersion=versionPlan.newVersion,
                targetRepo=targetRepo,
                testPypiUrl=resolvedConfig.testPypiUrl,
                verbose=options.verbosePypi,
                onMessage=onMessage,
            )
            return ReleaseResult(
                previousVersion=versionPlan.currentVersion,
                newVersion=versionPlan.newVersion,
                tagName=None,
                targetRepo=targetRepo,
                dryRun=False,
                test=True,
                builtFiles=builtRelPaths,
            )

        basePending = PendingRelease(
            previousVersion=versionPlan.currentVersion,
            newVersion=versionPlan.newVersion,
            targetRepo=targetRepo,
            commitFiles=commitFiles,
            commitMessage=commitMsg,
            tagName=tagName,
            tagMessage=tagMsg,
            stage="upload",
            repositoryUrl=resolvedConfig.pypiUrl,
            uploadedFiles=(),
            remainingFiles=tuple(path.as_posix() for path in builtRelPaths),
        )
        pendingRelease, _ = _uploadDistFiles(
            root,
            basePending,
            twineCmd=tools.twineCmd,
            uploadEnv=uploadEnv,
            verbose=options.verbosePypi,
            isResume=False,
            onMessage=onMessage,
            onUploadStarted=_markUploadStarted,
        )
        keepVersionBump = True
        keepBuildArtifacts = False
    finally:
        if not keepBuildArtifacts:
            cleanBuildArtifacts(root, resolvedConfig.buildDirs)
        if not keepVersionBump:
            _rollbackVersionFiles(semverPath, versionPlan.originalContent, originalGeneratedContents)

    assert pendingRelease is not None
    _finalizeReleaseGit(root, pendingRelease, buildDirs=resolvedConfig.buildDirs, onMessage=onMessage)

    _emit(onMessage, f"Successfully released {versionPlan.newVersion}!")
    return ReleaseResult(
        previousVersion=versionPlan.currentVersion,
        newVersion=versionPlan.newVersion,
        tagName=tagName,
        targetRepo=targetRepo,
        dryRun=False,
        test=False,
        builtFiles=builtRelPaths,
    )


def _prepareResumeUpload(
    root: Path,
    config: TablishConfig,
    pending: PendingRelease,
    *,
    dryRun: bool,
    onMessage: t.MessageCallback | None,
) -> tuple[list[str] | None, str | None, bool]:
    """Validate credentials, twine availability, and leftover dist files before resuming an upload."""
    secretsPath = resolveSecretsPath(root, config.secretsFile)
    ensureSecretsGitIgnored(root, secretsPath)
    twineCmd = findToolCommand("twine", "twine")
    if twineCmd is None and not dryRun:
        msg = _formatMissingToolsError(["twine"])
        raise ReleaseError(msg)
    token, tokenFromEnv = loadPyPiToken(
        secretsPath,
        isTest=False,
        required=not dryRun,
        pypiSecretKey=config.pypiSecretKey,
        testPypiSecretKey=config.testPypiSecretKey,
        onMessage=onMessage,
    )
    for relFile in pending.remainingFiles:
        if not (root / relFile).is_file():
            msg = (
                f"Pending distribution file {relFile} is missing; cannot resume upload.\n"
                + f"Rebuild version {pending.newVersion} or upload it manually with "
                + "`twine upload --skip-existing`."
            )
            raise ReleaseError(msg)
    return (twineCmd, token, tokenFromEnv)


def _emitResumeDryRunSummary(
    root: Path,
    config: TablishConfig,
    pending: PendingRelease,
    *,
    onMessage: t.MessageCallback | None,
) -> None:
    """Preview remaining upload and git steps for `tablish resume --dry-run`."""
    if pending.stage == "upload":
        _emit(onMessage, f"Would upload remaining file(s) with --skip-existing: {', '.join(pending.remainingFiles)}")
        if config.buildDirs:
            _emit(onMessage, f"Would clean build directories: {', '.join(config.buildDirs)}")
    if _needsVersionCommit(root, pending):
        _emit(onMessage, f"Would stage and commit {', '.join(pending.commitFiles)} ({pending.commitMessage!r}).")
    else:
        _emit(onMessage, f"Version files already committed ({pending.newVersion}); would skip commit.")
    if pending.tagName is not None:
        if _needsReleaseTag(root, pending):
            _emit(onMessage, f"Would create annotated tag {pending.tagName}.")
        else:
            _emit(onMessage, f"Tag {pending.tagName} already exists on HEAD; would skip tag.")
    pushDesc = "git push --follow-tags" if pending.tagName is not None else "git push"
    _emit(onMessage, f"Would run `{pushDesc}`.")


def resumeRelease(
    options: ReleaseOptions | None = None,
    *,
    projectRoot: Path | None = None,
    config: TablishConfig | None = None,
    onMessage: t.MessageCallback | None = None,
) -> ReleaseResult:
    """Finish pending upload and/or git commit/tag/push steps after an interrupted release."""
    opts = options if options is not None else ReleaseOptions(resume=True)
    if opts.test:
        msg = "Cannot combine 'resume' with --test."
        raise ReleaseError(msg)

    root = (projectRoot or Path.cwd()).resolve()
    resolvedConfig = config if config is not None else loadConfig(root)
    git.ensureInProjectRoot(root, resolvedConfig.projectName)

    pending = loadPendingRelease(root)
    if pending is None:
        msg = "No pending release to resume (no incomplete upload or git steps found)."
        raise ReleaseError(msg)

    # Apply --no-tag onto the pending state so retries after an upload failure also skip tagging.
    effectivePending = replace(pending, tagName=None, tagMessage=None) if opts.noTag else pending
    modeSuffix = " (dry run)" if opts.dryRun else ""
    twineCmd: list[str] | None = None
    token: str | None = None
    tokenFromEnv = False

    if effectivePending.stage == "upload":
        _emit(
            onMessage,
            f"Resuming partial upload for {effectivePending.newVersion} to "
            + f"{effectivePending.targetRepo}{modeSuffix}...",
        )
        twineCmd, token, tokenFromEnv = _prepareResumeUpload(
            root,
            resolvedConfig,
            effectivePending,
            dryRun=opts.dryRun,
            onMessage=onMessage,
        )
    else:
        _emit(
            onMessage,
            f"Resuming release {effectivePending.newVersion} "
            + f"(already uploaded to {effectivePending.targetRepo}){modeSuffix}...",
        )

    if opts.dryRun:
        _emitResumeDryRunSummary(root, resolvedConfig, effectivePending, onMessage=onMessage)
        return ReleaseResult(
            previousVersion=effectivePending.previousVersion,
            newVersion=effectivePending.newVersion,
            tagName=effectivePending.tagName,
            targetRepo=effectivePending.targetRepo,
            dryRun=True,
            test=False,
            builtFiles=tuple(Path(p) for p in effectivePending.remainingFiles),
        )

    gitPending = effectivePending
    uploadedOnResume: list[Path] = []
    if effectivePending.stage == "upload":
        assert twineCmd is not None
        assert token is not None
        uploadEnv = _buildTwineUploadEnv(token, tokenFromEnv=tokenFromEnv)
        gitPending, uploadedOnResume = _uploadDistFiles(
            root,
            effectivePending,
            twineCmd=twineCmd,
            uploadEnv=uploadEnv,
            verbose=opts.verbosePypi,
            isResume=True,
            onMessage=onMessage,
        )
        cleanBuildArtifacts(root, resolvedConfig.buildDirs)

    _finalizeReleaseGit(root, gitPending, buildDirs=resolvedConfig.buildDirs, onMessage=onMessage)
    _emit(onMessage, f"Successfully finished releasing {gitPending.newVersion}!")
    return ReleaseResult(
        previousVersion=gitPending.previousVersion,
        newVersion=gitPending.newVersion,
        tagName=gitPending.tagName,
        targetRepo=gitPending.targetRepo,
        dryRun=False,
        test=False,
        builtFiles=tuple(uploadedOnResume),
    )
