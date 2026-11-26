"""Configuration model and pyproject.toml / tablish.kdl loader for tablish."""

from __future__ import annotations

import shlex
import tomllib
from dataclasses import dataclass
from pathlib import Path

import kdl

from . import t

TABLISH_KDL_FILENAME: t.Final[str] = "tablish.kdl"
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

_KNOWN_KDL_NODES: t.Final[frozenset[str]] = _KNOWN_CONFIG_KEYS | frozenset({"generated-file"})
_KNOWN_GENERATED_FILE_KEYS: t.Final[frozenset[str]] = frozenset({"path", "command", "template"})
_GENERATED_FILE_WRAPPER_NAMES: t.Final[frozenset[str]] = frozenset({"file", "generated-file", "-"})

_SCALAR_NON_EMPTY_STRING_KEYS: t.Final[frozenset[str]] = frozenset(
    {
        "semver-file",
        "secrets-file",
        "pypi-secret-key",
        "test-pypi-secret-key",
        "test-pypi-url",
        "tag-format",
        "tag-message",
        "commit-message",
    },
)
_SCALAR_OPTIONAL_STRING_KEYS: t.Final[frozenset[str]] = frozenset({"project-name", "pypi-url"})
_SCALAR_BOOL_KEYS: t.Final[frozenset[str]] = frozenset({"check-manifest", "tag"})
_STRING_LIST_KEYS: t.Final[frozenset[str]] = frozenset({"build-dirs", "check-manifest-args"})


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
    """Project-specific release settings loaded from pyproject.toml and/or tablish.kdl."""

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
    hasPyproject: bool = True
    hasTablishTable: bool = False
    hasTablishKdl: bool = False
    explicitKeys: frozenset[str] = frozenset()
    explicitSources: tuple[tuple[str, str], ...] = ()
    inferredSources: tuple[tuple[str, str], ...] = ()

    def sourceOf(self, key: str) -> t.ConfigSource:
        """Return whether a config key was explicitly set, inferred, or defaulted."""
        if key in self.explicitKeys:
            return "explicit"
        if any(inferredKey == key for inferredKey, _ in self.inferredSources):
            return "inferred"
        return "default"

    def explicitOrigin(self, key: str) -> str | None:
        """Return the config file origin ('tablish.kdl' or 'pyproject.toml') for an explicit key."""
        for explicitKey, origin in self.explicitSources:
            if explicitKey == key:
                return origin
        return None

    def inferredOrigin(self, key: str) -> str | None:
        """Return the pyproject.toml origin string for an inferred config key, if any."""
        for inferredKey, origin in self.inferredSources:
            if inferredKey == key:
                return origin
        return None


def _keyLabel(keyName: str, origin: str = "pyproject.toml") -> str:
    """Format a user-facing config key reference for error messages."""
    if origin == TABLISH_KDL_FILENAME:
        return f"{TABLISH_KDL_FILENAME}: {keyName}"
    return f"[tool.tablish].{keyName}"


def _validateRelativePath(rawPath: str, keyLabel: str) -> Path:
    """Validate that a config value is a non-empty relative path inside the project root."""
    stripped = rawPath.strip()
    if not stripped:
        msg = f"{keyLabel} must be a non-empty relative path."
        raise ReleaseError(msg)
    path = Path(stripped)
    if path.is_absolute() or ".." in path.parts or path == Path("."):
        msg = f"{keyLabel} must be a relative path within the project root (got {rawPath!r})."
        raise ReleaseError(msg)
    return path


def _parseNonEmptyString(raw: object, keyLabel: str) -> str:
    """Validate that a config value is a non-empty string."""
    if not isinstance(raw, str) or not raw.strip():
        msg = f"{keyLabel} must be a non-empty string."
        raise ReleaseError(msg)
    return raw.strip()


def _parseOptionalString(raw: object, keyLabel: str) -> str | None:
    """Validate that a config value is a string, treating empty strings as None."""
    if not isinstance(raw, str):
        msg = f"{keyLabel} must be a string."
        raise ReleaseError(msg)
    stripped = raw.strip()
    return stripped if stripped else None


def _parseBool(raw: object, keyLabel: str) -> bool:
    """Validate that a config value is a boolean."""
    if not isinstance(raw, bool):
        msg = f"{keyLabel} must be a boolean."
        raise ReleaseError(msg)
    return raw


def _parseStringList(raw: object, keyLabel: str, *, allowEmpty: bool = True) -> tuple[str, ...]:
    """Validate that a config value is a list of non-empty strings."""
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        msg = f"{keyLabel} must be a list of strings."
        raise ReleaseError(msg)
    items = tuple(item for item in raw if item)
    if len(items) != len(raw):
        msg = f"{keyLabel} must not contain empty strings."
        raise ReleaseError(msg)
    if not allowEmpty and not items:
        msg = f"{keyLabel} must not be empty."
        raise ReleaseError(msg)
    return items


def _parseCommand(raw: object, keyLabel: str) -> tuple[str, ...]:
    """Parse a command configured as either a string (shlex-split) or a list of strings."""
    if isinstance(raw, str):
        try:
            parts = tuple(shlex.split(raw))
        except ValueError as exc:
            msg = f"Invalid shell syntax in {keyLabel}: {exc}"
            raise ReleaseError(msg) from exc
        if not parts:
            msg = f"{keyLabel} must not be empty."
            raise ReleaseError(msg)
        return parts
    return _parseStringList(raw, keyLabel, allowEmpty=False)


def _validateVersionTemplate(template: str, keyLabel: str) -> str:
    """Verify that a format template only references valid placeholders like {version}."""
    try:
        template.format(version="0.0.0")
    except (KeyError, ValueError, IndexError) as exc:
        msg = f"Invalid format template in {keyLabel} ({template!r}): {exc}"
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
                        return _validateRelativePath(fileSpec, "[tool.setuptools.dynamic].version.file")
                    if isinstance(fileSpec, list) and len(fileSpec) == 1 and isinstance(fileSpec[0], str):
                        return _validateRelativePath(fileSpec[0], "[tool.setuptools.dynamic].version.file")
    return None


def _parseLintCommand(raw: object, keyLabel: str) -> tuple[bool, tuple[str, ...] | None]:
    """Parse lint-command into (lintEnabled, lintCommand)."""
    if raw is False:
        return (False, None)
    if raw is True:
        return (True, None)
    if isinstance(raw, str):
        if not raw.strip():
            return (False, None)
        return (True, _parseCommand(raw, keyLabel))
    if isinstance(raw, list):
        if not raw:
            return (False, None)
        return (True, _parseStringList(raw, keyLabel, allowEmpty=False))
    msg = f"{keyLabel} must be a command string, list of strings, or false."
    raise ReleaseError(msg)


def _parseGeneratedFiles(raw: object, origin: str = "pyproject.toml") -> tuple[GeneratedFileConfig, ...]:
    """Parse generated-files entries from pyproject.toml or tablish.kdl."""
    if not isinstance(raw, list):
        msg = f"{_keyLabel('generated-files', origin)} must be an array of tables."
        raise ReleaseError(msg)

    entries: list[GeneratedFileConfig] = []
    for index, item in enumerate(raw):
        entryLabel = _keyLabel(f"generated-files[{index}]", origin)
        if not isinstance(item, dict):
            msg = f"{entryLabel} must be a table."
            raise ReleaseError(msg)

        unknownKeys = sorted(set(item) - _KNOWN_GENERATED_FILE_KEYS)
        if unknownKeys:
            msg = f"Unknown key(s) in {entryLabel}: {', '.join(unknownKeys)}"
            raise ReleaseError(msg)

        if "path" not in item or not isinstance(item["path"], str):
            msg = f"{entryLabel}.path must be a relative path string."
            raise ReleaseError(msg)
        path = _validateRelativePath(item["path"], f"{entryLabel}.path")

        hasCommand = "command" in item
        hasTemplate = "template" in item
        if hasCommand == hasTemplate:
            msg = f"{entryLabel} must specify exactly one of 'command' or 'template'."
            raise ReleaseError(msg)

        command: tuple[str, ...] | None = None
        template: str | None = None
        if hasCommand:
            command = _parseCommand(item["command"], f"{entryLabel}.command")
        else:
            rawTemplate = item["template"]
            if not isinstance(rawTemplate, str):
                msg = f"{entryLabel}.template must be a string."
                raise ReleaseError(msg)
            template = rawTemplate

        entries.append(GeneratedFileConfig(path=path, command=command, template=template))

    return tuple(entries)


def _ensureNoNodeTag(node: kdl.Node, context: str) -> None:
    """Reject unexpected KDL type annotations on config nodes."""
    if node.tag is not None:
        msg = f"Unexpected type annotation '({node.tag})' in {context}."
        raise ReleaseError(msg)


def _parseKdlScalarArg(node: kdl.Node, keyLabel: str) -> object:
    """Extract a single positional argument from a scalar KDL config node."""
    _ensureNoNodeTag(node, keyLabel)
    if node.props or node.nodes:
        msg = f"{keyLabel} does not accept properties or child nodes."
        raise ReleaseError(msg)
    if len(node.args) != 1:
        msg = f"{keyLabel} expects exactly one argument."
        raise ReleaseError(msg)
    value: object = node.args[0]
    return value


def _parseKdlStringListNode(node: kdl.Node, keyLabel: str) -> list[object]:
    """Extract list items from either positional arguments or '-' child nodes in KDL."""
    _ensureNoNodeTag(node, keyLabel)
    if node.props:
        msg = f"{keyLabel} does not accept properties."
        raise ReleaseError(msg)
    if node.args and node.nodes:
        msg = f"{keyLabel} cannot specify both arguments and child nodes."
        raise ReleaseError(msg)
    if node.nodes:
        items: list[object] = []
        for child in node.nodes:
            _ensureNoNodeTag(child, keyLabel)
            if child.name != "-" or child.props or child.nodes or len(child.args) != 1:
                msg = f"Child nodes of {keyLabel} must be '-' nodes with a single string argument."
                raise ReleaseError(msg)
            item: object = child.args[0]
            items.append(item)
        return items
    return list(node.args)


def _parseKdlCommandNode(node: kdl.Node, keyLabel: str) -> object:
    """Extract a command value (scalar arg, argv list, or '-' children) from a KDL node."""
    _ensureNoNodeTag(node, keyLabel)
    if node.props:
        msg = f"{keyLabel} does not accept properties."
        raise ReleaseError(msg)
    if node.args and node.nodes:
        msg = f"{keyLabel} cannot specify both arguments and child nodes."
        raise ReleaseError(msg)
    if node.nodes:
        return _parseKdlStringListNode(node, keyLabel)
    if len(node.args) == 1:
        value: object = node.args[0]
        return value
    return list(node.args)


def _parseKdlGeneratedFileEntry(
    node: kdl.Node,
    *,
    implicitPath: str | None,
    entryLabel: str,
) -> dict[str, object]:
    """Convert a KDL generated-file node into a normalized dict for validation."""
    _ensureNoNodeTag(node, entryLabel)
    unknownProps = sorted(set(node.props) - _KNOWN_GENERATED_FILE_KEYS)
    if unknownProps:
        msg = f"Unknown key(s) in {entryLabel}: {', '.join(unknownProps)}"
        raise ReleaseError(msg)

    entry: dict[str, object] = {}
    if implicitPath is not None:
        if node.args:
            msg = (
                f"Unknown child node {node.name!r} in {_keyLabel('generated-files', TABLISH_KDL_FILENAME)} "
                + "(unexpected positional arguments)."
            )
            raise ReleaseError(msg)
        entry["path"] = implicitPath
    else:
        if len(node.args) > 1:
            msg = f"{entryLabel} expects at most one positional path argument."
            raise ReleaseError(msg)
        if len(node.args) == 1:
            pathArg: object = node.args[0]
            entry["path"] = pathArg

    for propKey, propVal in node.props.items():
        if propKey in entry:
            msg = f"Duplicate '{propKey}' in {entryLabel}."
            raise ReleaseError(msg)
        propValue: object = propVal
        entry[propKey] = propValue

    for child in node.nodes:
        childLabel = f"{entryLabel}.{child.name}"
        _ensureNoNodeTag(child, childLabel)
        if child.name not in _KNOWN_GENERATED_FILE_KEYS:
            msg = f"Unknown key(s) in {entryLabel}: {child.name}"
            raise ReleaseError(msg)
        if child.name in entry:
            msg = f"Duplicate '{child.name}' in {entryLabel}."
            raise ReleaseError(msg)
        if child.name == "command":
            entry["command"] = _parseKdlCommandNode(child, childLabel)
        else:
            entry[child.name] = _parseKdlScalarArg(child, childLabel)

    return entry


def _parseKdlDocument(doc: kdl.Document) -> dict[str, t.Any]:
    """Convert a parsed tablish.kdl document into a validated raw config dictionary."""
    unknownNodes = sorted({node.name for node in doc.nodes} - _KNOWN_KDL_NODES)
    if unknownNodes:
        msg = f"Unknown key(s) in {TABLISH_KDL_FILENAME}: {', '.join(unknownNodes)}"
        raise ReleaseError(msg)

    kdlTable: dict[str, t.Any] = {}
    generatedEntries: list[dict[str, object]] = []
    hasGeneratedFiles = False

    for node in doc.nodes:
        if node.name == "generated-file":
            hasGeneratedFiles = True
            entryLabel = _keyLabel(f"generated-files[{len(generatedEntries)}]", TABLISH_KDL_FILENAME)
            generatedEntries.append(_parseKdlGeneratedFileEntry(node, implicitPath=None, entryLabel=entryLabel))
            continue

        if node.name == "generated-files":
            blockLabel = _keyLabel("generated-files", TABLISH_KDL_FILENAME)
            _ensureNoNodeTag(node, blockLabel)
            if node.args or node.props:
                msg = f"{blockLabel} does not accept arguments or properties; use child nodes."
                raise ReleaseError(msg)
            hasGeneratedFiles = True
            for child in node.nodes:
                entryLabel = _keyLabel(f"generated-files[{len(generatedEntries)}]", TABLISH_KDL_FILENAME)
                implicitPath = None if child.name in _GENERATED_FILE_WRAPPER_NAMES else child.name
                generatedEntries.append(
                    _parseKdlGeneratedFileEntry(child, implicitPath=implicitPath, entryLabel=entryLabel),
                )
            continue

        if node.name in kdlTable:
            msg = f"Duplicate key '{node.name}' in {TABLISH_KDL_FILENAME}."
            raise ReleaseError(msg)

        keyLabel = _keyLabel(node.name, TABLISH_KDL_FILENAME)
        if node.name in _SCALAR_NON_EMPTY_STRING_KEYS or node.name in _SCALAR_BOOL_KEYS:
            kdlTable[node.name] = _parseKdlScalarArg(node, keyLabel)
        elif node.name in _SCALAR_OPTIONAL_STRING_KEYS:
            optionalVal = _parseKdlScalarArg(node, keyLabel)
            kdlTable[node.name] = "" if optionalVal is None else optionalVal
        elif node.name in _STRING_LIST_KEYS:
            kdlTable[node.name] = _parseKdlStringListNode(node, keyLabel)
        elif node.name == "lint-command":
            rawLint = _parseKdlCommandNode(node, keyLabel)
            kdlTable[node.name] = False if rawLint is None else rawLint

    if hasGeneratedFiles:
        kdlTable["generated-files"] = generatedEntries

    return kdlTable


def parseConfigData(
    pyprojectData: t.Mapping[str, t.Any] | None = None,
    kdlData: kdl.Document | str | None = None,
) -> TablishConfig:
    """Parse decoded pyproject.toml and/or tablish.kdl data into a validated TablishConfig."""
    hasPyproject = pyprojectData is not None
    pyprojectMap: t.Mapping[str, t.Any] = pyprojectData if pyprojectData is not None else {}

    toolTable = pyprojectMap.get("tool")
    if toolTable is not None and not isinstance(toolTable, dict):
        msg = "Invalid pyproject.toml: [tool] must be a table."
        raise ReleaseError(msg)

    rawTablish = toolTable.get("tablish") if isinstance(toolTable, dict) else None
    hasTablishTable = rawTablish is not None
    if rawTablish is None:
        pyprojectTablishTable: dict[str, t.Any] = {}
    elif isinstance(rawTablish, dict):
        pyprojectTablishTable = rawTablish
    else:
        msg = "Invalid pyproject.toml: [tool.tablish] must be a table."
        raise ReleaseError(msg)

    unknownPyprojectKeys = sorted(set(pyprojectTablishTable) - _KNOWN_CONFIG_KEYS)
    if unknownPyprojectKeys:
        msg = f"Unknown key(s) in [tool.tablish]: {', '.join(unknownPyprojectKeys)}"
        raise ReleaseError(msg)

    hasTablishKdl = kdlData is not None
    kdlTable: dict[str, t.Any] = {}
    if kdlData is not None:
        if isinstance(kdlData, str):
            try:
                kdlDoc = kdl.parse(kdlData)
            except kdl.ParseError as exc:
                msg = f"Error parsing {TABLISH_KDL_FILENAME}: {exc}"
                raise ReleaseError(msg) from exc
        else:
            kdlDoc = kdlData
        kdlTable = _parseKdlDocument(kdlDoc)

    tablishTable: dict[str, t.Any] = dict(pyprojectTablishTable)
    keyOrigins: dict[str, str] = {key: "pyproject.toml" for key in pyprojectTablishTable}
    for key, value in kdlTable.items():
        tablishTable[key] = value
        keyOrigins[key] = TABLISH_KDL_FILENAME

    explicitKeys = frozenset(tablishTable)
    explicitSources = tuple(sorted(keyOrigins.items()))
    inferredSources: list[tuple[str, str]] = []

    def _label(key: str) -> str:
        return _keyLabel(key, keyOrigins.get(key, "pyproject.toml"))

    if "project-name" in tablishTable:
        projectName = _parseOptionalString(tablishTable["project-name"], _label("project-name"))
    else:
        projectName = _inferProjectName(pyprojectMap)
        if projectName is not None:
            inferredSources.append(("project-name", "[project].name"))

    if "semver-file" in tablishTable:
        rawSemverFile = _parseNonEmptyString(tablishTable["semver-file"], _label("semver-file"))
        semverFile = _validateRelativePath(rawSemverFile, _label("semver-file"))
    else:
        inferredSemver = _inferSemverFile(pyprojectMap)
        if inferredSemver is not None:
            semverFile = inferredSemver
            inferredSources.append(("semver-file", "[tool.setuptools.dynamic].version.file"))
        else:
            semverFile = Path(DEFAULT_SEMVER_FILE)

    if "secrets-file" in tablishTable:
        secretsFile = Path(_parseNonEmptyString(tablishTable["secrets-file"], _label("secrets-file")))
    else:
        secretsFile = Path(DEFAULT_SECRETS_FILE)

    pypiSecretKey = (
        _parseNonEmptyString(tablishTable["pypi-secret-key"], _label("pypi-secret-key"))
        if "pypi-secret-key" in tablishTable
        else DEFAULT_PYPI_SECRET_KEY
    )
    testPypiSecretKey = (
        _parseNonEmptyString(tablishTable["test-pypi-secret-key"], _label("test-pypi-secret-key"))
        if "test-pypi-secret-key" in tablishTable
        else DEFAULT_TEST_PYPI_SECRET_KEY
    )
    pypiUrl = _parseOptionalString(tablishTable["pypi-url"], _label("pypi-url")) if "pypi-url" in tablishTable else None
    testPypiUrl = (
        _parseNonEmptyString(tablishTable["test-pypi-url"], _label("test-pypi-url"))
        if "test-pypi-url" in tablishTable
        else DEFAULT_TEST_PYPI_URL
    )

    if "build-dirs" in tablishTable:
        buildDirsLabel = _label("build-dirs")
        rawBuildDirs = _parseStringList(tablishTable["build-dirs"], buildDirsLabel, allowEmpty=True)
        buildDirs = tuple(str(_validateRelativePath(dirName, buildDirsLabel)) for dirName in rawBuildDirs)
    else:
        buildDirs = DEFAULT_BUILD_DIRS

    if "lint-command" in tablishTable:
        lintEnabled, lintCommand = _parseLintCommand(tablishTable["lint-command"], _label("lint-command"))
    else:
        lintEnabled, lintCommand = (True, None)

    checkManifest = (
        _parseBool(tablishTable["check-manifest"], _label("check-manifest"))
        if "check-manifest" in tablishTable
        else True
    )
    checkManifestArgs = (
        _parseStringList(tablishTable["check-manifest-args"], _label("check-manifest-args"), allowEmpty=True)
        if "check-manifest-args" in tablishTable
        else DEFAULT_CHECK_MANIFEST_ARGS
    )

    tag = _parseBool(tablishTable["tag"], _label("tag")) if "tag" in tablishTable else True
    tagFormat = _validateVersionTemplate(
        (
            _parseNonEmptyString(tablishTable["tag-format"], _label("tag-format"))
            if "tag-format" in tablishTable
            else DEFAULT_TAG_FORMAT
        ),
        _label("tag-format"),
    )
    tagMessage = _validateVersionTemplate(
        (
            _parseNonEmptyString(tablishTable["tag-message"], _label("tag-message"))
            if "tag-message" in tablishTable
            else DEFAULT_TAG_MESSAGE
        ),
        _label("tag-message"),
    )
    commitMessage = _validateVersionTemplate(
        (
            _parseNonEmptyString(tablishTable["commit-message"], _label("commit-message"))
            if "commit-message" in tablishTable
            else DEFAULT_COMMIT_MESSAGE
        ),
        _label("commit-message"),
    )

    generatedFiles = (
        _parseGeneratedFiles(tablishTable["generated-files"], keyOrigins["generated-files"])
        if "generated-files" in tablishTable
        else ()
    )

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
        hasPyproject=hasPyproject,
        hasTablishTable=hasTablishTable,
        hasTablishKdl=hasTablishKdl,
        explicitKeys=explicitKeys,
        explicitSources=explicitSources,
        inferredSources=tuple(inferredSources),
    )


def loadConfig(projectRoot: Path) -> TablishConfig:
    """Load and validate tablish configuration from pyproject.toml and/or tablish.kdl in projectRoot."""
    pyprojectPath = projectRoot / "pyproject.toml"
    kdlPath = projectRoot / TABLISH_KDL_FILENAME
    hasPyproject = pyprojectPath.is_file()
    hasKdl = kdlPath.is_file()
    if not hasPyproject and not hasKdl:
        msg = f"Could not find pyproject.toml or {TABLISH_KDL_FILENAME} in {projectRoot}."
        raise ReleaseError(msg)

    rawPyproject: dict[str, t.Any] | None = None
    if hasPyproject:
        try:
            rawPyproject = tomllib.loads(pyprojectPath.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            msg = f"Error loading {pyprojectPath}: {exc}"
            raise ReleaseError(msg) from exc

    rawKdl: kdl.Document | None = None
    if hasKdl:
        try:
            rawKdl = kdl.parse(kdlPath.read_text(encoding="utf-8"))
        except (OSError, kdl.ParseError) as exc:
            msg = f"Error loading {kdlPath}: {exc}"
            raise ReleaseError(msg) from exc

    return parseConfigData(rawPyproject, rawKdl)
