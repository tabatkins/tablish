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
from .info import MetadataItem, ReleaseMetadata, collectReleaseMetadata
from .release import ReleaseOptions, createRelease, parseSemver, resumeRelease

_BUMP_TYPES: t.Final[dict[str, t.BumpType]] = {
    "break": "break",
    "major": "major",
    "feature": "feature",
    "minor": "minor",
    "bugfix": "bugfix",
    "patch": "patch",
}

_VALUE_COLUMN_WIDTH: t.Final[int] = 38

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

_SOURCE_BADGE_STYLES: t.Final[dict[t.MetadataSource, tuple[str, str]]] = {
    "explicit": ("explicit", _ANSI_GREEN_BOLD),
    "cli": ("cli", _ANSI_MAGENTA_BOLD),
    "inferred": ("default", _ANSI_CYAN),
    "default": ("default", _ANSI_DIM),
}
_STATUS_BADGE_STYLES: t.Final[dict[t.MetadataStatus, tuple[str, str]]] = {
    "error": ("error", _ANSI_RED_BOLD),
    "warning": ("warning", _ANSI_YELLOW),
    "ok": ("ok", _ANSI_GREEN),
}


def supportsColor(stream: t.SupportsIsatty | None = None) -> bool:
    """Return True if ANSI color output is supported for stream."""
    if os.environ.get("NO_COLOR"):
        return False
    for envVar in ("FORCE_COLOR", "CLICOLOR_FORCE"):
        forceVal = os.environ.get(envVar)
        if forceVal and forceVal != "0":
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


def _badgeLabel(kind: str, detail: str | None) -> str:
    """Format a bracketed badge label with optional detail suffix."""
    return f"[{kind}: {detail}]" if detail else f"[{kind}]"


def _formatBadge(item: MetadataItem) -> tuple[str, str]:
    """Return (plainBadgeText, ansiColorCode) for a metadata item's provenance or status."""
    sourceStyle = _SOURCE_BADGE_STYLES.get(item.source)
    if sourceStyle is not None:
        kind, color = sourceStyle
        return (_badgeLabel(kind, item.sourceDetail), color)
    if item.status is not None:
        kind, color = _STATUS_BADGE_STYLES[item.status]
        return (_badgeLabel(kind, item.sourceDetail), color)
    if item.sourceDetail:
        return (f"[{item.sourceDetail}]", _ANSI_CYAN)
    return ("", "")


def formatReleaseMetadata(metadata: ReleaseMetadata, *, useColor: bool = False) -> str:
    """Format resolved publish metadata into aligned, optionally colored sections."""
    keyWidth = max((len(item.key) for item in metadata.items), default=0) + 2

    lines: list[str] = []
    currentSection: str | None = None
    for item in metadata.items:
        if item.section != currentSection:
            if currentSection is not None:
                lines.append("")
            lines.append(_paint(item.section, _ANSI_CYAN_BOLD, useColor=useColor))
            currentSection = item.section

        paddedKey = item.key.ljust(keyWidth)
        styledKey = (
            _paint(paddedKey, _ANSI_BOLD, useColor=useColor) if item.source in ("explicit", "cli") else paddedKey
        )

        valueColor = ""
        if item.status == "error":
            valueColor = _ANSI_RED_BOLD
        elif item.status == "warning":
            valueColor = _ANSI_YELLOW
        styledValue = _paint(item.value, valueColor, useColor=useColor)

        badgeText, badgeColor = _formatBadge(item)
        if badgeText:
            padSpaces = max(2, _VALUE_COLUMN_WIDTH - len(item.value))
            styledBadge = _paint(badgeText, badgeColor, useColor=useColor)
            lines.append(f"  {styledKey}{styledValue}{' ' * padSpaces}{styledBadge}")
        else:
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
        description="Release a Python project to PyPI using tablish.kdl and/or [tool.tablish] in pyproject.toml.",
    )
    parser.add_argument(
        "target",
        metavar="{break,feature,bugfix,major,minor,patch,MAJOR.MINOR.PATCH,info,resume}",
        help=(
            "Bump type (break/major, feature/minor, bugfix/patch), explicit MAJOR.MINOR.PATCH version, "
            + "'info' to print publish metadata, or 'resume' to finish an interrupted upload or git release."
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
    isInfo = False
    isResume = False
    bump: t.BumpType | None = None
    explicitVersion: t.Semver | None = None

    if rawTarget == "info":
        isInfo = True
    elif rawTarget == "resume":
        isResume = True
        if args.test:
            parser.error("cannot combine 'resume' with --test.")
    elif rawTarget in _BUMP_TYPES:
        bump = _BUMP_TYPES[rawTarget]
    else:
        try:
            explicitVersion = parseSemver(rawTarget)
        except ReleaseError:
            parser.error(
                f"invalid release target {rawTarget!r}: expected 'break'/'major', "
                + "'feature'/'minor', 'bugfix'/'patch', a MAJOR.MINOR.PATCH version, 'info', or 'resume'.",
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
        resume=isResume,
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
        if options.resume:
            resumeRelease(options, projectRoot=projectDir, onMessage=print)
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
