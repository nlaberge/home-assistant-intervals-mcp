import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import supervisor
import mcp_http


class SupervisorTests(unittest.TestCase):
    def test_environment_maps_options_without_printing_them(self):
        options = {
            "openai_tunnel_id": "tunnel_private",
            "openai_runtime_api_key": "sk-secret-openai",
            "intervals_icu_api_key": "secret-icu",
            "intervals_icu_athlete_id": "i123456",
        }
        env = supervisor.build_environment(options, base={})

        self.assertEqual(env["CONTROL_PLANE_TUNNEL_ID"], "tunnel_private")
        self.assertEqual(env["CONTROL_PLANE_API_KEY"], "sk-secret-openai")
        self.assertEqual(env["INTERVALS_ICU_API_KEY"], "secret-icu")
        self.assertEqual(env["INTERVALS_ICU_ATHLETE_ID"], "i123456")
        self.assertNotIn("MCP_SERVER_URL", env)
        self.assertEqual(env["INTERVALS_ICU_DELETE_MODE"], "safe")

    def test_tunnel_targets_local_stateless_http_server(self):
        command = supervisor.tunnel_command({"HEALTH_LISTEN_ADDR": "127.0.0.1:8766"})
        self.assertEqual(command[0:3], ("/usr/bin/tunnel-client", "run", "--mcp.server-url"))
        self.assertEqual(command[3], "url=http://127.0.0.1:8765/mcp")
        self.assertIn("--mcp.extra-headers", command)
        extra_headers = [
            command[index + 1]
            for index, item in enumerate(command[:-1])
            if item == "--mcp.extra-headers"
        ]
        self.assertEqual(
            extra_headers,
            [
                "Content-Type: application/json",
                "Accept: application/json, text/event-stream",
            ],
        )
        self.assertIn("--mcp.startup-wait-timeout", command)
        self.assertNotIn("--mcp-command", command)
        self.assertNotIn("--mcp.stdio-send-initialized-notification", command)

    def test_mcp_server_command_starts_local_wrapper(self):
        self.assertEqual(
            supervisor.mcp_server_command(),
            ("/opt/intervals/.venv/bin/python", "/opt/intervals/run_mcp_server.py"),
        )

    def test_incoming_protocol_version_is_normalized_to_supported_version(self):
        headers = [
            (b"MCP-Protocol-Version", b"2026-07-28"),
            (b"Accept", b"application/json"),
        ]

        normalized, changed = mcp_http.normalize_protocol_version_headers(headers)

        self.assertTrue(changed)
        self.assertEqual(
            normalized,
            [
                (b"MCP-Protocol-Version", b"2025-11-25"),
                (b"Accept", b"application/json"),
            ],
        )

    def test_unrecognized_protocol_versions_are_not_rewritten(self):
        headers = [(b"mcp-protocol-version", b"2099-01-01")]

        normalized, changed = mcp_http.normalize_protocol_version_headers(headers)

        self.assertFalse(changed)
        self.assertEqual(normalized, headers)

    def test_compatibility_middleware_rewrites_mcp_post_header(self):
        observed_scopes = []

        async def app(scope, receive, send):
            observed_scopes.append(scope)

        middleware = mcp_http.ProtocolVersionCompatibilityMiddleware(app)
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/mcp",
            "headers": [(b"mcp-protocol-version", b"2026-07-28")],
        }
        asyncio.run(middleware(scope, Mock(), Mock()))

        self.assertEqual(
            observed_scopes[0]["headers"],
            [(b"mcp-protocol-version", b"2025-11-25")],
        )

    def test_mcp_listener_health_distinguishes_connection_failure(self):
        with patch.object(supervisor.socket, "create_connection"):
            self.assertTrue(supervisor.mcp_http_listener_healthy())
        with patch.object(supervisor.socket, "create_connection", side_effect=OSError):
            self.assertFalse(supervisor.mcp_http_listener_healthy())

    def test_tunnel_readiness_uses_http_status_without_parsing_body(self):
        response = Mock()
        response.status = 200
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(supervisor, "urlopen", return_value=response):
            self.assertEqual(supervisor.http_status("http://127.0.0.1:8766/readyz"), 200)
        response.read.assert_not_called()

    def test_missing_credentials_fail_without_echoing_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "options.json"
            path.write_text(json.dumps({"openai_runtime_api_key": "never-log-this"}))

            with self.assertRaises(supervisor.ConfigurationError) as error:
                supervisor.load_options(path)

        self.assertNotIn("never-log-this", str(error.exception))
        self.assertIn("openai_tunnel_id", str(error.exception))

    def test_child_output_redacts_all_configured_secrets(self):
        self.assertEqual(
            supervisor.redact(
                "key=secret-one and token=secret-two",
                ("secret-one", "secret-two"),
            ),
            "key=[redacted] and token=[redacted]",
        )

    def test_mcp_tool_call_is_required_for_healthy_status(self):
        state = {
            "mcp_process_running": True,
            "mcp_http_listener": True,
            "tunnel_process_running": True,
            "tunnel_ready": True,
            "tunnel_control_plane_status": "ok",
            "mcp_tool_call_succeeded": False,
            "mcp_tool_checked_at": time.time(),
        }

        self.assertFalse(supervisor.healthcheck_from_state(state))

    def test_all_layers_must_be_healthy(self):
        state = {
            "mcp_process_running": True,
            "mcp_http_listener": True,
            "tunnel_process_running": True,
            "tunnel_ready": True,
            "tunnel_control_plane_status": "ok",
            "mcp_tool_call_succeeded": True,
            "mcp_tool_checked_at": time.time(),
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.json"
            path.write_text(json.dumps(state))
            self.assertTrue(supervisor.healthcheck(path)[0])

    def test_unhealthy_control_plane_is_reported(self):
        state = {
            "mcp_process_running": True,
            "mcp_http_listener": True,
            "tunnel_process_running": True,
            "tunnel_ready": True,
            "tunnel_control_plane_status": "degraded",
            "mcp_tool_call_succeeded": True,
            "mcp_tool_checked_at": time.time(),
        }
        self.assertFalse(supervisor.healthcheck_from_state(state))

    def test_invalid_health_snapshot_is_unhealthy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.json"
            path.write_text("[]")
            healthy, state = supervisor.healthcheck(path)

        self.assertFalse(healthy)
        self.assertEqual(state["status"], "error")

    def test_tool_probe_rejects_an_intervals_error_envelope(self):
        response = Mock()
        response.status = 200
        response.headers = {"Content-Type": "text/event-stream"}
        response.read.return_value = (
            b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"content":'
            b'[{"type":"text","text":"{\\"error\\":{\\"type\\":\\"api_error\\"}}"}],'
            b'"isError":false}}\n\n'
        )
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(supervisor, "urlopen", return_value=response):
            with self.assertRaisesRegex(ValueError, "Intervals.icu API"):
                supervisor.probe_mcp_tool()

    def test_tool_probe_calls_stateless_http_without_initializing(self):
        response = Mock()
        response.status = 200
        response.headers = {"Content-Type": "text/event-stream"}
        response.read.return_value = (
            b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"content":'
            b'[{"type":"text","text":"{\\"data\\":{\\"profile\\":{\\"id\\":\\"i123456\\"}}}"}],'
            b'"isError":false}}\n\n'
        )
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(supervisor, "urlopen", return_value=response) as urlopen:
            supervisor.probe_mcp_tool()

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8765/mcp")
        self.assertEqual(request.method, "POST")
        self.assertEqual(
            [json.loads(request.data)["method"]],
            ["tools/call"],
        )
        self.assertEqual(request.get_header("Mcp-protocol-version"), "2025-11-25")

    def test_pins_match_docker_build_defaults(self):
        root = Path(__file__).resolve().parents[2]
        pins = dict(
            line.split("=", 1)
            for line in (root / "versions.env").read_text().splitlines()
        )
        dockerfile = (root / "intervals-mcp" / "Dockerfile").read_text()

        for name in (
            "INTERVALS_VERSION",
            "INTERVALS_COMMIT",
            "TUNNEL_CLIENT_VERSION",
            "UV_VERSION",
        ):
            self.assertIn(f"ARG {name}={pins[name]}", dockerfile)


if __name__ == "__main__":
    unittest.main()
