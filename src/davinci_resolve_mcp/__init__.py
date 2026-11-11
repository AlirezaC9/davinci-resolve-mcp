"""DaVinci Resolve MCP server."""

from __future__ import annotations


def build():
    from .core import mcp
    from . import (  # noqa: F401  (registers tools)
        tools_color, tools_edit, tools_fusion, tools_media, tools_project, tools_render, tools_script, tools_timeline,
    )
    return mcp


def main() -> None:
    build().run("stdio")
