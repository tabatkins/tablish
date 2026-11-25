"""Shared type aliases and typing re-exports for tablish."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final, Literal, Protocol

type BumpType = Literal["break", "feature", "bugfix", "major", "minor", "patch"]
type ConfigSource = Literal["explicit", "inferred", "default"]
type MetadataSource = Literal["explicit", "inferred", "default", "cli", "derived"]
type MetadataStatus = Literal["ok", "warning", "error"]
type Semver = tuple[int, int, int]
type MessageCallback = Callable[[str], None]


class SupportsIsatty(Protocol):
    """Minimal stream protocol for terminal TTY detection."""

    def isatty(self) -> bool:
        """Return True if the stream is connected to an interactive terminal."""


__all__ = [
    "Any",
    "BumpType",
    "Callable",
    "ConfigSource",
    "Final",
    "Literal",
    "Mapping",
    "MessageCallback",
    "MetadataSource",
    "MetadataStatus",
    "Protocol",
    "Semver",
    "Sequence",
    "SupportsIsatty",
]
