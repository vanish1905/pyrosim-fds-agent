"""Entrypoint for both the desktop workbench and its MCP stdio service."""

from __future__ import annotations

import sys


def main() -> int:
    # Keep the MCP process independent from Qt. PyInstaller bundles Qt DLLs for
    # the GUI, but MCP clients only need the standard-library server and should
    # not fail if GUI plugins or platform libraries are unavailable.
    if "--mcp-server" in sys.argv[1:]:
        from pyro_agent.mcp_server import main as mcp_main

        mcp_main()
        return 0

    from pyro_agent.desktop import main as desktop_main

    desktop_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
