"""Tests for tablish pyproject.toml configuration loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from tablish import (
    DEFAULT_BUILD_DIRS,
    DEFAULT_CHECK_MANIFEST_ARGS,
    DEFAULT_COMMIT_MESSAGE,
    DEFAULT_PYPI_SECRET_KEY,
    DEFAULT_SECRETS_FILE,
    DEFAULT_SEMVER_FILE,
    DEFAULT_TAG_FORMAT,
    DEFAULT_TAG_MESSAGE,
    DEFAULT_TEST_PYPI_SECRET_KEY,
    DEFAULT_TEST_PYPI_URL,
    GeneratedFileConfig,
    ReleaseError,
    loadConfig,
    parseConfigData,
)


def test_parse_defaults_empty_pyproject() -> None:
    """Verify default configuration values when pyproject.toml has no [tool.tablish] table."""
    cfg = parseConfigData({})
    assert cfg.projectName is None
    assert cfg.semverFile == Path(DEFAULT_SEMVER_FILE)
    assert cfg.secretsFile == Path(DEFAULT_SECRETS_FILE)
    assert cfg.pypiSecretKey == DEFAULT_PYPI_SECRET_KEY
    assert cfg.testPypiSecretKey == DEFAULT_TEST_PYPI_SECRET_KEY
    assert cfg.pypiUrl is None
    assert cfg.testPypiUrl == DEFAULT_TEST_PYPI_URL
    assert cfg.buildDirs == DEFAULT_BUILD_DIRS
    assert cfg.lintEnabled is True
    assert cfg.lintCommand is None
    assert cfg.checkManifest is True
    assert cfg.checkManifestArgs == DEFAULT_CHECK_MANIFEST_ARGS
    assert cfg.tag is True
    assert cfg.tagFormat == DEFAULT_TAG_FORMAT
    assert cfg.tagMessage == DEFAULT_TAG_MESSAGE
    assert cfg.commitMessage == DEFAULT_COMMIT_MESSAGE
    assert not cfg.generatedFiles
    assert cfg.hasTablishTable is False
    assert not cfg.explicitKeys
    assert not cfg.inferredSources
    assert cfg.sourceOf("semver-file") == "default"
    assert cfg.inferredOrigin("semver-file") is None


def test_infer_project_name_and_setuptools_semver_file() -> None:
    """Infer projectName from [project].name and semverFile from setuptools dynamic version."""
    cfg = parseConfigData(
        {
            "project": {"name": "ao3py"},
            "tool": {
                "setuptools": {
                    "dynamic": {
                        "version": {"file": "pkg/semver.txt"},
                    },
                },
            },
        },
    )
    assert cfg.projectName == "ao3py"
    assert cfg.semverFile == Path("pkg/semver.txt")
    assert cfg.sourceOf("project-name") == "inferred"
    assert cfg.inferredOrigin("project-name") == "[project].name"
    assert cfg.sourceOf("semver-file") == "inferred"
    assert cfg.inferredOrigin("semver-file") == "[tool.setuptools.dynamic].version.file"
    assert cfg.sourceOf("secrets-file") == "default"


def test_infer_setuptools_semver_file_single_element_list() -> None:
    """Infer semverFile when setuptools dynamic version.file is a single-element list."""
    cfg = parseConfigData(
        {
            "tool": {
                "setuptools": {
                    "dynamic": {
                        "version": {"file": ["nested/semver.txt"]},
                    },
                },
            },
        },
    )
    assert cfg.semverFile == Path("nested/semver.txt")


def test_explicit_tablish_keys_override_inferred_values() -> None:
    """Verify all explicit [tool.tablish] keys parse and override defaults."""
    cfg = parseConfigData(
        {
            "project": {"name": "kdl-py"},
            "tool": {
                "setuptools": {"dynamic": {"version": {"file": "ignored.txt"}}},
                "tablish": {
                    "project-name": "kdlpy",
                    "semver-file": "bikeshed/semver.txt",
                    "secrets-file": "~/.config/tablish/secrets.json",
                    "pypi-secret-key": "prod",
                    "test-pypi-secret-key": "staging",
                    "pypi-url": "https://upload.example.com/legacy/",
                    "test-pypi-url": "https://test.example.com/legacy/",
                    "build-dirs": ["build", "dist", "out"],
                    "lint-command": ["pytest", "-q"],
                    "check-manifest": False,
                    "check-manifest-args": [],
                    "tag": False,
                    "tag-format": "release-{version}",
                    "tag-message": "Ship {version}",
                    "commit-message": "Release v{version}",
                    "generated-files": [
                        {
                            "path": "pkg/info.txt",
                            "command": ["git", "log", "-1", "--format=%h"],
                        },
                        {
                            "path": "pkg/_version.py",
                            "template": '__version__ = "{version}"\n',
                        },
                    ],
                },
            },
        },
    )
    assert cfg.projectName == "kdlpy"
    assert cfg.semverFile == Path("bikeshed/semver.txt")
    assert cfg.secretsFile == Path("~/.config/tablish/secrets.json")
    assert cfg.pypiSecretKey == "prod"
    assert cfg.testPypiSecretKey == "staging"
    assert cfg.pypiUrl == "https://upload.example.com/legacy/"
    assert cfg.testPypiUrl == "https://test.example.com/legacy/"
    assert cfg.buildDirs == ("build", "dist", "out")
    assert cfg.lintEnabled is True
    assert cfg.lintCommand == ("pytest", "-q")
    assert cfg.checkManifest is False
    assert not cfg.checkManifestArgs
    assert cfg.tag is False
    assert cfg.tagFormat == "release-{version}"
    assert cfg.tagMessage == "Ship {version}"
    assert cfg.commitMessage == "Release v{version}"
    assert cfg.generatedFiles == (
        GeneratedFileConfig(path=Path("pkg/info.txt"), command=("git", "log", "-1", "--format=%h")),
        GeneratedFileConfig(path=Path("pkg/_version.py"), template='__version__ = "{version}"\n'),
    )
    assert cfg.hasTablishTable is True
    assert cfg.sourceOf("project-name") == "explicit"
    assert cfg.inferredOrigin("project-name") is None
    assert cfg.sourceOf("semver-file") == "explicit"
    assert cfg.sourceOf("generated-files") == "explicit"


def test_empty_project_name_disables_name_check() -> None:
    """Explicit empty project-name disables remote/directory name matching."""
    cfg = parseConfigData({"project": {"name": "ao3py"}, "tool": {"tablish": {"project-name": ""}}})
    assert cfg.projectName is None


@pytest.mark.parametrize(
    ("rawLint", "expectedEnabled", "expectedCmd"),
    [
        (False, False, None),
        (True, True, None),
        ("", False, None),
        ([], False, None),
        ("sh ./lint", True, ("sh", "./lint")),
        (["sh", "lint"], True, ("sh", "lint")),
    ],
)
def test_lint_command_variants(
    rawLint: object,
    expectedEnabled: bool,
    expectedCmd: tuple[str, ...] | None,
) -> None:
    """Verify lint-command accepts booleans, empty values, shell strings, and argv lists."""
    cfg = parseConfigData({"tool": {"tablish": {"lint-command": rawLint}}})
    assert cfg.lintEnabled is expectedEnabled
    assert cfg.lintCommand == expectedCmd


def test_unknown_config_key_raises() -> None:
    """Reject typos and unrecognized keys in [tool.tablish]."""
    with pytest.raises(ReleaseError, match="Unknown key"):
        parseConfigData({"tool": {"tablish": {"semver_file": "semver.txt"}}})


def test_invalid_relative_paths_rejected() -> None:
    """Reject parent-traversal and absolute paths for in-repo config paths."""
    with pytest.raises(ReleaseError, match="relative path"):
        parseConfigData({"tool": {"tablish": {"semver-file": "../semver.txt"}}})
    with pytest.raises(ReleaseError, match="relative path"):
        parseConfigData({"tool": {"tablish": {"build-dirs": ["/abs/build"]}}})


def test_invalid_template_placeholder_rejected() -> None:
    """Reject unknown format placeholders in tag/commit templates."""
    with pytest.raises(ReleaseError, match="Invalid format template"):
        parseConfigData({"tool": {"tablish": {"tag-format": "v{ver}"}}})


def test_generated_files_validation() -> None:
    """Ensure generated-files entries specify exactly one of command or template."""
    with pytest.raises(ReleaseError, match="exactly one of 'command' or 'template'"):
        parseConfigData(
            {
                "tool": {
                    "tablish": {
                        "generated-files": [
                            {"path": "a.txt", "command": ["echo", "hi"], "template": "{version}"},
                        ],
                    },
                },
            },
        )
    with pytest.raises(ReleaseError, match="exactly one of 'command' or 'template'"):
        parseConfigData({"tool": {"tablish": {"generated-files": [{"path": "a.txt"}]}}})
    with pytest.raises(ReleaseError, match="Unknown key"):
        parseConfigData(
            {"tool": {"tablish": {"generated-files": [{"path": "a.txt", "template": "x", "extra": 1}]}}},
        )


def test_load_config_from_disk(tmp_path: Path) -> None:
    """Load and parse a pyproject.toml file from disk."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "demo"\n\n[tool.tablish]\nsemver-file = "VERSION"\ntag = false\n',
        encoding="utf-8",
    )
    cfg = loadConfig(tmp_path)
    assert cfg.projectName == "demo"
    assert cfg.semverFile == Path("VERSION")
    assert cfg.tag is False


def test_load_config_missing_file_raises(tmp_path: Path) -> None:
    """Raise ReleaseError when pyproject.toml is missing."""
    with pytest.raises(ReleaseError, match=r"Could not find pyproject\.toml"):
        loadConfig(tmp_path)


def test_load_config_invalid_toml_raises(tmp_path: Path) -> None:
    """Raise ReleaseError when pyproject.toml contains invalid TOML syntax."""
    (tmp_path / "pyproject.toml").write_text("[tool.tablish\n", encoding="utf-8")
    with pytest.raises(ReleaseError, match="Error loading"):
        loadConfig(tmp_path)
