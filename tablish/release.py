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
    TABLISH_KDL_FILENAME,
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
    resume: bool = False


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
    if options.resume:
        msg = "Use resumeRelease() to finish a pending release rather than createRelease()."
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

    if not options.dryRun and not options.test:
        pending = loadPendingRelease(root)
        if pending is not None:
            if pending.stage == "upload":
                remCount = len(pending.remainingFiles)
                remNoun = "file" if remCount == 1 else "files"
                msg = (
                    f"A previous release ({pending.newVersion}) was partially uploaded to {pending.targetRepo} "
                    + f"({remCount} {remNoun} remaining).\n"
                    + "Run `tablish resume` to finish uploading and committing/tagging/pushing "
                    + "before starting a new release."
                )
            else:
                msg = (
                    f"A previous release ({pending.newVersion}) was already uploaded to {pending.targetRepo}, "
                    + "but its git steps have not been completed.\n"
                    + "Run `tablish resume` to finish committing/tagging/pushing before starting a new release."
                )
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

    commitFiles = [str(resolvedConfig.semverFile), *(str(entry.path) for entry, _, _ in renderedGenerated)]
    commitMsg = resolvedConfig.commitMessage.format(version=newVersion)
    tagMsg = resolvedConfig.tagMessage.format(version=newVersion) if tagName is not None else None
    distFiles: list[Path] = []
    keepVersionBump = False
    keepBuildArtifacts = False
    pendingRelease: PendingRelease | None = None
    try:
        _emit(onMessage, f"Bumping {resolvedConfig.semverFile} to {newVersion}...")
        for _, targetPath, content in renderedGenerated:
            targetPath.parent.mkdir(parents=True, exist_ok=True)
            targetPath.write_text(content, encoding="utf-8")
        semverPath.write_text(newVersion, encoding="utf-8")

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
        uploadEnv = _buildTwineUploadEnv(token, tokenFromEnv=tokenFromEnv)

        if options.test:
            uploadCmd = _buildTwineUploadCmd(
                twineCmd,
                distFiles,
                verbose=options.verbosePypi,
                repositoryUrl=resolvedConfig.testPypiUrl,
                skipExisting=False,
            )
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

        relDistFiles = [path.relative_to(root).as_posix() for path in distFiles]
        uploadedRelPaths: list[str] = []
        for idx, distPath in enumerate(distFiles):
            relPath = relDistFiles[idx]
            uploadCmd = _buildTwineUploadCmd(
                twineCmd,
                [distPath],
                verbose=options.verbosePypi,
                repositoryUrl=resolvedConfig.pypiUrl,
                skipExisting=False,
            )
            desc = (
                f"Uploading {newVersion} ({idx + 1}/{len(distFiles)}: {distPath.name}) to {targetRepo}"
                if len(distFiles) > 1
                else f"Uploading {newVersion} to {targetRepo}"
            )
            try:
                runCommand(uploadCmd, desc, cwd=root, env=uploadEnv, onMessage=onMessage)
            except subprocess.CalledProcessError as exc:
                if not options.verbosePypi:
                    _emit(onMessage, "Hint: Re-run with --verbose-pypi for full PyPI upload error details.")
                if uploadedRelPaths:
                    remainingRelPaths = tuple(relDistFiles[idx:])
                    partialPending = PendingRelease(
                        previousVersion=currentVersion,
                        newVersion=newVersion,
                        targetRepo=targetRepo,
                        commitFiles=tuple(commitFiles),
                        commitMessage=commitMsg,
                        tagName=tagName,
                        tagMessage=tagMsg,
                        stage="upload",
                        repositoryUrl=resolvedConfig.pypiUrl,
                        uploadedFiles=tuple(uploadedRelPaths),
                        remainingFiles=remainingRelPaths,
                    )
                    _savePendingRelease(root, partialPending)
                    keepVersionBump = True
                    keepBuildArtifacts = True
                    uploadedSummary = ", ".join(uploadedRelPaths)
                    remainingSummary = ", ".join(remainingRelPaths)
                    msg = (
                        f"Version {newVersion} was PARTIALLY uploaded to {targetRepo} "
                        + f"(uploaded: {uploadedSummary}; remaining: {remainingSummary}), "
                        + f"but uploading {relPath} failed (exit code {exc.returncode}).\n"
                        + "Do NOT start a new release; resolve the upload issue and run `tablish resume` "
                        + "to upload the remaining file(s) and finish committing/tagging/pushing."
                    )
                    raise ReleaseError(msg) from exc
                raise

            uploadedRelPaths.append(relPath)
            remainingAfter = tuple(relDistFiles[idx + 1 :])
            if remainingAfter:
                keepVersionBump = True
                keepBuildArtifacts = True
                _savePendingRelease(
                    root,
                    PendingRelease(
                        previousVersion=currentVersion,
                        newVersion=newVersion,
                        targetRepo=targetRepo,
                        commitFiles=tuple(commitFiles),
                        commitMessage=commitMsg,
                        tagName=tagName,
                        tagMessage=tagMsg,
                        stage="upload",
                        repositoryUrl=resolvedConfig.pypiUrl,
                        uploadedFiles=tuple(uploadedRelPaths),
                        remainingFiles=remainingAfter,
                    ),
                )

        pendingRelease = PendingRelease(
            previousVersion=currentVersion,
            newVersion=newVersion,
            targetRepo=targetRepo,
            commitFiles=tuple(commitFiles),
            commitMessage=commitMsg,
            tagName=tagName,
            tagMessage=tagMsg,
            stage="git",
            repositoryUrl=resolvedConfig.pypiUrl,
            uploadedFiles=tuple(uploadedRelPaths),
            remainingFiles=(),
        )
        _savePendingRelease(root, pendingRelease)
        keepVersionBump = True
        keepBuildArtifacts = False
    finally:
        if not keepBuildArtifacts:
            cleanBuildArtifacts(root, resolvedConfig.buildDirs)
        if not keepVersionBump:
            semverPath.write_text(originalSemverContent, encoding="utf-8")
            for targetPath, originalContent in originalGeneratedContents:
                if originalContent is not None:
                    targetPath.write_text(originalContent, encoding="utf-8")
                elif targetPath.exists():
                    targetPath.unlink()

    assert pendingRelease is not None
    _finalizeReleaseGit(root, pendingRelease, buildDirs=resolvedConfig.buildDirs, onMessage=onMessage)

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

    if not inProjectRoot(root, resolvedConfig.projectName):
        if resolvedConfig.projectName:
            msg = f"Run this command from inside the {resolvedConfig.projectName} project root folder."
        else:
            msg = f"Run this command from inside the git project root folder ({root})."
        raise ReleaseError(msg)

    pending = loadPendingRelease(root)
    if pending is None:
        msg = "No pending release to resume (no incomplete upload or git steps found)."
        raise ReleaseError(msg)

    effectivePending = (
        PendingRelease(
            previousVersion=pending.previousVersion,
            newVersion=pending.newVersion,
            targetRepo=pending.targetRepo,
            commitFiles=pending.commitFiles,
            commitMessage=pending.commitMessage,
            tagName=None,
            tagMessage=None,
            stage=pending.stage,
            repositoryUrl=pending.repositoryUrl,
            uploadedFiles=pending.uploadedFiles,
            remainingFiles=pending.remainingFiles,
        )
        if opts.noTag
        else pending
    )

    modeSuffix = " (dry run)" if opts.dryRun else ""
    if effectivePending.stage == "upload":
        _emit(
            onMessage,
            f"Resuming partial upload for {effectivePending.newVersion} to "
            + f"{effectivePending.targetRepo}{modeSuffix}...",
        )
        secretsPath = resolveSecretsPath(root, resolvedConfig.secretsFile)
        ensureSecretsGitIgnored(root, secretsPath)
        twineCmd = findToolCommand("twine", "twine")
        if twineCmd is None and not opts.dryRun:
            msg = (
                "Missing required release tool(s): twine\n"
                + f"Active Python: {sys.executable}\n"
                + "Install them with:\n"
                + "  python -m pip install twine"
            )
            raise ReleaseError(msg)
        token, tokenFromEnv = loadPyPiToken(
            secretsPath,
            isTest=False,
            required=not opts.dryRun,
            pypiSecretKey=resolvedConfig.pypiSecretKey,
            testPypiSecretKey=resolvedConfig.testPypiSecretKey,
            onMessage=onMessage,
        )
        for relFile in effectivePending.remainingFiles:
            if not (root / relFile).is_file():
                msg = (
                    f"Pending distribution file {relFile} is missing; cannot resume upload.\n"
                    + f"Rebuild version {effectivePending.newVersion} or upload it manually with "
                    + "`twine upload --skip-existing`."
                )
                raise ReleaseError(msg)
    else:
        _emit(
            onMessage,
            f"Resuming release {effectivePending.newVersion} "
            + f"(already uploaded to {effectivePending.targetRepo}){modeSuffix}...",
        )
        twineCmd = None
        token = None
        tokenFromEnv = False

    if opts.dryRun:
        if effectivePending.stage == "upload":
            remList = ", ".join(effectivePending.remainingFiles)
            _emit(onMessage, f"Would upload remaining file(s) with --skip-existing: {remList}")
            if resolvedConfig.buildDirs:
                _emit(onMessage, f"Would clean build directories: {', '.join(resolvedConfig.buildDirs)}")
        needCommit = _needsVersionCommit(root, effectivePending)
        needTag = _needsReleaseTag(root, effectivePending)
        if needCommit:
            fileList = ", ".join(effectivePending.commitFiles)
            _emit(onMessage, f"Would stage and commit {fileList} ({effectivePending.commitMessage!r}).")
        else:
            _emit(onMessage, f"Version files already committed ({effectivePending.newVersion}); would skip commit.")
        if effectivePending.tagName is not None:
            if needTag:
                _emit(onMessage, f"Would create annotated tag {effectivePending.tagName}.")
            else:
                _emit(onMessage, f"Tag {effectivePending.tagName} already exists on HEAD; would skip tag.")
        pushDesc = "git push --follow-tags" if effectivePending.tagName is not None else "git push"
        _emit(onMessage, f"Would run `{pushDesc}`.")
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
        uploadedSoFar = list(effectivePending.uploadedFiles)
        remainingList = list(effectivePending.remainingFiles)
        for idx, relPath in enumerate(remainingList):
            distPath = root / relPath
            uploadCmd = _buildTwineUploadCmd(
                twineCmd,
                [distPath],
                verbose=opts.verbosePypi,
                repositoryUrl=effectivePending.repositoryUrl,
                skipExisting=True,
            )
            desc = (
                f"Uploading remaining {effectivePending.newVersion} "
                + f"({idx + 1}/{len(remainingList)}: {distPath.name}) to {effectivePending.targetRepo}"
                if len(remainingList) > 1
                else f"Uploading remaining {effectivePending.newVersion} ({distPath.name}) "
                + f"to {effectivePending.targetRepo}"
            )
            try:
                runCommand(uploadCmd, desc, cwd=root, env=uploadEnv, onMessage=onMessage)
            except subprocess.CalledProcessError as exc:
                if not opts.verbosePypi:
                    _emit(onMessage, "Hint: Re-run `tablish resume --verbose-pypi` for full PyPI upload error details.")
                stillRemaining = tuple(remainingList[idx:])
                _savePendingRelease(
                    root,
                    PendingRelease(
                        previousVersion=effectivePending.previousVersion,
                        newVersion=effectivePending.newVersion,
                        targetRepo=effectivePending.targetRepo,
                        commitFiles=effectivePending.commitFiles,
                        commitMessage=effectivePending.commitMessage,
                        tagName=effectivePending.tagName,
                        tagMessage=effectivePending.tagMessage,
                        stage="upload",
                        repositoryUrl=effectivePending.repositoryUrl,
                        uploadedFiles=tuple(uploadedSoFar),
                        remainingFiles=stillRemaining,
                    ),
                )
                msg = (
                    f"Uploading {relPath} for {effectivePending.newVersion} failed (exit code {exc.returncode}); "
                    + f"remaining: {', '.join(stillRemaining)}.\n"
                    + "Resolve the upload issue and run `tablish resume` again."
                )
                raise ReleaseError(msg) from exc

            uploadedSoFar.append(relPath)
            uploadedOnResume.append(Path(relPath))
            remainingAfter = tuple(remainingList[idx + 1 :])
            if remainingAfter:
                _savePendingRelease(
                    root,
                    PendingRelease(
                        previousVersion=effectivePending.previousVersion,
                        newVersion=effectivePending.newVersion,
                        targetRepo=effectivePending.targetRepo,
                        commitFiles=effectivePending.commitFiles,
                        commitMessage=effectivePending.commitMessage,
                        tagName=effectivePending.tagName,
                        tagMessage=effectivePending.tagMessage,
                        stage="upload",
                        repositoryUrl=effectivePending.repositoryUrl,
                        uploadedFiles=tuple(uploadedSoFar),
                        remainingFiles=remainingAfter,
                    ),
                )

        gitPending = PendingRelease(
            previousVersion=effectivePending.previousVersion,
            newVersion=effectivePending.newVersion,
            targetRepo=effectivePending.targetRepo,
            commitFiles=effectivePending.commitFiles,
            commitMessage=effectivePending.commitMessage,
            tagName=effectivePending.tagName,
            tagMessage=effectivePending.tagMessage,
            stage="git",
            repositoryUrl=effectivePending.repositoryUrl,
            uploadedFiles=tuple(uploadedSoFar),
            remainingFiles=(),
        )
        _savePendingRelease(root, gitPending)
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


def _gitCommitHash(projectRoot: Path, rev: str) -> str | None:
    """Resolve a git revision to its full commit SHA, or None if unavailable."""
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", rev],
        cwd=projectRoot,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha if sha else None


def _getPendingReleasePath(projectRoot: Path) -> Path | None:
    """Return the path to the pending release state file inside the repository's git dir."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-path", PENDING_RELEASE_FILENAME],
            cwd=projectRoot,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    rawPath = result.stdout.strip()
    if not rawPath:
        return None
    path = Path(rawPath)
    return path if path.is_absolute() else (projectRoot / path)


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


def _savePendingRelease(projectRoot: Path, pending: PendingRelease) -> None:
    """Persist pending upload/git release state into the repository's git directory."""
    path = _getPendingReleasePath(projectRoot)
    if path is None:
        return
    payload = {
        "stage": pending.stage,
        "previousVersion": pending.previousVersion,
        "newVersion": pending.newVersion,
        "targetRepo": pending.targetRepo,
        "repositoryUrl": pending.repositoryUrl,
        "commitFiles": list(pending.commitFiles),
        "commitMessage": pending.commitMessage,
        "tagName": pending.tagName,
        "tagMessage": pending.tagMessage,
        "uploadedFiles": list(pending.uploadedFiles),
        "remainingFiles": list(pending.remainingFiles),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _clearPendingRelease(projectRoot: Path) -> None:
    """Remove any saved pending release state file."""
    path = _getPendingReleasePath(projectRoot)
    if path is not None:
        path.unlink(missing_ok=True)


def loadPendingRelease(projectRoot: Path) -> PendingRelease | None:
    """Load pending upload/git release state from the repository's git directory, if present."""
    path = _getPendingReleasePath(projectRoot)
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
    previousVersion = rawData.get("previousVersion")
    newVersion = rawData.get("newVersion")
    targetRepo = rawData.get("targetRepo")
    repositoryUrl = rawData.get("repositoryUrl")
    rawCommitFiles = rawData.get("commitFiles")
    commitMessage = rawData.get("commitMessage")
    tagName = rawData.get("tagName")
    tagMessage = rawData.get("tagMessage")
    rawUploadedFiles = rawData.get("uploadedFiles", [])
    rawRemainingFiles = rawData.get("remainingFiles", [])

    if (
        rawStage not in ("upload", "git")
        or not isinstance(previousVersion, str)
        or not isinstance(newVersion, str)
        or not newVersion.strip()
        or not isinstance(targetRepo, str)
        or not targetRepo.strip()
        or (repositoryUrl is not None and (not isinstance(repositoryUrl, str) or not repositoryUrl.strip()))
        or not isinstance(rawCommitFiles, list)
        or not rawCommitFiles
        or not all(isinstance(item, str) and item.strip() for item in rawCommitFiles)
        or not isinstance(commitMessage, str)
        or not commitMessage.strip()
        or (tagName is not None and (not isinstance(tagName, str) or not tagName.strip()))
        or (tagMessage is not None and not isinstance(tagMessage, str))
        or (tagName is not None and tagMessage is None)
        or not isinstance(rawUploadedFiles, list)
        or not all(isinstance(item, str) and item.strip() for item in rawUploadedFiles)
        or not isinstance(rawRemainingFiles, list)
        or not all(isinstance(item, str) and item.strip() for item in rawRemainingFiles)
        or (rawStage == "upload" and not rawRemainingFiles)
    ):
        msg = f"Invalid pending release state in {path}: missing or malformed fields."
        raise ReleaseError(msg)

    stage: t.ResumeStage = "upload" if rawStage == "upload" else "git"
    return PendingRelease(
        previousVersion=previousVersion,
        newVersion=newVersion,
        targetRepo=targetRepo,
        commitFiles=tuple(rawCommitFiles),
        commitMessage=commitMessage,
        tagName=tagName,
        tagMessage=tagMessage,
        stage=stage,
        repositoryUrl=repositoryUrl,
        uploadedFiles=tuple(rawUploadedFiles),
        remainingFiles=tuple(rawRemainingFiles),
    )


def _needsVersionCommit(projectRoot: Path, pending: PendingRelease) -> bool:
    """Return True if any pending commit files still have uncommitted changes."""
    for relFile in pending.commitFiles:
        if not (projectRoot / relFile).is_file():
            msg = f"Expected release file {relFile} is missing. Restore it before running `tablish resume`."
            raise ReleaseError(msg)
    statusResult = subprocess.run(
        ["git", "status", "--porcelain", "--", *pending.commitFiles],
        cwd=projectRoot,
        capture_output=True,
        text=True,
        check=True,
    )
    if any(line.strip() for line in statusResult.stdout.splitlines()):
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
    tagCommit = _gitCommitHash(projectRoot, f"refs/tags/{pending.tagName}^{{commit}}")
    headCommit = _gitCommitHash(projectRoot, "HEAD")
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

    _clearPendingRelease(projectRoot)


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
            sourceDetail=resolvedConfig.explicitOrigin(key) or resolvedConfig.inferredOrigin(key),
            status=status,
            details=details,
        )

    items: list[MetadataItem] = []

    # Project section
    if projectRoot is not None:
        items.append(MetadataItem("Project", "project-root", str(root), "cli", "-C"))
    else:
        items.append(MetadataItem("Project", "project-root", str(root), "default", "cwd"))

    if not resolvedConfig.hasPyproject:
        pyprojectDisplay = "not found"
    elif resolvedConfig.hasTablishTable:
        pyprojectDisplay = "[tool.tablish] present"
    else:
        pyprojectDisplay = "no [tool.tablish] table"
    items.append(
        MetadataItem(
            "Project",
            "pyproject.toml",
            pyprojectDisplay,
            "explicit" if resolvedConfig.hasTablishTable else "default",
        ),
    )
    items.append(
        MetadataItem(
            "Project",
            TABLISH_KDL_FILENAME,
            "present" if resolvedConfig.hasTablishKdl else "not found",
            "explicit" if resolvedConfig.hasTablishKdl else "default",
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
        items.append(
            MetadataItem(
                "PyPI & Credentials",
                "target-repo",
                resolvedConfig.pypiUrl,
                "explicit",
                resolvedConfig.explicitOrigin("pypi-url"),
            ),
        )
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

    if isValidRepo:
        try:
            pendingResume = loadPendingRelease(root)
        except ReleaseError:
            items.append(
                MetadataItem(
                    "Git & Release",
                    "pending-resume",
                    f"invalid {PENDING_RELEASE_FILENAME} in git dir",
                    "derived",
                    status="error",
                ),
            )
        else:
            if pendingResume is not None:
                if pendingResume.stage == "upload":
                    remCount = len(pendingResume.remainingFiles)
                    remNoun = "file" if remCount == 1 else "files"
                    pendingSummary = (
                        f"{pendingResume.newVersion} partially uploaded to {pendingResume.targetRepo} "
                        + f"({remCount} {remNoun} remaining; run `tablish resume`)"
                    )
                else:
                    pendingSummary = (
                        f"{pendingResume.newVersion} uploaded to {pendingResume.targetRepo} (run `tablish resume`)"
                    )
                items.append(
                    MetadataItem(
                        "Git & Release",
                        "pending-resume",
                        pendingSummary,
                        "derived",
                        status="warning",
                    ),
                )

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
