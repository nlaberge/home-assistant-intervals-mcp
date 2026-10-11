import logging

LOGGER = logging.getLogger("ha-intervals.mcp-http")
LOGGER.setLevel(logging.INFO)
INCOMING_PROTOCOL_VERSION = b"2026-07-28"
SUPPORTED_PROTOCOL_VERSION = b"2025-11-25"


def normalize_protocol_version_headers(
    headers: list[tuple[bytes, bytes]],
) -> tuple[list[tuple[bytes, bytes]], bool]:
    normalized = []
    changed = False
    for name, value in headers:
        if name.lower() == b"mcp-protocol-version" and value == INCOMING_PROTOCOL_VERSION:
            value = SUPPORTED_PROTOCOL_VERSION
            changed = True
        normalized.append((name, value))
    return normalized, changed


class ProtocolVersionCompatibilityMiddleware:
    def __init__(self, app):
        self.app = app
        self.reported = False

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and scope.get("path") == "/mcp"
        ):
            headers, changed = normalize_protocol_version_headers(scope.get("headers", []))
            if changed:
                scope = {**scope, "headers": headers}
                if not self.reported:
                    LOGGER.info(
                        "Normalized MCP protocol version %s to %s for the upstream server",
                        INCOMING_PROTOCOL_VERSION.decode(),
                        SUPPORTED_PROTOCOL_VERSION.decode(),
                    )
                    self.reported = True
        await self.app(scope, receive, send)
