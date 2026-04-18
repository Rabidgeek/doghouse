"""Smoke tests that keep pytest happy on an empty scaffold."""

from __future__ import annotations

from doghouse import __version__


def test_version_is_string() -> None:
    """Package exports a semver-like version string."""
    assert isinstance(__version__, str)
    assert __version__.count(".") == 2
