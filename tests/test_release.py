"""Tests for tablish release workflow, helpers, and CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tablish import (
    PENDING_RELEASE_FILENAME,
    ReleaseError,
    ReleaseOptions,
    bumpSemver,
    collectReleaseMetadata,
    createRelease,
    ensureSecretsGitIgnored,
    formatReleaseMetadata,
    inProjectRoot,
    isGitIgnored,
    loadPendingRelease,
    loadPyPiToken,
    main,
    parseArgs,
    parseSemver,
    release as releaseMod,
    resolveSecretsPath,
    resumeRelease,
    supportsColor,
    t,
)


def _git(repo: Path, *args: str) -> str:
    """Run a git command inside repo and return stripped stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _attachBareOrigin(tmpPath: Path, repo: Path, name: str, *, pushInitial: bool = False) -> Path:
    """Create a bare origin repository, wire it up to repo, and optionally push the initial branch."""
    originsDir = tmpPath / "origins"
    originsDir.mkdir(exist_ok=True)
    remoteRepo = originsDir / f"{name}.git"
    subprocess.run(["git", "init", "--bare", str(remoteRepo)], check=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(remoteRepo))
    branch = _git(repo, "branch", "--show-current")
    if pushInitial:
        _git(repo, "push", "-u", "origin", branch)
    else:
        _git(repo, "config", f"branch.{branch}.remote", "origin")
        _git(repo, "config", f"branch.{branch}.merge", f"refs/heads/{branch}")
    return remoteRepo


def _createSampleRepo(
    tmpPath: Path,
    *,
    name: str = "demo",
    pyprojectBody: str | None = None,
    withRemote: bool = True,
    withLintScript: bool = True,
    lintBody: str = "#!/bin/sh\nexit 0\n",
) -> tuple[Path, Path | None]:
    """Create a committed sample git repository with optional bare origin remote."""
    repo = tmpPath / name
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "core.excludesFile", os.devnull)

    if pyprojectBody is None:
        pyprojectBody = f'[project]\nname = "{name}"\n\n[tool.tablish]\nproject-name = "{name}"\n'
    (repo / "pyproject.toml").write_text(pyprojectBody, encoding="utf-8")
    (repo / "semver.txt").write_text("1.2.3", encoding="utf-8")
    (repo / ".gitignore").write_text("secrets.json\nbuild/\ndist/\n", encoding="utf-8")
    if withLintScript:
        lintScript = repo / "lint"
        lintScript.write_text(lintBody, encoding="utf-8")
        lintScript.chmod(0o755)

    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Initial commit")

    remoteRepo = _attachBareOrigin(tmpPath, repo, name, pushInitial=True) if withRemote else None
    return (repo, remoteRepo)


def _installFakeTooling(
    monkeypatch: pytest.MonkeyPatch,
    tmpPath: Path,
    *,
    failUpload: bool = False,
    failUploadSuffix: str | None = None,
    recordLog: Path | None = None,
    missingTools: t.Sequence[str] = (),
) -> Path:
    """Monkeypatch findToolCommand to invoke lightweight local Python scripts."""
    logPath = recordLog or (tmpPath / "tool_calls.jsonl")

    fakeBuild = tmpPath / "fake_build.py"
    fakeBuild.write_text(
        textwrap.dedent(
            f"""\
            import json, pathlib, sys

            log = pathlib.Path({str(logPath)!r})
            with log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({{"tool": "build", "args": sys.argv[1:]}}) + "\\n")
            ver = pathlib.Path("semver.txt").read_text(encoding="utf-8").strip()
            dist = pathlib.Path("dist")
            dist.mkdir(exist_ok=True)
            (dist / f"demo-{{ver}}.tar.gz").write_text("sdist", encoding="utf-8")
            (dist / f"demo-{{ver}}-py3-none-any.whl").write_text("wheel", encoding="utf-8")
            """,
        ),
        encoding="utf-8",
    )

    fakeTwine = tmpPath / "fake_twine.py"
    fakeTwine.write_text(
        textwrap.dedent(
            f"""\
            import json, os, pathlib, sys

            log = pathlib.Path({str(logPath)!r})
            with log.open("a", encoding="utf-8") as fh:
                entry = {{
                    "tool": "twine",
                    "args": sys.argv[1:],
                    "user": os.environ.get("TWINE_USERNAME"),
                    "auth": os.environ.get("TWINE_PASSWORD"),
                }}
                fh.write(json.dumps(entry) + "\\n")
            if {failUpload!r} and sys.argv[1:2] == ["upload"]:
                sys.exit(2)
            failSuffix = {failUploadSuffix!r}
            if failSuffix is not None and sys.argv[1:2] == ["upload"] and any(
                arg.endswith(failSuffix) for arg in sys.argv[2:]
            ):
                sys.exit(3)
            """,
        ),
        encoding="utf-8",
    )

    fakeCheckManifest = tmpPath / "fake_check_manifest.py"
    fakeCheckManifest.write_text(
        textwrap.dedent(
            f"""\
            import json, pathlib, sys

            log = pathlib.Path({str(logPath)!r})
            with log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({{"tool": "check_manifest", "args": sys.argv[1:]}}) + "\\n")
            """,
        ),
        encoding="utf-8",
    )

    toolMap: dict[str, list[str]] = {
        "build": [sys.executable, str(fakeBuild)],
        "twine": [sys.executable, str(fakeTwine)],
        "check_manifest": [sys.executable, str(fakeCheckManifest)],
    }

    def _fakeFindTool(moduleName: str, _executableName: str) -> list[str] | None:
        if moduleName in missingTools:
            return None
        return toolMap.get(moduleName)

    monkeypatch.setattr(releaseMod, "findToolCommand", _fakeFindTool)
    return logPath


def _readToolLog(logPath: Path) -> list[dict[str, object]]:
    """Read recorded JSONL tool invocations."""
    if not logPath.is_file():
        return []
    return [json.loads(line) for line in logPath.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_parse_and_bump_semver() -> None:
    """Parse valid semver strings, bump each component, and reject malformed versions."""
    assert parseSemver("1.2.3\n") == (1, 2, 3)
    assert bumpSemver((1, 2, 3), "bugfix") == "1.2.4"
    assert bumpSemver((1, 2, 3), "patch") == "1.2.4"
    assert bumpSemver((1, 2, 3), "feature") == "1.3.0"
    assert bumpSemver((1, 2, 3), "minor") == "1.3.0"
    assert bumpSemver((1, 2, 3), "break") == "2.0.0"
    assert bumpSemver((1, 2, 3), "major") == "2.0.0"

    for bad in ("1.2", "1.2.3.4", "1.a.3", "-1.0.0"):
        with pytest.raises(ReleaseError):
            parseSemver(bad)


def test_parse_args_modes() -> None:
    """Verify CLI flag parsing, positional semver handling, and invalid target checks."""
    opts, projectDir = parseArgs(
        ["feature", "--dry-run", "--test", "--skip-lint", "--no-tag", "--verbose-pypi", "-C", "sub"],
    )
    assert opts == ReleaseOptions(
        bump="feature",
        version=None,
        test=True,
        dryRun=True,
        skipLint=True,
        noTag=True,
        verbosePypi=True,
    )
    assert projectDir == Path("sub")

    opts2, projectDir2 = parseArgs(["2.0.5"])
    assert opts2 == ReleaseOptions(bump=None, version=(2, 0, 5), verbosePypi=False)
    assert projectDir2 is None

    opts3, projectDir3 = parseArgs(["--dry-run", "3.1.4"])
    assert opts3 == ReleaseOptions(bump=None, version=(3, 1, 4), dryRun=True)
    assert projectDir3 is None

    assert parseArgs(["major"]) == (ReleaseOptions(bump="major"), None)
    assert parseArgs(["minor"]) == (ReleaseOptions(bump="minor"), None)
    assert parseArgs(["patch"]) == (ReleaseOptions(bump="patch"), None)
    assert parseArgs(["info"]) == (ReleaseOptions(info=True), None)
    assert parseArgs(["info", "--test", "--no-tag", "-C", "sub"]) == (
        ReleaseOptions(test=True, noTag=True, info=True),
        Path("sub"),
    )
    assert parseArgs(["resume"]) == (ReleaseOptions(resume=True), None)
    assert parseArgs(["resume", "--dry-run", "--no-tag", "-C", "sub"]) == (
        ReleaseOptions(dryRun=True, noTag=True, resume=True),
        Path("sub"),
    )

    for badArgv in ([], ["bugfix", "1.2.3"], ["invalid"], ["1.2"], ["--set-version", "2.0.5"], ["resume", "--test"]):
        with pytest.raises(SystemExit):
            parseArgs(badArgv)


def test_load_pypi_token_from_file_and_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Load PyPI API tokens from secrets.json or fall back to TWINE_PASSWORD."""
    secretsPath = tmp_path / "secrets.json"
    secretsPath.write_text(
        json.dumps({"pypi.org release key": "pypi-prod", "test.pypi.org release key": "pypi-test"}),
        encoding="utf-8",
    )
    assert loadPyPiToken(secretsPath, isTest=False, required=True) == ("pypi-prod", False)
    assert loadPyPiToken(secretsPath, isTest=True, required=True) == ("pypi-test", False)

    missingPath = tmp_path / "missing.json"
    monkeypatch.setenv("TWINE_PASSWORD", "pypi-from-env")
    assert loadPyPiToken(missingPath, isTest=False, required=True) == ("pypi-from-env", True)

    monkeypatch.delenv("TWINE_PASSWORD")
    warnings: list[str] = []
    assert loadPyPiToken(missingPath, isTest=False, required=False, onMessage=warnings.append) == (None, False)
    assert warnings
    with pytest.raises(ReleaseError, match="TWINE_PASSWORD"):
        loadPyPiToken(missingPath, isTest=False, required=True)


def test_resolve_secrets_path(tmp_path: Path) -> None:
    """Resolve relative, absolute, and tilde-prefixed secrets file paths."""
    assert resolveSecretsPath(tmp_path, Path("secrets.json")) == tmp_path / "secrets.json"
    assert resolveSecretsPath(tmp_path, Path("/etc/secrets.json")) == Path("/etc/secrets.json")
    assert resolveSecretsPath(tmp_path, Path("~/secrets.json")) == Path.home() / "secrets.json"


def test_in_project_root_checks_git_and_name(tmp_path: Path) -> None:
    """Verify git top-level and strict remote repository-name matching."""
    repo, _ = _createSampleRepo(tmp_path, name="demo", withRemote=False)
    assert inProjectRoot(repo, "demo") is True
    assert inProjectRoot(repo, "other") is False
    assert inProjectRoot(repo, None) is True

    sub = repo / "sub"
    sub.mkdir()
    assert inProjectRoot(sub, "demo") is False

    # Remotes must match the repository name segment (without .git), not owner/host/substring.
    _git(repo, "remote", "add", "origin", "https://github.com/demo/other.git")
    assert inProjectRoot(repo, "demo") is False
    assert inProjectRoot(repo, "other") is True

    _git(repo, "remote", "set-url", "origin", "https://demo.example.com/")
    assert inProjectRoot(repo, "demo") is False

    _git(repo, "remote", "set-url", "origin", "https://github.com/owner/demo-origin.git")
    assert inProjectRoot(repo, "demo") is False

    for matchingUrl in (
        "https://github.com/owner/demo.git",
        "https://github.com/owner/demo/",
        "git@github.com:owner/demo.git",
        "git@github.com:demo.git",
        "ssh://git@github.com:22/owner/demo.git",
        "/srv/repos/demo/.git",
    ):
        _git(repo, "remote", "set-url", "origin", matchingUrl)
        assert inProjectRoot(repo, "demo") is True, matchingUrl
        assert inProjectRoot(repo, "owner") is False, matchingUrl


def test_dry_run_builds_validates_and_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Dry runs build and validate artifacts while rolling back version and generated files."""
    pyproject = (
        '[project]\nname = "demo"\n\n'
        + "[tool.tablish]\n"
        + 'project-name = "demo"\n\n'
        + "[[tool.tablish.generated-files]]\n"
        + 'path = "generated/version.py"\n'
        + "template = '__version__ = \"{version}\"\\n'\n"
    )
    repo, _ = _createSampleRepo(tmp_path, pyprojectBody=pyproject)
    logPath = _installFakeTooling(monkeypatch, tmp_path)
    monkeypatch.delenv("TWINE_PASSWORD", raising=False)

    messages: list[str] = []
    result = createRelease(
        ReleaseOptions(bump="feature", dryRun=True),
        projectRoot=repo,
        onMessage=messages.append,
    )

    assert result.previousVersion == "1.2.3"
    assert result.newVersion == "1.3.0"
    assert result.tagName == "v1.3.0"
    assert result.dryRun is True
    assert len(result.builtFiles) == 2

    # Rolled back version & newly generated file, cleaned dist/
    assert (repo / "semver.txt").read_text(encoding="utf-8") == "1.2.3"
    assert not (repo / "generated" / "version.py").exists()
    assert not (repo / "dist").exists()

    calls = _readToolLog(logPath)
    assert [call["tool"] for call in calls] == ["build", "twine"]
    checkArgs = calls[1]["args"]
    assert isinstance(checkArgs, list)
    assert checkArgs[0] == "check"


def test_full_release_commits_tags_pushes_and_writes_generated_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full releases upload, commit version and generated files, tag, and push to origin."""
    pyproject = (
        '[project]\nname = "demo"\n\n'
        + "[tool.tablish]\n"
        + 'project-name = "demo"\n'
        + 'commit-message = "Release {version}"\n'
        + 'tag-format = "rel-{version}"\n'
        + 'tag-message = "Tagged {version}"\n\n'
        + "[[tool.tablish.generated-files]]\n"
        + 'path = "version_info.txt"\n'
        + 'command = ["git", "log", "-1", "--format=rev %h"]\n'
    )
    repo, _ = _createSampleRepo(tmp_path, pyprojectBody=pyproject)
    (repo / "secrets.json").write_text(json.dumps({"pypi.org release key": "pypi-live-token"}), encoding="utf-8")
    logPath = _installFakeTooling(monkeypatch, tmp_path)

    result = createRelease(ReleaseOptions(bump="bugfix"), projectRoot=repo)

    assert result.newVersion == "1.2.4"
    assert result.tagName == "rel-1.2.4"
    assert (repo / "semver.txt").read_text(encoding="utf-8") == "1.2.4"
    assert (repo / "version_info.txt").read_text(encoding="utf-8").startswith("rev ")
    assert _git(repo, "log", "-1", "--format=%s") == "Release 1.2.4"
    assert _git(repo, "tag", "-l") == "rel-1.2.4"
    assert "refs/tags/rel-1.2.4" in _git(repo, "ls-remote", "origin")

    calls = _readToolLog(logPath)
    assert [call["tool"] for call in calls] == ["build", "twine", "twine", "twine"]
    for uploadCall in calls[2:]:
        uploadArgs = uploadCall["args"]
        assert isinstance(uploadArgs, list)
        assert uploadArgs[0] == "upload"
        assert "--verbose" not in uploadArgs
        assert "--skip-existing" not in uploadArgs
        assert uploadCall["user"] == "__token__"
        assert uploadCall["auth"] == "pypi-live-token"


def test_verbose_pypi_passes_verbose_to_twine_upload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pass --verbose only to `twine upload` (not `twine check`) when verbosePypi is enabled."""
    repo, _ = _createSampleRepo(tmp_path)
    (repo / "secrets.json").write_text(
        json.dumps({"test.pypi.org release key": "pypi-test-token"}),
        encoding="utf-8",
    )
    logPath = _installFakeTooling(monkeypatch, tmp_path)

    assert main(["bugfix", "--test", "--verbose-pypi", "-C", str(repo)]) == 0

    calls = _readToolLog(logPath)
    assert [call["tool"] for call in calls] == ["build", "twine", "twine"]
    checkArgs = calls[1]["args"]
    uploadArgs = calls[2]["args"]
    assert isinstance(checkArgs, list)
    assert isinstance(uploadArgs, list)
    assert checkArgs[0] == "check"
    assert "--verbose" not in checkArgs
    assert uploadArgs[:2] == ["upload", "--verbose"]
    assert "--repository-url" in uploadArgs


def test_test_pypi_upload_rolls_back_and_skips_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test PyPI uploads target test-pypi-url and roll back without git commit or tag."""
    repo, _ = _createSampleRepo(tmp_path)
    (repo / "secrets.json").write_text(
        json.dumps({"test.pypi.org release key": "pypi-test-token"}),
        encoding="utf-8",
    )
    logPath = _installFakeTooling(monkeypatch, tmp_path)

    result = createRelease(ReleaseOptions(version=(2, 0, 0), test=True), projectRoot=repo)

    assert result.newVersion == "2.0.0"
    assert result.test is True
    assert result.tagName is None
    assert (repo / "semver.txt").read_text(encoding="utf-8") == "1.2.3"
    assert _git(repo, "tag", "-l") == ""

    calls = _readToolLog(logPath)
    uploadCall = calls[-1]
    assert uploadCall["tool"] == "twine"
    uploadArgs = uploadCall["args"]
    assert isinstance(uploadArgs, list)
    assert "--repository-url" in uploadArgs
    assert "https://test.pypi.org/legacy/" in uploadArgs


def test_skip_lint_runs_check_manifest_unless_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run check-manifest when lint is skipped unless check-manifest = false."""
    repo, _ = _createSampleRepo(tmp_path, withLintScript=False)
    logPath = _installFakeTooling(monkeypatch, tmp_path)
    monkeypatch.delenv("TWINE_PASSWORD", raising=False)

    createRelease(ReleaseOptions(bump="bugfix", dryRun=True, skipLint=True), projectRoot=repo)
    calls = _readToolLog(logPath)
    assert calls[0]["tool"] == "check_manifest"
    assert calls[0]["args"] == ["--no-build-isolation"]

    # Disable both lint and check-manifest in pyproject.toml
    logPath.unlink()
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "demo"\n\n[tool.tablish]\nlint-command = false\ncheck-manifest = false\n',
        encoding="utf-8",
    )
    createRelease(ReleaseOptions(bump="bugfix", dryRun=True), projectRoot=repo)
    calls2 = _readToolLog(logPath)
    assert [call["tool"] for call in calls2] == ["build", "twine"]


def test_dirty_tree_and_existing_tag_and_upload_failure_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject dirty trees, existing tags, and roll back version bump when upload fails."""
    repo, _ = _createSampleRepo(tmp_path)
    (repo / "secrets.json").write_text(json.dumps({"pypi.org release key": "pypi-live"}), encoding="utf-8")
    _installFakeTooling(monkeypatch, tmp_path, failUpload=True)

    # Dirty tree rejected unless --dry-run
    (repo / "untracked.txt").write_text("dirty", encoding="utf-8")
    with pytest.raises(ReleaseError, match="Working tree is dirty"):
        createRelease(ReleaseOptions(bump="bugfix"), projectRoot=repo)
    (repo / "untracked.txt").unlink()

    # Existing tag rejected
    _git(repo, "tag", "v1.2.4")
    with pytest.raises(ReleaseError, match="already exists"):
        createRelease(ReleaseOptions(bump="bugfix"), projectRoot=repo)
    _git(repo, "tag", "-d", "v1.2.4")

    # Failed upload rolls back semver.txt and suggests --verbose-pypi unless already enabled
    messages: list[str] = []
    with pytest.raises(subprocess.CalledProcessError):
        createRelease(ReleaseOptions(bump="bugfix"), projectRoot=repo, onMessage=messages.append)
    assert (repo / "semver.txt").read_text(encoding="utf-8") == "1.2.3"
    assert any("--verbose-pypi" in message for message in messages)

    verboseMessages: list[str] = []
    with pytest.raises(subprocess.CalledProcessError):
        createRelease(
            ReleaseOptions(bump="bugfix", verbosePypi=True),
            projectRoot=repo,
            onMessage=verboseMessages.append,
        )
    assert (repo / "semver.txt").read_text(encoding="utf-8") == "1.2.3"
    assert not any("--verbose-pypi" in message for message in verboseMessages)


def test_lint_dirtying_tree_and_no_tag_and_post_upload_git_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch dirty working trees after lint, honor --no-tag, and warn on post-upload git failures."""
    dirtyRepo, _ = _createSampleRepo(
        tmp_path,
        name="dirtydemo",
        lintBody="#!/bin/sh\necho changed > touched.txt\n",
    )
    (dirtyRepo / "secrets.json").write_text(json.dumps({"pypi.org release key": "pypi-live"}), encoding="utf-8")
    _installFakeTooling(monkeypatch, tmp_path)
    with pytest.raises(ReleaseError, match="modified the working tree"):
        createRelease(ReleaseOptions(bump="bugfix"), projectRoot=dirtyRepo)

    # --no-tag skips tag creation; missing remote triggers post-upload git warning
    noRemoteRepo, _ = _createSampleRepo(tmp_path, name="noremote", withRemote=False)
    (noRemoteRepo / "secrets.json").write_text(json.dumps({"pypi.org release key": "pypi-live"}), encoding="utf-8")
    with pytest.raises(ReleaseError, match="ALREADY uploaded"):
        createRelease(ReleaseOptions(bump="bugfix", noTag=True), projectRoot=noRemoteRepo)
    assert (noRemoteRepo / "semver.txt").read_text(encoding="utf-8") == "1.2.4"
    assert _git(noRemoteRepo, "tag", "-l") == ""


def test_cli_main_dry_run_and_error_exit_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify CLI main exit codes for successful dry runs and expected ReleaseError failures."""
    repo, _ = _createSampleRepo(tmp_path)
    _installFakeTooling(monkeypatch, tmp_path)
    monkeypatch.delenv("TWINE_PASSWORD", raising=False)

    assert main(["bugfix", "--dry-run", "-C", str(repo)]) == 0
    captured = capsys.readouterr()
    assert "Dry run succeeded" in captured.out

    assert main(["2.0.0", "--dry-run", "-C", str(repo)]) == 0
    capturedExplicit = capsys.readouterr()
    assert "1.2.3 -> 2.0.0" in capturedExplicit.out
    assert "Dry run succeeded" in capturedExplicit.out

    assert main(["minor", "--dry-run", "-C", str(repo)]) == 0
    capturedMinor = capsys.readouterr()
    assert "1.2.3 -> 1.3.0" in capturedMinor.out
    assert "Dry run succeeded" in capturedMinor.out

    emptyDir = tmp_path / "empty"
    emptyDir.mkdir()
    assert main(["bugfix", "--dry-run", "-C", str(emptyDir)]) == 1
    capturedErr = capsys.readouterr()
    assert "pyproject.toml" in capturedErr.err


def test_secrets_file_must_be_gitignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse to run when an in-repo secrets file is not gitignored or is tracked by git."""
    repo, _ = _createSampleRepo(tmp_path)
    _installFakeTooling(monkeypatch, tmp_path)
    monkeypatch.delenv("TWINE_PASSWORD", raising=False)

    # Remove secrets.json from .gitignore and commit that change
    (repo / ".gitignore").write_text("build/\ndist/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "Drop secrets.json from gitignore")

    # Missing secrets.json is allowed (e.g. when using TWINE_PASSWORD or --dry-run)
    createRelease(ReleaseOptions(bump="bugfix", dryRun=True), projectRoot=repo)

    # Unignored secrets.json in repo root fails even on --dry-run (before dirty-tree error)
    secretsPath = repo / "secrets.json"
    secretsPath.write_text(json.dumps({"pypi.org release key": "pypi-live"}), encoding="utf-8")
    assert isGitIgnored(repo, secretsPath) is False
    with pytest.raises(ReleaseError, match="not gitignored"):
        createRelease(ReleaseOptions(bump="bugfix", dryRun=True), projectRoot=repo)
    with pytest.raises(ReleaseError, match="not gitignored"):
        createRelease(ReleaseOptions(bump="bugfix"), projectRoot=repo)

    # Negated gitignore rule still fails
    (repo / ".gitignore").write_text("build/\ndist/\nsecrets.json\n!secrets.json\n", encoding="utf-8")
    assert isGitIgnored(repo, secretsPath) is False
    with pytest.raises(ReleaseError, match="not gitignored"):
        ensureSecretsGitIgnored(repo, secretsPath)

    # Symlink inside repo pointing to an external secrets file must still be gitignored
    externalSecrets = tmp_path / "external_secrets.json"
    externalSecrets.write_text(json.dumps({"pypi.org release key": "pypi-external"}), encoding="utf-8")
    secretsPath.unlink()
    secretsPath.symlink_to(externalSecrets)
    with pytest.raises(ReleaseError, match="not gitignored"):
        ensureSecretsGitIgnored(repo, secretsPath)
    secretsPath.unlink()

    # Properly gitignored secrets.json succeeds
    (repo / ".gitignore").write_text("secrets.json\nbuild/\ndist/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "Ignore secrets.json")
    secretsPath.write_text(json.dumps({"pypi.org release key": "pypi-live"}), encoding="utf-8")
    assert isGitIgnored(repo, secretsPath) is True
    ensureSecretsGitIgnored(repo, secretsPath)
    createRelease(ReleaseOptions(bump="bugfix", dryRun=True), projectRoot=repo)

    # Tracked secrets.json fails even if listed in .gitignore
    _git(repo, "add", "-f", "secrets.json")
    _git(repo, "commit", "-m", "Accidentally commit secrets")
    assert isGitIgnored(repo, secretsPath) is False
    with pytest.raises(ReleaseError, match="tracked by git"):
        createRelease(ReleaseOptions(bump="bugfix", dryRun=True), projectRoot=repo)
    _git(repo, "rm", "--cached", "secrets.json")
    _git(repo, "commit", "-m", "Untrack secrets.json")
    secretsPath.unlink()

    # External secrets-file outside the repository does not require gitignore
    pyproject = (
        '[project]\nname = "demo"\n\n'
        + "[tool.tablish]\n"
        + 'project-name = "demo"\n'
        + f'secrets-file = "{externalSecrets.as_posix()}"\n'
    )
    (repo / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    _git(repo, "add", "pyproject.toml")
    _git(repo, "commit", "-m", "Use external secrets file")
    createRelease(ReleaseOptions(bump="bugfix", dryRun=True), projectRoot=repo)

    # Nested in-repo secrets-file also requires gitignore
    nestedSecrets = repo / "config" / "secrets.json"
    nestedSecrets.parent.mkdir()
    nestedSecrets.write_text(json.dumps({"pypi.org release key": "pypi-nested"}), encoding="utf-8")
    (repo / ".gitignore").write_text("build/\ndist/\n", encoding="utf-8")
    with pytest.raises(ReleaseError, match="not gitignored"):
        ensureSecretsGitIgnored(repo, nestedSecrets)
    (repo / ".gitignore").write_text("build/\ndist/\nconfig/secrets.json\n", encoding="utf-8")
    ensureSecretsGitIgnored(repo, nestedSecrets)


class _FakeStream:
    """Minimal stream stub for testing supportsColor()."""

    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        """Return whether this fake stream reports as a TTY."""
        return self._tty


def test_supports_color_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Respect NO_COLOR, FORCE_COLOR, CLICOLOR, TERM=dumb, and TTY status."""
    for envVar in ("NO_COLOR", "FORCE_COLOR", "CLICOLOR_FORCE", "CLICOLOR", "TERM"):
        monkeypatch.delenv(envVar, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")

    assert supportsColor(_FakeStream(tty=True)) is True
    assert supportsColor(_FakeStream(tty=False)) is False

    monkeypatch.setenv("TERM", "dumb")
    assert supportsColor(_FakeStream(tty=True)) is False
    monkeypatch.setenv("TERM", "xterm-256color")

    monkeypatch.setenv("CLICOLOR", "0")
    assert supportsColor(_FakeStream(tty=True)) is False
    monkeypatch.delenv("CLICOLOR")

    monkeypatch.setenv("FORCE_COLOR", "1")
    assert supportsColor(_FakeStream(tty=False)) is True

    monkeypatch.setenv("NO_COLOR", "1")
    assert supportsColor(_FakeStream(tty=True)) is False
    assert supportsColor(_FakeStream(tty=False)) is False


def test_info_command_metadata_and_formatting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Inspect explicit, inferred, defaulted, and CLI-overridden metadata with and without color."""
    pyproject = (
        '[project]\nname = "inferred-pkg"\n\n'
        + "[tool.setuptools.dynamic]\n"
        + 'version = {file = "semver.txt"}\n\n'
        + "[tool.tablish]\n"
        + 'commit-message = "Ship {version}"\n\n'
        + "[[tool.tablish.generated-files]]\n"
        + 'path = "pkg/_version.py"\n'
        + "template = '__version__ = \"{version}\"'\n"
    )
    repo, _ = _createSampleRepo(tmp_path, name="inferred-pkg", pyprojectBody=pyproject)
    (repo / "secrets.json").write_text(
        json.dumps({"pypi.org release key": "pypi-secret-do-not-print"}),
        encoding="utf-8",
    )
    _installFakeTooling(monkeypatch, tmp_path)
    monkeypatch.delenv("TWINE_PASSWORD", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")

    metadata = collectReleaseMetadata(
        ReleaseOptions(info=True, noTag=True, verbosePypi=True),
        projectRoot=repo,
    )
    byKey = {item.key: item for item in metadata.items}

    assert byKey["project-name"].value == "inferred-pkg"
    assert byKey["project-name"].source == "inferred"
    assert byKey["project-name"].sourceDetail == "[project].name"

    assert byKey["semver-file"].value == "semver.txt"
    assert byKey["semver-file"].source == "inferred"
    assert byKey["semver-file"].sourceDetail == "[tool.setuptools.dynamic].version.file"

    assert byKey["current-version"].value == "1.2.3"
    assert byKey["commit-message"].value == "Ship {version}"
    assert byKey["commit-message"].source == "explicit"
    assert byKey["tag-format"].source == "default"
    assert byKey["tag"].value == "false"
    assert byKey["tag"].source == "cli"
    assert byKey["tag"].sourceDetail == "--no-tag"
    assert byKey["verbose-pypi"].value == "true"
    assert byKey["verbose-pypi"].source == "cli"
    assert byKey["secrets-file"].status == "ok"
    assert byKey["auth-token"].status == "ok"
    assert byKey["generated-files"].source == "explicit"
    assert byKey["generated-files"].details == ("pkg/_version.py (template: '__version__ = \"{version}\"')",)

    plainText = formatReleaseMetadata(metadata, useColor=False)
    assert "\033[" not in plainText
    assert "[explicit]" in plainText
    assert "[default]" in plainText
    assert "[default: [project].name]" in plainText
    assert "[cli: --no-tag]" in plainText
    assert "pypi-secret-do-not-print" not in plainText

    coloredText = formatReleaseMetadata(metadata, useColor=True)
    assert "\033[" in coloredText
    assert "pypi-secret-do-not-print" not in coloredText

    assert main(["info", "--test", "--skip-lint", "-C", str(repo)]) == 0
    captured = capsys.readouterr()
    assert "\033[" not in captured.out
    assert "test.pypi.org" in captured.out
    assert "[cli: --test]" in captured.out
    assert "[cli: --skip-lint]" in captured.out
    assert "pypi-secret-do-not-print" not in captured.out


def test_resume_after_git_push_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Resume cleanly after a post-upload git push failure without re-uploading or re-committing."""
    repo, _ = _createSampleRepo(tmp_path, name="pushfail", withRemote=False)
    (repo / "secrets.json").write_text(json.dumps({"pypi.org release key": "pypi-live"}), encoding="utf-8")
    logPath = _installFakeTooling(monkeypatch, tmp_path)

    # First release uploads, commits, tags, and fails on `git push` (no remote yet)
    with pytest.raises(ReleaseError, match=r"(?s)ALREADY uploaded.*tablish resume") as excInfo:
        createRelease(ReleaseOptions(bump="bugfix"), projectRoot=repo)
    assert "tablish resume" in str(excInfo.value)
    assert loadPendingRelease(repo) is not None
    assert _git(repo, "tag", "-l") == "v1.2.4"

    # Starting a new release while one is pending is blocked
    with pytest.raises(ReleaseError, match="tablish resume"):
        createRelease(ReleaseOptions(bump="bugfix"), projectRoot=repo)

    # `tablish info` reports the pending resume
    infoMeta = collectReleaseMetadata(ReleaseOptions(info=True), projectRoot=repo)
    infoByKey = {item.key: item for item in infoMeta.items}
    assert infoByKey["pending-resume"].status == "warning"
    assert "1.2.4" in infoByKey["pending-resume"].value

    # `tablish resume --dry-run` reports skipping commit/tag and preserves pending state
    dryMessages: list[str] = []
    dryResult = resumeRelease(ReleaseOptions(resume=True, dryRun=True), projectRoot=repo, onMessage=dryMessages.append)
    assert dryResult.dryRun is True
    assert dryResult.newVersion == "1.2.4"
    assert any("already committed" in msg for msg in dryMessages)
    assert any("already exists on HEAD" in msg for msg in dryMessages)
    assert loadPendingRelease(repo) is not None

    # Attach remote and finish via CLI `tablish resume`
    _attachBareOrigin(tmp_path, repo, "pushfail")

    callsBeforeResume = len(_readToolLog(logPath))
    assert main(["resume", "-C", str(repo)]) == 0
    captured = capsys.readouterr()
    assert "Successfully finished releasing 1.2.4!" in captured.out
    assert len(_readToolLog(logPath)) == callsBeforeResume  # No re-build or re-upload
    assert loadPendingRelease(repo) is None
    assert "refs/tags/v1.2.4" in _git(repo, "ls-remote", "origin")

    # Resuming again when nothing is pending raises ReleaseError
    with pytest.raises(ReleaseError, match="No pending release to resume"):
        resumeRelease(ReleaseOptions(resume=True), projectRoot=repo)


def test_resume_after_git_commit_failure_and_multi_step_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resume across commit failure (pre-commit hook) followed by push failure and final success."""
    pyproject = (
        '[project]\nname = "hookdemo"\n\n'
        + "[tool.tablish]\n"
        + 'project-name = "hookdemo"\n\n'
        + "[[tool.tablish.generated-files]]\n"
        + 'path = "generated/version.txt"\n'
        + 'template = "{version}"\n'
    )
    repo, _ = _createSampleRepo(tmp_path, name="hookdemo", pyprojectBody=pyproject, withRemote=False)
    (repo / "secrets.json").write_text(json.dumps({"pypi.org release key": "pypi-live"}), encoding="utf-8")
    logPath = _installFakeTooling(monkeypatch, tmp_path)

    # Install a failing pre-commit hook so git commit fails right after upload
    hookPath = repo / ".git" / "hooks" / "pre-commit"
    hookPath.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hookPath.chmod(0o755)

    with pytest.raises(ReleaseError, match="tablish resume"):
        createRelease(ReleaseOptions(bump="feature"), projectRoot=repo)
    assert (repo / "semver.txt").read_text(encoding="utf-8") == "1.3.0"
    assert (repo / "generated" / "version.txt").read_text(encoding="utf-8") == "1.3.0"
    assert _git(repo, "tag", "-l") == ""
    assert loadPendingRelease(repo) is not None

    # Fix pre-commit hook; first resume commits & tags, then fails at push (still no remote)
    hookPath.unlink()
    with pytest.raises(ReleaseError, match="tablish resume"):
        resumeRelease(ReleaseOptions(resume=True), projectRoot=repo)
    assert _git(repo, "log", "-1", "--format=%s") == "Bump semver to 1.3.0"
    assert _git(repo, "tag", "-l") == "v1.3.0"
    assert loadPendingRelease(repo) is not None

    # If tag is moved to an older commit, resume refuses instead of pushing a stale tag
    initialSha = _git(repo, "rev-parse", "HEAD~1")
    _git(repo, "tag", "-f", "-a", "v1.3.0", "-m", "Wrong commit", initialSha)
    with pytest.raises(ReleaseError, match="points to"):
        resumeRelease(ReleaseOptions(resume=True), projectRoot=repo)
    _git(repo, "tag", "-d", "v1.3.0")

    # Attach remote and finish; resume recreates v1.3.0 on HEAD and pushes
    _attachBareOrigin(tmp_path, repo, "hookdemo")

    callsBeforeResume = len(_readToolLog(logPath))
    result = resumeRelease(ReleaseOptions(resume=True), projectRoot=repo)
    assert result.newVersion == "1.3.0"
    assert result.tagName == "v1.3.0"
    assert len(_readToolLog(logPath)) == callsBeforeResume
    assert loadPendingRelease(repo) is None
    assert "refs/tags/v1.3.0" in _git(repo, "ls-remote", "origin")


def test_resume_after_partial_twine_upload_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preserve dist/ and resume remaining files with --skip-existing when twine fails mid-upload."""
    repo, _ = _createSampleRepo(tmp_path, name="partialdemo")
    (repo / "secrets.json").write_text(json.dumps({"pypi.org release key": "pypi-live"}), encoding="utf-8")
    # Wheel uploads first (sorted order); fail on the second file (.tar.gz)
    logPath = _installFakeTooling(monkeypatch, tmp_path, failUploadSuffix=".tar.gz")

    with pytest.raises(ReleaseError, match=r"(?s)PARTIALLY uploaded.*tablish resume"):
        createRelease(ReleaseOptions(bump="bugfix"), projectRoot=repo)

    # Version bump and dist/ must be preserved, and pending state must record stage='upload'
    assert (repo / "semver.txt").read_text(encoding="utf-8") == "1.2.4"
    assert (repo / "dist" / "demo-1.2.4.tar.gz").is_file()
    pending = loadPendingRelease(repo)
    assert pending is not None
    assert pending.stage == "upload"
    assert pending.uploadedFiles == ("dist/demo-1.2.4-py3-none-any.whl",)
    assert pending.remainingFiles == ("dist/demo-1.2.4.tar.gz",)

    # Starting a new release is blocked while partial upload is pending
    with pytest.raises(ReleaseError, match="partially uploaded"):
        createRelease(ReleaseOptions(bump="bugfix"), projectRoot=repo)

    # `tablish info` reports the partial upload pending resume
    infoMeta = collectReleaseMetadata(ReleaseOptions(info=True), projectRoot=repo)
    infoByKey = {item.key: item for item in infoMeta.items}
    assert infoByKey["pending-resume"].status == "warning"
    assert "partially uploaded" in infoByKey["pending-resume"].value

    # `tablish resume --dry-run` previews remaining upload without mutating state
    dryMessages: list[str] = []
    dryResult = resumeRelease(ReleaseOptions(resume=True, dryRun=True), projectRoot=repo, onMessage=dryMessages.append)
    assert dryResult.dryRun is True
    assert any("--skip-existing" in msg and "demo-1.2.4.tar.gz" in msg for msg in dryMessages)
    assert loadPendingRelease(repo) is not None

    # Failing again on resume suggests `tablish resume --verbose-pypi` and keeps state
    retryMessages: list[str] = []
    with pytest.raises(ReleaseError, match="tablish resume"):
        resumeRelease(ReleaseOptions(resume=True), projectRoot=repo, onMessage=retryMessages.append)
    assert any("tablish resume --verbose-pypi" in msg for msg in retryMessages)
    assert loadPendingRelease(repo) is not None

    # Fix upload issue and resume with --verbose-pypi: uploads only remaining file with --skip-existing, cleans dist/, commits/tags/pushes
    _installFakeTooling(monkeypatch, tmp_path, recordLog=logPath)
    callsBeforeResume = len(_readToolLog(logPath))
    result = resumeRelease(ReleaseOptions(resume=True, verbosePypi=True), projectRoot=repo)

    assert result.newVersion == "1.2.4"
    assert result.tagName == "v1.2.4"
    assert result.builtFiles == (Path("dist/demo-1.2.4.tar.gz"),)
    assert not (repo / "dist").exists()
    assert loadPendingRelease(repo) is None
    assert _git(repo, "log", "-1", "--format=%s") == "Bump semver to 1.2.4"
    assert "refs/tags/v1.2.4" in _git(repo, "ls-remote", "origin")

    resumeCalls = _readToolLog(logPath)[callsBeforeResume:]
    assert [call["tool"] for call in resumeCalls] == ["twine"]
    resumeArgs = resumeCalls[0]["args"]
    assert isinstance(resumeArgs, list)
    assert resumeArgs[:3] == ["upload", "--verbose", "--skip-existing"]
    assert resumeArgs[-1].endswith("demo-1.2.4.tar.gz")


def test_version_write_rollback_and_post_upload_cleanup_failure_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Roll back earlier generated files if writing semver fails, and save git resume state before cleanup."""
    pyproject = (
        '[project]\nname = "windowdemo"\n\n'
        + "[tool.tablish]\n"
        + 'project-name = "windowdemo"\n\n'
        + "[[tool.tablish.generated-files]]\n"
        + 'path = "generated/version.txt"\n'
        + 'template = "{version}"\n'
    )
    repo, _ = _createSampleRepo(tmp_path, name="windowdemo", pyprojectBody=pyproject)
    (repo / "secrets.json").write_text(json.dumps({"pypi.org release key": "pypi-live"}), encoding="utf-8")
    _installFakeTooling(monkeypatch, tmp_path)

    # Simulate failure when writing semver.txt after generated/version.txt was already written
    realWriteText = Path.write_text
    semverPath = (repo / "semver.txt").resolve()

    def _failSemverWrite(
        self: Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        if self.resolve() == semverPath and data == "1.2.4":
            msg = "simulated disk error"
            raise OSError(msg)
        return realWriteText(self, data, encoding=encoding, errors=errors, newline=newline)

    monkeypatch.setattr(Path, "write_text", _failSemverWrite)
    with pytest.raises(OSError, match="simulated disk error"):
        createRelease(ReleaseOptions(bump="bugfix"), projectRoot=repo)
    monkeypatch.setattr(Path, "write_text", realWriteText)

    assert (repo / "semver.txt").read_text(encoding="utf-8") == "1.2.3"
    assert not (repo / "generated" / "version.txt").exists()
    assert loadPendingRelease(repo) is None

    # Simulate failure in cleanBuildArtifacts right after upload succeeds: resume state must already be saved!
    realClean = releaseMod.cleanBuildArtifacts
    cleanCalls = 0

    def _failSecondClean(projectRoot: Path, buildDirs: t.Sequence[str]) -> None:
        nonlocal cleanCalls
        cleanCalls += 1
        if cleanCalls == 2:
            msg = "simulated cleanup failure"
            raise OSError(msg)
        realClean(projectRoot, buildDirs)

    monkeypatch.setattr(releaseMod, "cleanBuildArtifacts", _failSecondClean)
    with pytest.raises(OSError, match="simulated cleanup failure"):
        createRelease(ReleaseOptions(bump="bugfix"), projectRoot=repo)
    monkeypatch.setattr(releaseMod, "cleanBuildArtifacts", realClean)

    pending = loadPendingRelease(repo)
    assert pending is not None
    assert pending.stage == "git"
    assert (repo / "dist").exists()

    # Resuming cleans leftover dist/ and finishes git commit/tag/push
    result = resumeRelease(ReleaseOptions(resume=True), projectRoot=repo)
    assert result.newVersion == "1.2.4"
    assert not (repo / "dist").exists()
    assert loadPendingRelease(repo) is None
    assert "refs/tags/v1.2.4" in _git(repo, "ls-remote", "origin")


def test_tablish_kdl_overrides_pyproject_in_info_and_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify tablish.kdl overrides pyproject.toml in both `tablish info` and full releases."""
    pyproject = (
        '[project]\nname = "kdldemo"\n\n'
        + "[tool.setuptools.dynamic]\n"
        + 'version = {file = "semver.txt"}\n\n'
        + "[tool.tablish]\n"
        + 'commit-message = "From TOML {version}"\n'
        + 'tag-message = "TOML tag {version}"\n'
    )
    repo, _ = _createSampleRepo(tmp_path, name="kdldemo", pyprojectBody=pyproject)
    (repo / "tablish.kdl").write_text(
        'commit-message "From KDL {version}"\n'
        + 'tag-format "kdl-{version}"\n'
        + "generated-files {\n"
        + '    "generated/version.py" template=r#"__version__ = "{version}""#\n'
        + "}\n",
        encoding="utf-8",
    )
    _git(repo, "add", "tablish.kdl")
    _git(repo, "commit", "-m", "Add tablish.kdl")
    (repo / "secrets.json").write_text(json.dumps({"pypi.org release key": "pypi-live"}), encoding="utf-8")
    _installFakeTooling(monkeypatch, tmp_path)

    metadata = collectReleaseMetadata(ReleaseOptions(info=True), projectRoot=repo)
    byKey = {item.key: item for item in metadata.items}
    assert byKey["pyproject.toml"].value == "[tool.tablish] present"
    assert byKey["tablish.kdl"].value == "present"
    assert byKey["tablish.kdl"].source == "explicit"
    assert byKey["commit-message"].value == "From KDL {version}"
    assert byKey["commit-message"].source == "explicit"
    assert byKey["commit-message"].sourceDetail == "tablish.kdl"
    assert byKey["tag-message"].value == "TOML tag {version}"
    assert byKey["tag-message"].source == "explicit"
    assert byKey["tag-message"].sourceDetail == "pyproject.toml"
    assert byKey["project-name"].source == "inferred"
    assert byKey["semver-file"].source == "inferred"

    plainText = formatReleaseMetadata(metadata, useColor=False)
    assert "[explicit: tablish.kdl]" in plainText
    assert "[explicit: pyproject.toml]" in plainText

    result = createRelease(ReleaseOptions(bump="bugfix"), projectRoot=repo)
    assert result.newVersion == "1.2.4"
    assert result.tagName == "kdl-1.2.4"
    assert _git(repo, "log", "-1", "--format=%s") == "From KDL 1.2.4"
    assert _git(repo, "tag", "-l") == "kdl-1.2.4"
    assert (repo / "generated" / "version.py").read_text(encoding="utf-8") == '__version__ = "1.2.4"'


def test_standalone_tablish_kdl_without_pyproject(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Allow projects configured purely with tablish.kdl (no pyproject.toml) to inspect and dry-run."""
    repo, _ = _createSampleRepo(tmp_path, name="kdlonly", withRemote=False)
    (repo / "pyproject.toml").unlink()
    (repo / "tablish.kdl").write_text('project-name "kdlonly"\nlint-command false\n', encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "Switch to standalone tablish.kdl")
    _installFakeTooling(monkeypatch, tmp_path)
    monkeypatch.delenv("TWINE_PASSWORD", raising=False)

    metadata = collectReleaseMetadata(ReleaseOptions(info=True), projectRoot=repo)
    byKey = {item.key: item for item in metadata.items}
    assert byKey["pyproject.toml"].value == "not found"
    assert byKey["pyproject.toml"].source == "default"
    assert byKey["tablish.kdl"].value == "present"
    assert byKey["tablish.kdl"].source == "explicit"
    assert byKey["project-name"].value == "kdlonly"
    assert byKey["project-name"].sourceDetail == "tablish.kdl"

    result = createRelease(ReleaseOptions(bump="feature", dryRun=True), projectRoot=repo)
    assert result.newVersion == "1.3.0"
    assert result.dryRun is True


def test_load_pending_release_validation_errors(tmp_path: Path) -> None:
    """Reject malformed pending-release state files with specific field diagnostics."""
    repo, _ = _createSampleRepo(tmp_path, name="badpending", withRemote=False)
    pendingPath = repo / ".git" / PENDING_RELEASE_FILENAME

    validPayload: dict[str, object] = {
        "stage": "git",
        "previousVersion": "1.2.3",
        "newVersion": "1.2.4",
        "targetRepo": "pypi.org",
        "repositoryUrl": None,
        "commitFiles": ["semver.txt"],
        "commitMessage": "Bump semver to 1.2.4",
        "tagName": "v1.2.4",
        "tagMessage": "Release 1.2.4",
        "uploadedFiles": ["dist/demo-1.2.4.tar.gz"],
        "remainingFiles": [],
    }

    pendingPath.write_text("{not json", encoding="utf-8")
    with pytest.raises(ReleaseError, match="Invalid pending release state"):
        loadPendingRelease(repo)

    pendingPath.write_text("[]", encoding="utf-8")
    with pytest.raises(ReleaseError, match="expected a JSON object"):
        loadPendingRelease(repo)

    cases: list[tuple[dict[str, object], str]] = [
        ({**validPayload, "stage": "bogus"}, "'stage'"),
        ({**validPayload, "newVersion": "   "}, "'newVersion'"),
        ({**validPayload, "commitFiles": []}, "'commitFiles'"),
        ({**validPayload, "tagName": "v1.2.4", "tagMessage": None}, "'tagMessage'"),
        ({**validPayload, "stage": "upload", "remainingFiles": []}, "'remainingFiles'"),
    ]
    for badPayload, expectedMatch in cases:
        pendingPath.write_text(json.dumps(badPayload), encoding="utf-8")
        with pytest.raises(ReleaseError, match=expectedMatch):
            loadPendingRelease(repo)


def test_check_manifest_only_required_when_lint_skipped_or_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Require check-manifest tool only when lint is skipped or disabled and check-manifest is enabled."""
    repo, _ = _createSampleRepo(tmp_path, name="nomanifest")
    _installFakeTooling(monkeypatch, tmp_path, missingTools=("check_manifest",))
    monkeypatch.delenv("TWINE_PASSWORD", raising=False)

    # Lint is enabled and not skipped -> check-manifest is not required for release or info.
    result = createRelease(ReleaseOptions(bump="bugfix", dryRun=True), projectRoot=repo)
    assert result.dryRun is True
    infoWithLint = {
        item.key: item for item in collectReleaseMetadata(ReleaseOptions(info=True), projectRoot=repo).items
    }
    assert infoWithLint["tools"].status == "ok"
    assert "check-manifest" not in infoWithLint["tools"].value

    # Passing --skip-lint requires check-manifest.
    with pytest.raises(ReleaseError, match="check-manifest"):
        createRelease(ReleaseOptions(bump="bugfix", dryRun=True, skipLint=True), projectRoot=repo)
    infoSkipped = {
        item.key: item
        for item in collectReleaseMetadata(ReleaseOptions(info=True, skipLint=True), projectRoot=repo).items
    }
    assert infoSkipped["tools"].status == "error"
    assert "check-manifest: MISSING" in infoSkipped["tools"].value

    # Disabling lint-command in config also requires check-manifest unless check-manifest = false.
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "nomanifest"\n\n[tool.tablish]\nlint-command = false\n',
        encoding="utf-8",
    )
    with pytest.raises(ReleaseError, match="check-manifest"):
        createRelease(ReleaseOptions(bump="bugfix", dryRun=True), projectRoot=repo)
