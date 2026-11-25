"""Tests for tablish release workflow, helpers, and CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tablish import (
    ReleaseError,
    ReleaseOptions,
    bumpSemver,
    collectReleaseMetadata,
    createRelease,
    ensureSecretsGitIgnored,
    formatReleaseMetadata,
    inProjectRoot,
    isGitIgnored,
    loadPyPiToken,
    main,
    parseArgs,
    parseSemver,
    release as releaseMod,
    resolveSecretsPath,
    supportsColor,
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

    remoteRepo: Path | None = None
    if withRemote:
        remoteRepo = tmpPath / f"{name}-origin.git"
        subprocess.run(["git", "init", "--bare", str(remoteRepo)], check=True, capture_output=True)
        _git(repo, "remote", "add", "origin", str(remoteRepo))
        branch = _git(repo, "branch", "--show-current")
        _git(repo, "push", "-u", "origin", branch)

    return (repo, remoteRepo)


def _installFakeTooling(
    monkeypatch: pytest.MonkeyPatch,
    tmpPath: Path,
    *,
    failUpload: bool = False,
    recordLog: Path | None = None,
) -> Path:
    """Monkeypatch findToolCommand to invoke lightweight local Python scripts."""
    logPath = recordLog or (tmpPath / "tool_calls.jsonl")

    fakeBuild = tmpPath / "fake_build.py"
    fakeBuild.write_text(
        "import json, pathlib, sys\n"
        + f"log = pathlib.Path({str(logPath)!r})\n"
        + 'with log.open("a", encoding="utf-8") as fh:\n'
        + '    fh.write(json.dumps({"tool": "build", "args": sys.argv[1:]}) + "\\n")\n'
        + 'ver = pathlib.Path("semver.txt").read_text(encoding="utf-8").strip()\n'
        + 'dist = pathlib.Path("dist")\n'
        + "dist.mkdir(exist_ok=True)\n"
        + '(dist / f"demo-{ver}.tar.gz").write_text("sdist", encoding="utf-8")\n'
        + '(dist / f"demo-{ver}-py3-none-any.whl").write_text("wheel", encoding="utf-8")\n',
        encoding="utf-8",
    )

    fakeTwine = tmpPath / "fake_twine.py"
    fakeTwine.write_text(
        "import json, os, pathlib, sys\n"
        + f"log = pathlib.Path({str(logPath)!r})\n"
        + 'with log.open("a", encoding="utf-8") as fh:\n'
        + '    fh.write(json.dumps({"tool": "twine", "args": sys.argv[1:], '
        + '"user": os.environ.get("TWINE_USERNAME"), "auth": os.environ.get("TWINE_PASSWORD")}) + "\\n")\n'
        + f'if {failUpload!r} and sys.argv[1:2] == ["upload"]:\n'
        + "    sys.exit(2)\n",
        encoding="utf-8",
    )

    fakeCheckManifest = tmpPath / "fake_check_manifest.py"
    fakeCheckManifest.write_text(
        "import json, pathlib, sys\n"
        + f"log = pathlib.Path({str(logPath)!r})\n"
        + 'with log.open("a", encoding="utf-8") as fh:\n'
        + '    fh.write(json.dumps({"tool": "check_manifest", "args": sys.argv[1:]}) + "\\n")\n',
        encoding="utf-8",
    )

    toolMap: dict[str, list[str]] = {
        "build": [sys.executable, str(fakeBuild)],
        "twine": [sys.executable, str(fakeTwine)],
        "check_manifest": [sys.executable, str(fakeCheckManifest)],
    }

    def _fakeFindTool(moduleName: str, _executableName: str) -> list[str] | None:
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

    for badArgv in ([], ["bugfix", "1.2.3"], ["invalid"], ["1.2"], ["--set-version", "2.0.5"]):
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
    """Verify git top-level and optional project-name matching."""
    repo, _ = _createSampleRepo(tmp_path, name="demo", withRemote=False)
    assert inProjectRoot(repo, "demo") is True
    assert inProjectRoot(repo, "other") is False
    assert inProjectRoot(repo, None) is True

    sub = repo / "sub"
    sub.mkdir()
    assert inProjectRoot(sub, "demo") is False


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
    assert calls[1]["args"][0] == "check"  # type: ignore[index]


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
    assert [call["tool"] for call in calls] == ["build", "twine", "twine"]
    uploadArgs = calls[2]["args"]
    assert isinstance(uploadArgs, list)
    assert uploadArgs[0] == "upload"
    assert "--verbose" not in uploadArgs
    assert calls[2]["user"] == "__token__"
    assert calls[2]["auth"] == "pypi-live-token"


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
