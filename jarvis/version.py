"""Version and build identity for JARVIS.

The version triple follows the owner's `0.0.x` identifier convention where the
first number is a major capability gate, the second a scope revision and the
third a build. `BUILD_STAMP` is stamped at package time by scripts/build.
"""

from __future__ import annotations

__all__ = ["VERSION", "CODENAME", "BUILD_STAMP", "banner"]

VERSION = "0.1.0"

# Internal codenames for capability gates. Not release names.
CODENAME = "FOUNDATION"

BUILD_STAMP = "dev"


def banner() -> str:
    return f"J.A.R.V.I.S. {VERSION} ({CODENAME}) build {BUILD_STAMP}"
