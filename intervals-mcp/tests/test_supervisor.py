import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import supervisor


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
        self.assertEqual(env["MCP_SERVER_URL"], "http://127.0.0.1:8765/mcp")
        self.assertEqual(env["INTERVALS_ICU_DELETE_MODE"], "safe")

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
            "mcp_http_healthy": True,
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
            "mcp_http_healthy": True,
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
            "mcp_http_healthy": True,
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
        result = {
            "result": {
                "content": [{"type": "text", "text": '{"error":{"type":"api_error"}}'}],
                "isError": False,
            }
        }
        with patch.object(supervisor, "rpc_request") as rpc:
            rpc.side_effect = [
                ({"result": {"protocolVersion": supervisor.MCP_PROTOCOL_VERSION}}, "session-1"),
                (None, "session-1"),
                (result, "session-1"),
            ]
            with self.assertRaisesRegex(ValueError, "Intervals.icu API"):
                supervisor.probe_mcp_tool()

    def test_tool_probe_accepts_a_data_response(self):
        result = {
            "result": {
                "content": [{"type": "text", "text": '{"data":{"profile":{"id":"i123456"}}}'}],
                "isError": False,
            }
        }
        with patch.object(supervisor, "rpc_request") as rpc:
            rpc.side_effect = [
                ({"result": {"protocolVersion": supervisor.MCP_PROTOCOL_VERSION}}, "session-1"),
                (None, "session-1"),
                (result, "session-1"),
            ]
            supervisor.probe_mcp_tool()

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
