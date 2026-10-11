#!/usr/bin/env python3
from starlette.middleware import Middleware

from intervals_icu_mcp.server import mcp

from mcp_http import ProtocolVersionCompatibilityMiddleware


if __name__ == "__main__":
    mcp.run(
        transport="http",
        host="127.0.0.1",
        port=8765,
        path="/mcp",
        stateless_http=True,
        show_banner=False,
        middleware=[Middleware(ProtocolVersionCompatibilityMiddleware)],
    )
