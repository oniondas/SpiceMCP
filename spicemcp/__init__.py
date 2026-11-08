"""SpiceMCP -- LTspice MCP server with persistent optimization state."""

from . import asc, db, engine, ir, raw, sim  # noqa: F401

__all__ = ["asc", "db", "engine", "ir", "raw", "sim"]
try:                                   # one source of truth: pyproject.toml
    from importlib.metadata import PackageNotFoundError, version
    __version__ = version("spicemcp")
except PackageNotFoundError:           # running from a checkout that is not installed
    __version__ = "0+unknown"
