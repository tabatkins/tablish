"""Configuration model and pyproject.toml loader for tablish."""

from __future__ import annotations

import shlex
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import t

DEFAULT_TEST_PYPI_URL: t.Final[str] = "https://test.pypi.org/legacy/"
DEFAULT_SEMVER_FILE: t.Final[str] = "semver.txt"
DEFAULT_SECRETS_FILE: t.Final[str] = "secrets.json"
DEFAULT_PYPI_SECRET_KEY: t.Final[str] = "pypi.org release key"
DEFAULT_TEST_PYPI_SECRET_KEY: t.Final[str] = "test.pypi.org release key"
DEFAULT_BUILD_DIRS: t.Final[tuple[str, ...]] = ("build", "dist")
DEFAULT_CHECK_MANIFEST_ARGS: t.Final[tuple[str, ...]] = ("--no-build-isolation",)
DEFAULT_TAG_FORMAT: t.Final[str] = "v{version}"
DEFAULT_TAG_MESSAGE: t.Final[str] = "Release {version}"
DEFAULT_COMMIT_MESSAGE: t.Final[str] = "Bump semver to {version}"

_KNOWN_CONFIG_KEYS: t.Final[frozenset[str]] = frozenset(
    {
        "project-name",
        "semver-file",
        "secrets-file",
        "pypi-secret-key",
        "test-pypi-secret-key",
        "pypi-url",
        "test-pypi-url",
        "build-dirs",
        "lint-command",
        "check-manifest",
        "check-manifest-args",
        "tag",
        "tag-format",
        "tag-message",
        "commit-message",
        "generated-files",
    },
)

_KNOWN_GENERATED_FILE_KEYS: t.Final[frozenset[str]] = frozenset({"path", "command", "template"})


class ReleaseError(Exception):
    """Expected, user-facing failure during release preflight or execution."""


@dataclass(frozen=True, slots=True)
class GeneratedFileConfig:
    """Configuration for an extra file generated and committed during release."""

    path: Path
    command: tuple[str, ...] | None = None
    template: str | None = None


@dataclass(frozen=True, slots=True)
class TablishConfig:
    """Project-specific release settings loaded from pyproject.toml."""

    projectName: str | None = None
    semverFile: Path = Path(DEFAULT_SEMVER_FILE)
    secretsFile: Path = Path(DEFAULT_SECRETS_FILE)
    pypiSecretKey: str = DEFAULT_PYPI_SECRET_KEY
    testPypiSecretKey: str = DEFAULT_TEST_PYPI_SECRET_KEY
    pypiUrl: str | None = None
    testPypiUrl: str = DEFAULT_TEST_PYPI_URL
    buildDirs: tuple[str, ...] = DEFAULT_BUILD_DIRS
    lintEnabled: bool = True
    lintCommand: tuple[str, ...] | None = None
    checkManifest: bool = True
    checkManifestArgs: tuple[str, ...] = DEFAULT_CHECK_MANIFEST_ARGS
    tag: bool = True
    tagFormat: str = DEFAULT_TAG_FORMAT
    tagMessage: str = DEFAULT_TAG_MESSAGE
    commitMessage: str = DEFAULT_COMMIT_MESSAGE
    generatedFiles: tuple[GeneratedFileConfig, ...] = ()
    hasTablishTable: bool = False
    explicitKeys: frozenset[str] = frozenset()
    inferredSources: tuple[tuple[str, str], ...] = ()

    def sourceOf(self, key: str) -> t.ConfigSource:
        """Return whether a config key was explicitly set, inferred, or defaulted."""
        if key in self.explicitKeys:
            return "explicit"
        if any(inferredKey == key for inferredKey, _ in self.inferredSources):
            return "inferred"
        return "default"

    def inferredOrigin(self, key: str) -> str | None:
        """Return the pyproject.toml origin string for an inferred config key, if any."""
        for inferredKey, origin in self.inferredSources:
            if inferredKey == key:
                return origin
        return None


def _validateRelativePath(rawPath: str, keyName: str) -> Path:
    """Validate that a config value is a non-empty relative path inside the project root."""
    stripped = rawPath.strip()
    if not stripped:
        msg = f"[tool.tablish].{keyName} must be a non-empty relative path."
        raise ReleaseError(msg)
    path = Path(stripped)
    if path.is_absolute() or ".." in path.parts or path == Path("."):
        msg = f"[tool.tablish].{keyName} must be a relative path within the project root (got {rawPath!r})."
        raise ReleaseError(msg)
    return path


def _parseNonEmptyString(raw: object, keyName: str) -> str:
    """Validate that a config value is a non-empty string."""
    if not isinstance(raw, str) or not raw.strip():
        msg = f"[tool.tablish].{keyName} must be a non-empty string."
        raise ReleaseError(msg)
    return raw.strip()


def _parseOptionalString(raw: object, keyName: str) -> str | None:
    """Validate that a config value is a string, treating empty strings as None."""
    if not isinstance(raw, str):
        msg = f"[tool.tablish].{keyName} must be a string."
        raise ReleaseError(msg)
    stripped = raw.strip()
    return stripped if stripped else None


def _parseBool(raw: object, keyName: str) -> bool:
    """Validate that a config value is a boolean."""
    if not isinstance(raw, bool):
        msg = f"[tool.tablish].{keyName} must be a boolean."
        raise ReleaseError(msg)
    return raw


def _parseStringList(raw: object, keyName: str, *, allowEmpty: bool = True) -> tuple[str, ...]:
    """Validate that a config value is a list of non-empty strings."""
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        msg = f"[tool.tablish].{keyName} must be a list of strings."
        raise ReleaseError(msg)
    items = tuple(item for item in raw if item)
    if len(items) != len(raw):
        msg = f"[tool.tablish].{keyName} must not contain empty strings."
        raise ReleaseError(msg)
    if not allowEmpty and not items:
        msg = f"[tool.tablish].{keyName} must not be empty."
        raise ReleaseError(msg)
    return items


def _parseCommand(raw: object, keyName: str) -> tuple[str, ...]:
    """Parse a command configured as either a string (shlex-split) or a list of strings."""
    if isinstance(raw, str):
        try:
            parts = tuple(shlex.split(raw))
        except ValueError as exc:
            msg = f"Invalid shell syntax in [tool.tablish].{keyName}: {exc}"
            raise ReleaseError(msg) from exc
        if not parts:
            msg = f"[tool.tablish].{keyName} must not be empty."
            raise ReleaseError(msg)
        return parts
    return _parseStringList(raw, keyName, allowEmpty=False)


def _validateVersionTemplate(template: str, keyName: str) -> str:
    """Verify that a format template only references valid placeholders like {version}."""
    try:
        template.format(version="0.0.0")
    except (KeyError, ValueError, IndexError) as exc:
        msg = f"Invalid format template in [tool.tablish].{keyName} ({template!r}): {exc}"
        raise ReleaseError(msg) from exc
    return template


def _inferProjectName(pyprojectData: t.Mapping[str, t.Any]) -> str | None:
    """Infer the default project name from [project].name when present."""
    projectTable = pyprojectData.get("project")
    if isinstance(projectTable, dict):
        name = projectTable.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def _inferSemverFile(pyprojectData: t.Mapping[str, t.Any]) -> Path | None:
    """Infer the version file from [tool.setuptools.dynamic].version.file when present."""
    toolTable = pyprojectData.get("tool")
    if isinstance(toolTable, dict):
        setuptoolsTable = toolTable.get("setuptools")
        if isinstance(setuptoolsTable, dict):
            dynamicTable = setuptoolsTable.get("dynamic")
            if isinstance(dynamicTable, dict):
                versionSpec = dynamicTable.get("version")
                if isinstance(versionSpec, dict):
                    fileSpec = versionSpec.get("file")
                    if isinstance(fileSpec, str) and fileSpec.strip():
                        return _validateRelativePath(fileSpec, "setuptools.dynamic.version.file")
                    if isinstance(fileSpec, list) and len(fileSpec) == 1 and isinstance(fileSpec[0], str):
                        return _validateRelativePath(fileSpec[0], "setuptools.dynamic.version.file")
    return None


def _parseLintCommand(raw: object) -> tuple[bool, tuple[str, ...] | None]:
    """Parse [tool.tablish].lint-command into (lintEnabled, lintCommand)."""
    if raw is False:
        return (False, None)
    if raw is True:
        return (True, None)
    if isinstance(raw, str):
        if not raw.strip():
            return (False, None)
        return (True, _parseCommand(raw, "lint-command"))
    if isinstance(raw, list):
        if not raw:
            return (False, None)
        return (True, _parseStringList(raw, "lint-command", allowEmpty=False))
    msg = "[tool.tablish].lint-command must be a command string, list of strings, or false."
    raise ReleaseError(msg)


def _parseGeneratedFiles(raw: object) -> tuple[GeneratedFileConfig, ...]:
    """Parse [[tool.tablish.generated-files]] entries."""
    if not isinstance(raw, list):
        msg = "[tool.tablish].generated-files must be an array of tables."
        raise ReleaseError(msg)

    entries: list[GeneratedFileConfig] = []
    for index, item in enumerate(raw):
        prefix = f"generated-files[{index}]"
        if not isinstance(item, dict):
            msg = f"[tool.tablish].{prefix} must be a table."
            raise ReleaseError(msg)

        unknownKeys = sorted(set(item) - _KNOWN_GENERATED_FILE_KEYS)
        if unknownKeys:
            msg = f"Unknown key(s) in [tool.tablish].{prefix}: {', '.join(unknownKeys)}"
            raise ReleaseError(msg)

        if "path" not in item or not isinstance(item["path"], str):
            msg = f"[tool.tablish].{prefix}.path must be a relative path string."
            raise ReleaseError(msg)
        path = _validateRelativePath(item["path"], f"{prefix}.path")

        hasCommand = "command" in item
        hasTemplate = "template" in item
        if hasCommand == hasTemplate:
            msg = f"[tool.tablish].{prefix} must specify exactly one of 'command' or 'template'."
            raise ReleaseError(msg)

        command: tuple[str, ...] | None = None
        template: str | None = None
        if hasCommand:
            command = _parseCommand(item["command"], f"{prefix}.command")
        else:
            rawTemplate = item["template"]
            if not isinstance(rawTemplate, str):
                msg = f"[tool.tablish].{prefix}.template must be a string."
                raise ReleaseError(msg)
            template = rawTemplate

        entries.append(GeneratedFileConfig(path=path, command=command, template=template))

    return tuple(entries)


def parseConfigData(pyprojectData: t.Mapping[str, t.Any]) -> TablishConfig:
    """Parse a decoded pyproject.toml mapping into a validated TablishConfig."""
    toolTable = pyprojectData.get("tool")
    if toolTable is not None and not isinstance(toolTable, dict):
        msg = "Invalid pyproject.toml: [tool] must be a table."
        raise ReleaseError(msg)

    rawTablish = toolTable.get("tablish") if isinstance(toolTable, dict) else None
    hasTablishTable = rawTablish is not None
    if rawTablish is None:
        tablishTable: dict[str, t.Any] = {}
    elif isinstance(rawTablish, dict):
        tablishTable = rawTablish
    else:
        msg = "Invalid pyproject.toml: [tool.tablish] must be a table."
        raise ReleaseError(msg)

    unknownKeys = sorted(set(tablishTable) - _KNOWN_CONFIG_KEYS)
    if unknownKeys:
        msg = f"Unknown key(s) in [tool.tablish]: {', '.join(unknownKeys)}"
        raise ReleaseError(msg)

    explicitKeys = frozenset(tablishTable)
    inferredSources: list[tuple[str, str]] = []

    if "project-name" in tablishTable:
        projectName = _parseOptionalString(tablishTable["project-name"], "project-name")
    else:
        projectName = _inferProjectName(pyprojectData)
        if projectName is not None:
            inferredSources.append(("project-name", "[project].name"))

    if "semver-file" in tablishTable:
        rawSemverFile = _parseNonEmptyString(tablishTable["semver-file"], "semver-file")
        semverFile = _validateRelativePath(rawSemverFile, "semver-file")
    else:
        inferredSemver = _inferSemverFile(pyprojectData)
        if inferredSemver is not None:
            semverFile = inferredSemver
            inferredSources.append(("semver-file", "[tool.setuptools.dynamic].version.file"))
        else:
            semverFile = Path(DEFAULT_SEMVER_FILE)

    if "secrets-file" in tablishTable:
        secretsFile = Path(_parseNonEmptyString(tablishTable["secrets-file"], "secrets-file"))
    else:
        secretsFile = Path(DEFAULT_SECRETS_FILE)

    pypiSecretKey = (
        _parseNonEmptyString(tablishTable["pypi-secret-key"], "pypi-secret-key")
        if "pypi-secret-key" in tablishTable
        else DEFAULT_PYPI_SECRET_KEY
    )
    testPypiSecretKey = (
        _parseNonEmptyString(tablishTable["test-pypi-secret-key"], "test-pypi-secret-key")
        if "test-pypi-secret-key" in tablishTable
        else DEFAULT_TEST_PYPI_SECRET_KEY
    )
    pypiUrl = _parseOptionalString(tablishTable["pypi-url"], "pypi-url") if "pypi-url" in tablishTable else None
    testPypiUrl = (
        _parseNonEmptyString(tablishTable["test-pypi-url"], "test-pypi-url")
        if "test-pypi-url" in tablishTable
        else DEFAULT_TEST_PYPI_URL
    )

    if "build-dirs" in tablishTable:
        rawBuildDirs = _parseStringList(tablishTable["build-dirs"], "build-dirs", allowEmpty=True)
        buildDirs = tuple(str(_validateRelativePath(dirName, "build-dirs")) for dirName in rawBuildDirs)
    else:
        buildDirs = DEFAULT_BUILD_DIRS

    if "lint-command" in tablishTable:
        lintEnabled, lintCommand = _parseLintCommand(tablishTable["lint-command"])
    else:
        lintEnabled, lintCommand = (True, None)

    checkManifest = (
        _parseBool(tablishTable["check-manifest"], "check-manifest") if "check-manifest" in tablishTable else True
    )
    checkManifestArgs = (
        _parseStringList(tablishTable["check-manifest-args"], "check-manifest-args", allowEmpty=True)
        if "check-manifest-args" in tablishTable
        else DEFAULT_CHECK_MANIFEST_ARGS
    )

    tag = _parseBool(tablishTable["tag"], "tag") if "tag" in tablishTable else True
    tagFormat = _validateVersionTemplate(
        (
            _parseNonEmptyString(tablishTable["tag-format"], "tag-format")
            if "tag-format" in tablishTable
            else DEFAULT_TAG_FORMAT
        ),
        "tag-format",
    )
    tagMessage = _validateVersionTemplate(
        (
            _parseNonEmptyString(tablishTable["tag-message"], "tag-message")
            if "tag-message" in tablishTable
            else DEFAULT_TAG_MESSAGE
        ),
        "tag-message",
    )
    commitMessage = _validateVersionTemplate(
        (
            _parseNonEmptyString(tablishTable["commit-message"], "commit-message")
            if "commit-message" in tablishTable
            else DEFAULT_COMMIT_MESSAGE
        ),
        "commit-message",
    )

    generatedFiles = _parseGeneratedFiles(tablishTable["generated-files"]) if "generated-files" in tablishTable else ()

    return TablishConfig(
        projectName=projectName,
        semverFile=semverFile,
        secretsFile=secretsFile,
        pypiSecretKey=pypiSecretKey,
        testPypiSecretKey=testPypiSecretKey,
        pypiUrl=pypiUrl,
        testPypiUrl=testPypiUrl,
        buildDirs=buildDirs,
        lintEnabled=lintEnabled,
        lintCommand=lintCommand,
        checkManifest=checkManifest,
        checkManifestArgs=checkManifestArgs,
        tag=tag,
        tagFormat=tagFormat,
        tagMessage=tagMessage,
        commitMessage=commitMessage,
        generatedFiles=generatedFiles,
        hasTablishTable=hasTablishTable,
        explicitKeys=explicitKeys,
        inferredSources=tuple(inferredSources),
    )


def loadConfig(projectRoot: Path) -> TablishConfig:
    """Load and validate tablish configuration from pyproject.toml in projectRoot."""
    pyprojectPath = projectRoot / "pyproject.toml"
    if not pyprojectPath.is_file():
        msg = f"Could not find pyproject.toml in {projectRoot}."
        raise ReleaseError(msg)
    try:
        rawData = tomllib.loads(pyprojectPath.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        msg = f"Error loading {pyprojectPath}: {exc}"
        raise ReleaseError(msg) from exc
    return parseConfigData(rawData)
