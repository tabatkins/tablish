"""Tests for tablish pyproject.toml and tablish.kdl configuration loading and validation."""

from __future__ import annotations

from pathlib import Path

import kdl
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
    TABLISH_KDL_FILENAME,
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


def test_kdl_full_config_all_keys() -> None:
    """Verify all configuration keys parse from a standalone tablish.kdl document."""
    kdlText = (
        'project-name "kdlpy"\n'
        + 'semver-file "bikeshed/semver.txt"\n'
        + 'secrets-file "~/.config/tablish/secrets.json"\n'
        + 'pypi-secret-key "prod"\n'
        + 'test-pypi-secret-key "staging"\n'
        + 'pypi-url "https://upload.example.com/legacy/"\n'
        + 'test-pypi-url "https://test.example.com/legacy/"\n'
        + 'build-dirs "build" "dist" "out"\n'
        + 'lint-command "pytest" "-q"\n'
        + "check-manifest false\n"
        + "check-manifest-args\n"
        + "tag false\n"
        + 'tag-format "release-{version}"\n'
        + 'tag-message "Ship {version}"\n'
        + 'commit-message "Release v{version}"\n'
        + 'generated-file "pkg/info.txt" {\n'
        + '    command "git" "log" "-1" "--format=%h"\n'
        + "}\n"
        + 'generated-file "pkg/_version.py" template=r#"__version__ = "{version}""#\n'
    )
    cfg = parseConfigData(kdlData=kdlText)
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
        GeneratedFileConfig(path=Path("pkg/_version.py"), template='__version__ = "{version}"'),
    )
    assert cfg.hasPyproject is False
    assert cfg.hasTablishTable is False
    assert cfg.hasTablishKdl is True
    assert cfg.sourceOf("project-name") == "explicit"
    assert cfg.explicitOrigin("project-name") == TABLISH_KDL_FILENAME
    assert cfg.explicitOrigin("generated-files") == TABLISH_KDL_FILENAME


def test_kdl_overrides_pyproject_and_preserves_unset_keys() -> None:
    """Verify tablish.kdl overrides pyproject.toml key-by-key while preserving unset keys."""
    pyprojectData = {
        "project": {"name": "from-project"},
        "tool": {
            "setuptools": {"dynamic": {"version": {"file": "pkg/semver.txt"}}},
            "tablish": {
                "project-name": "from-toml",
                "pypi-url": "https://old.example.com/legacy/",
                "tag": False,
                "tag-format": "toml-{version}",
                "commit-message": "TOML {version}",
                "generated-files": [{"path": "old.txt", "template": "{version}"}],
            },
        },
    }
    kdlText = "project-name null\n" + "pypi-url null\n" + 'commit-message "KDL {version}"\n' + "generated-files {}\n"
    cfg = parseConfigData(pyprojectData, kdl.parse(kdlText))
    assert cfg.hasPyproject is True
    assert cfg.hasTablishTable is True
    assert cfg.hasTablishKdl is True

    # Overridden by tablish.kdl
    assert cfg.projectName is None
    assert cfg.sourceOf("project-name") == "explicit"
    assert cfg.explicitOrigin("project-name") == TABLISH_KDL_FILENAME
    assert cfg.pypiUrl is None
    assert cfg.explicitOrigin("pypi-url") == TABLISH_KDL_FILENAME
    assert cfg.commitMessage == "KDL {version}"
    assert cfg.explicitOrigin("commit-message") == TABLISH_KDL_FILENAME
    assert not cfg.generatedFiles
    assert cfg.explicitOrigin("generated-files") == TABLISH_KDL_FILENAME

    # Preserved from pyproject.toml [tool.tablish]
    assert cfg.tag is False
    assert cfg.sourceOf("tag") == "explicit"
    assert cfg.explicitOrigin("tag") == "pyproject.toml"
    assert cfg.tagFormat == "toml-{version}"
    assert cfg.explicitOrigin("tag-format") == "pyproject.toml"

    # Still inferred from pyproject.toml
    assert cfg.semverFile == Path("pkg/semver.txt")
    assert cfg.sourceOf("semver-file") == "inferred"
    assert cfg.inferredOrigin("semver-file") == "[tool.setuptools.dynamic].version.file"
    assert cfg.explicitOrigin("semver-file") is None


@pytest.mark.parametrize(
    ("kdlLine", "expectedEnabled", "expectedCmd"),
    [
        ("lint-command false", False, None),
        ("lint-command true", True, None),
        ("lint-command null", False, None),
        ('lint-command ""', False, None),
        ("lint-command", False, None),
        ("lint-command {}", False, None),
        ('lint-command "sh ./lint"', True, ("sh", "./lint")),
        ('lint-command "sh" "lint"', True, ("sh", "lint")),
        ('lint-command {\n    - "pytest"\n    - "-q"\n}', True, ("pytest", "-q")),
    ],
)
def test_kdl_lint_command_variants(
    kdlLine: str,
    expectedEnabled: bool,
    expectedCmd: tuple[str, ...] | None,
) -> None:
    """Verify KDL lint-command accepts booleans, null, empty, strings, argv, and '-' children."""
    cfg = parseConfigData(kdlData=kdlLine)
    assert cfg.lintEnabled is expectedEnabled
    assert cfg.lintCommand == expectedCmd


def test_kdl_list_forms_and_generated_files_variants() -> None:
    """Verify positional args, '-' child lists, generated-files blocks, and generated-file nodes."""
    kdlText = (
        "build-dirs {\n"
        + '    - "build"\n'
        + '    - "custom-dist"\n'
        + "}\n"
        + "check-manifest-args {}\n"
        + "generated-files {\n"
        + '    file "pkg/a.py" template=r#"v = "{version}""#\n'
        + '    generated-file "pkg/b.txt" {\n'
        + '        command "git" "log" "-1" "--format=%h"\n'
        + "    }\n"
        + '    - "pkg/c.txt" command="echo {version}"\n'
        + '    "pkg/d.txt" {\n'
        + "        command {\n"
        + '            - "git"\n'
        + '            - "rev-parse"\n'
        + '            - "HEAD"\n'
        + "        }\n"
        + "    }\n"
        + '    "pkg/e.py" {\n'
        + '        template "{version}"\n'
        + "    }\n"
        + "}\n"
        + 'generated-file path="pkg/f.txt" template="f-{version}"\n'
        + "generated-file {\n"
        + '    path "pkg/g.txt"\n'
        + '    command "echo g"\n'
        + "}\n"
    )
    cfg = parseConfigData(kdlData=kdlText)
    assert cfg.buildDirs == ("build", "custom-dist")
    assert not cfg.checkManifestArgs
    assert cfg.generatedFiles == (
        GeneratedFileConfig(path=Path("pkg/a.py"), template='v = "{version}"'),
        GeneratedFileConfig(path=Path("pkg/b.txt"), command=("git", "log", "-1", "--format=%h")),
        GeneratedFileConfig(path=Path("pkg/c.txt"), command=("echo", "{version}")),
        GeneratedFileConfig(path=Path("pkg/d.txt"), command=("git", "rev-parse", "HEAD")),
        GeneratedFileConfig(path=Path("pkg/e.py"), template="{version}"),
        GeneratedFileConfig(path=Path("pkg/f.txt"), template="f-{version}"),
        GeneratedFileConfig(path=Path("pkg/g.txt"), command=("echo", "g")),
    )


@pytest.mark.parametrize(
    ("badKdl", "matchPattern"),
    [
        ('semver_file "semver.txt"', r"Unknown key.*tablish\.kdl"),
        ("tag true\ntag false", "Duplicate key"),
        ("(bad)tag true", "Unexpected type annotation"),
        ('tag true "extra"', "expects exactly one argument"),
        ('tag true foo="bar"', "does not accept properties"),
        ('build-dirs "build" {\n    - "dist"\n}', "cannot specify both arguments and child nodes"),
        ('build-dirs {\n    dir "build"\n}', "Child nodes"),
        ('generated-files "oops" {}', "does not accept arguments or properties"),
        ('generated-files {\n    flie "a.txt" template="x"\n}', "Unknown child node 'flie'"),
        ('generated-file "a.txt" path="b.txt" template="x"', "Duplicate 'path'"),
        ('generated-file "a.txt" template="x" {\n    template "y"\n}', "Duplicate 'template'"),
        ('generated-file "a.txt" command="echo hi" template="x"', "exactly one of 'command' or 'template'"),
        ('generated-file "a.txt"', "exactly one of 'command' or 'template'"),
        ('generated-file "a.txt" template="x" extra="y"', "Unknown key"),
        ('generated-file "a.txt" {\n    extra "y"\n}', "Unknown key"),
        ('semver-file "../escape.txt"', r"tablish\.kdl.*relative path"),
        ('tag-format "v{ver}"', r"Invalid format template.*tablish\.kdl"),
        ('tag "not-a-bool"', r"tablish\.kdl.*boolean"),
        ("invalid [kdl", "Error parsing"),
    ],
)
def test_kdl_validation_errors(badKdl: str, matchPattern: str) -> None:
    """Reject invalid syntax, unknown keys, bad structure, and invalid values in tablish.kdl."""
    with pytest.raises(ReleaseError, match=matchPattern):
        parseConfigData(kdlData=badKdl)


def test_load_config_kdl_from_disk(tmp_path: Path) -> None:
    """Load standalone tablish.kdl, combined pyproject.toml + tablish.kdl, and reject bad KDL."""
    kdlPath = tmp_path / TABLISH_KDL_FILENAME

    # Standalone empty tablish.kdl (no pyproject.toml) succeeds with defaults
    kdlPath.write_text("// empty config\n", encoding="utf-8")
    emptyCfg = loadConfig(tmp_path)
    assert emptyCfg.hasPyproject is False
    assert emptyCfg.hasTablishKdl is True
    assert emptyCfg.semverFile == Path(DEFAULT_SEMVER_FILE)

    # Standalone tablish.kdl with values
    kdlPath.write_text('project-name "solo"\nsemver-file "VERSION"\ntag false\n', encoding="utf-8")
    soloCfg = loadConfig(tmp_path)
    assert soloCfg.hasPyproject is False
    assert soloCfg.hasTablishKdl is True
    assert soloCfg.projectName == "solo"
    assert soloCfg.semverFile == Path("VERSION")
    assert soloCfg.tag is False

    # Combined pyproject.toml + tablish.kdl: tablish.kdl overrides pyproject.toml
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\n\n[tool.tablish]\nsemver-file = "OLD_VER"\ncommit-message = "From TOML {version}"\n',
        encoding="utf-8",
    )
    mergedCfg = loadConfig(tmp_path)
    assert mergedCfg.hasPyproject is True
    assert mergedCfg.hasTablishTable is True
    assert mergedCfg.hasTablishKdl is True
    assert mergedCfg.projectName == "solo"
    assert mergedCfg.semverFile == Path("VERSION")
    assert mergedCfg.commitMessage == "From TOML {version}"
    assert mergedCfg.explicitOrigin("semver-file") == TABLISH_KDL_FILENAME
    assert mergedCfg.explicitOrigin("commit-message") == "pyproject.toml"

    # Invalid KDL syntax raises ReleaseError
    kdlPath.write_text('tag "unterminated\n', encoding="utf-8")
    with pytest.raises(ReleaseError, match="Error loading"):
        loadConfig(tmp_path)
