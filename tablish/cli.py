"""Command-line interface for tablish."""

from __future__ import annotations

import argparse
import importlib.metadata
import os
import subprocess
import sys
from pathlib import Path

from . import t
from .config import ReleaseError
from .release import (
    MetadataItem,
    ReleaseMetadata,
    ReleaseOptions,
    collectReleaseMetadata,
    createRelease,
    parseSemver,
)

_BUMP_TYPES: t.Final[dict[str, t.BumpType]] = {
    "break": "break",
    "major": "major",
    "feature": "feature",
    "minor": "minor",
    "bugfix": "bugfix",
    "patch": "patch",
}

_ANSI_RESET: t.Final[str] = "\033[0m"
_ANSI_BOLD: t.Final[str] = "\033[1m"
_ANSI_DIM: t.Final[str] = "\033[2m"
_ANSI_RED_BOLD: t.Final[str] = "\033[1;31m"
_ANSI_GREEN: t.Final[str] = "\033[32m"
_ANSI_GREEN_BOLD: t.Final[str] = "\033[1;32m"
_ANSI_YELLOW: t.Final[str] = "\033[33m"
_ANSI_MAGENTA_BOLD: t.Final[str] = "\033[1;35m"
_ANSI_CYAN: t.Final[str] = "\033[36m"
_ANSI_CYAN_BOLD: t.Final[str] = "\033[1;36m"


def supportsColor(stream: t.SupportsIsatty | None = None) -> bool:
    """Return True if ANSI color output is supported for stream."""
    if os.environ.get("NO_COLOR"):
        return False
    forceColor = os.environ.get("FORCE_COLOR") or os.environ.get("CLICOLOR_FORCE")
    if forceColor and forceColor != "0":
        return True
    if os.environ.get("CLICOLOR") == "0":
        return False
    if os.environ.get("TERM", "") == "dumb":
        return False
    targetStream: t.SupportsIsatty = sys.stdout if stream is None else stream
    try:
        if not targetStream.isatty():
            return False
    except OSError:
        return False
    if sys.platform == "win32":
        return (
            any(key in os.environ for key in ("WT_SESSION", "ANSICON", "TERM"))
            or os.environ.get("TERM_PROGRAM") == "vscode"
        )
    return True


def _paint(text: str, code: str, *, useColor: bool) -> str:
    """Wrap text in ANSI escape codes when useColor is enabled."""
    if not useColor or not code:
        return text
    return f"{code}{text}{_ANSI_RESET}"


def _formatBadge(item: MetadataItem) -> tuple[str, str]:
    """Return (plainBadgeText, ansiColorCode) for a metadata item's provenance or status."""
    if item.source == "explicit":
        return ("[explicit]", _ANSI_GREEN_BOLD)
    if item.source == "cli":
        label = f"[cli: {item.sourceDetail}]" if item.sourceDetail else "[cli]"
        return (label, _ANSI_MAGENTA_BOLD)
    if item.source == "inferred":
        label = f"[default: {item.sourceDetail}]" if item.sourceDetail else "[default]"
        return (label, _ANSI_CYAN)
    if item.source == "default":
        label = f"[default: {item.sourceDetail}]" if item.sourceDetail else "[default]"
        return (label, _ANSI_DIM)
    if item.status == "error":
        label = f"[error: {item.sourceDetail}]" if item.sourceDetail else "[error]"
        return (label, _ANSI_RED_BOLD)
    if item.status == "warning":
        label = f"[warning: {item.sourceDetail}]" if item.sourceDetail else "[warning]"
        return (label, _ANSI_YELLOW)
    if item.status == "ok":
        label = f"[ok: {item.sourceDetail}]" if item.sourceDetail else "[ok]"
        return (label, _ANSI_GREEN)
    if item.sourceDetail:
        return (f"[{item.sourceDetail}]", _ANSI_CYAN)
    return ("", "")


def formatReleaseMetadata(metadata: ReleaseMetadata, *, useColor: bool = False) -> str:
    """Format resolved publish metadata into aligned, optionally colored sections."""
    keyWidth = max((len(item.key) for item in metadata.items), default=0) + 2
    valueWidth = 38

    lines: list[str] = []
    currentSection: str | None = None
    for item in metadata.items:
        if item.section != currentSection:
            if currentSection is not None:
                lines.append("")
            lines.append(_paint(item.section, _ANSI_CYAN_BOLD, useColor=useColor))
            currentSection = item.section

        paddedKey = item.key.ljust(keyWidth)
        if item.source in ("explicit", "cli"):
            styledKey = _paint(paddedKey, _ANSI_BOLD, useColor=useColor)
        else:
            styledKey = paddedKey

        valueColor = ""
        if item.status == "error":
            valueColor = _ANSI_RED_BOLD
        elif item.status == "warning":
            valueColor = _ANSI_YELLOW

        badgeText, badgeColor = _formatBadge(item)
        if badgeText:
            padSpaces = max(2, valueWidth - len(item.value))
            styledValue = _paint(item.value, valueColor, useColor=useColor)
            styledBadge = _paint(badgeText, badgeColor, useColor=useColor)
            lines.append(f"  {styledKey}{styledValue}{' ' * padSpaces}{styledBadge}")
        else:
            styledValue = _paint(item.value, valueColor, useColor=useColor)
            lines.append(f"  {styledKey}{styledValue}")

        for detail in item.details:
            lines.append(f"  {' ' * keyWidth}- {detail}")

    return "\n".join(lines)


def _getPackageVersion() -> str:
    """Return the installed tablish package version, or fallback to semver.txt."""
    try:
        return importlib.metadata.version("tablish")
    except importlib.metadata.PackageNotFoundError:
        semverFile = Path(__file__).resolve().parent.parent / "semver.txt"
        if semverFile.is_file():
            return semverFile.read_text(encoding="utf-8").strip()
        return "0.0.0"


def _buildArgParser() -> argparse.ArgumentParser:
    """Create the CLI argument parser for tablish."""
    parser = argparse.ArgumentParser(
        prog="tablish",
        description="Release a Python project to PyPI using [tool.tablish] in pyproject.toml.",
    )
    parser.add_argument(
        "target",
        metavar="{break,feature,bugfix,major,minor,patch,MAJOR.MINOR.PATCH,info}",
        help=(
            "Bump type (break/major, feature/minor, bugfix/patch), explicit MAJOR.MINOR.PATCH version, "
            + "or 'info' to print publish metadata."
        ),
    )
    parser.add_argument(
        "--test",
        dest="test",
        action="store_true",
        help="Upload to test.pypi.org instead (rolls back version files and skips git commit/tag/push).",
    )
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Run checks and build/validate artifacts without uploading or pushing to git.",
    )
    parser.add_argument(
        "--skip-lint",
        dest="skip_lint",
        action="store_true",
        help="Skip running the configured lint command before building (still runs check-manifest if enabled).",
    )
    parser.add_argument(
        "--no-tag",
        dest="no_tag",
        action="store_true",
        help="Skip creating and pushing the git release tag.",
    )
    parser.add_argument(
        "--verbose-pypi",
        dest="verbose_pypi",
        action="store_true",
        help="Pass --verbose to `twine upload` for full PyPI responses and error messages.",
    )
    parser.add_argument(
        "-C",
        "--project-dir",
        dest="project_dir",
        type=Path,
        default=None,
        help="Path to the target project root directory (defaults to current working directory).",
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {_getPackageVersion()}",
    )
    return parser


def parseArgs(argv: t.Sequence[str] | None = None) -> tuple[ReleaseOptions, Path | None]:
    """Parse CLI arguments into ReleaseOptions and an optional project directory."""
    parser = _buildArgParser()
    args = parser.parse_args(argv)

    rawTarget: str = args.target
    isInfo = rawTarget == "info"
    bump: t.BumpType | None = None if isInfo else _BUMP_TYPES.get(rawTarget)
    explicitVersion: t.Semver | None = None
    if not isInfo and bump is None:
        try:
            explicitVersion = parseSemver(rawTarget)
        except ReleaseError:
            parser.error(
                f"invalid release target {rawTarget!r}: expected 'break'/'major', "
                + "'feature'/'minor', 'bugfix'/'patch', a MAJOR.MINOR.PATCH version, or 'info'.",
            )

    options = ReleaseOptions(
        bump=bump,
        version=explicitVersion,
        test=bool(args.test),
        dryRun=bool(args.dry_run),
        skipLint=bool(args.skip_lint),
        noTag=bool(args.no_tag),
        verbosePypi=bool(args.verbose_pypi),
        info=isInfo,
    )
    return (options, args.project_dir)


def main(argv: t.Sequence[str] | None = None) -> int:
    """Entry point for the tablish CLI."""
    try:
        options, projectDir = parseArgs(argv)
        if options.info:
            metadata = collectReleaseMetadata(options, projectRoot=projectDir)
            print(formatReleaseMetadata(metadata, useColor=supportsColor(sys.stdout)))
            return 0
        createRelease(options, projectRoot=projectDir, onMessage=print)
    except ReleaseError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"Error: Command failed with exit code {exc.returncode}.", file=sys.stderr)
        return exc.returncode or 1
    except KeyboardInterrupt:
        print("\nRelease cancelled.", file=sys.stderr)
        return 130
    return 0
