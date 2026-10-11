from io import StringIO
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

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
        self.assertNotIn("MCP_SERVER_URL", env)
        self.assertEqual(env["INTERVALS_ICU_DELETE_MODE"], "safe")

    def test_tunnel_runs_mcp_server_over_stdio(self):
        command = supervisor.tunnel_command({"HEALTH_LISTEN_ADDR": "127.0.0.1:8766"})
        self.assertEqual(command[0:3], ("/usr/bin/tunnel-client", "run", "--mcp-command"))
        self.assertEqual(
            command[3],
            "/opt/intervals/.venv/bin/python -m intervals_icu_mcp.server --transport stdio",
        )
        self.assertIn("--mcp.stdio-send-initialized-notification", command)
        self.assertNotIn("--mcp.server-url", command)

    def test_mcp_component_reports_managed_stdio_child(self):
        self.assertEqual(
            supervisor.mcp_process_health(
                {"status": "ok", "details": {"child_state": "running"}}
            ),
            (True, True),
        )
        self.assertEqual(
            supervisor.mcp_process_health(
                {"status": "degraded", "details": {"child_state": "running"}}
            ),
            (True, False),
        )
        self.assertEqual(
            supervisor.mcp_process_health({"status": "unknown", "details": {}}),
            (False, False),
        )

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
            "mcp_stdio_healthy": True,
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
            "mcp_stdio_healthy": True,
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
            "mcp_stdio_healthy": True,
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
        stdout = StringIO(
            "\n".join(
                (
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "result": {"protocolVersion": "2025-03-26"},
                        }
                    ),
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "result": {
                                "content": [
                                    {"type": "text", "text": '{"error":{"type":"api_error"}}'}
                                ],
                                "isError": False,
                            },
                        }
                    ),
                )
            )
        )
        process = Mock(stdin=Mock(), stdout=stdout)
        process.wait.return_value = 0
        process.returncode = 0
        with patch.object(supervisor.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(ValueError, "Intervals.icu API"):
                supervisor.probe_mcp_tool(
                    {
                        "INTERVALS_ICU_API_KEY": "secret",
                        "CONTROL_PLANE_API_KEY": "openai-secret",
                    }
                )

    def test_tool_probe_accepts_a_data_response(self):
        stdout = StringIO(
            "\n".join(
                (
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "result": {"protocolVersion": "2025-03-26"},
                        }
                    ),
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "result": {
                                "content": [
                                    {
                                        "type": "text",
                                        "text": '{"data":{"profile":{"id":"i123456"}}}',
                                    }
                                ],
                                "isError": False,
                            },
                        }
                    ),
                )
            )
        )
        process = Mock(stdin=Mock(), stdout=stdout)
        process.wait.return_value = 0
        process.returncode = 0
        with patch.object(supervisor.subprocess, "Popen", return_value=process) as popen:
            supervisor.probe_mcp_tool(
                {
                    "PATH": "/usr/bin",
                    "INTERVALS_ICU_API_KEY": "secret",
                    "INTERVALS_ICU_ATHLETE_ID": "i123456",
                    "CONTROL_PLANE_API_KEY": "openai-secret",
                }
            )
        command = popen.call_args.args[0]
        self.assertEqual(command[-1], "stdio")
        self.assertEqual(
            popen.call_args.kwargs["env"],
            {
                "PATH": "/usr/bin",
                "INTERVALS_ICU_API_KEY": "secret",
                "INTERVALS_ICU_ATHLETE_ID": "i123456",
            },
        )
        sent_messages = [json.loads(call.args[0]) for call in process.stdin.write.call_args_list]
        self.assertEqual(
            [message.get("method") for message in sent_messages],
            ["initialize", "notifications/initialized", "tools/call"],
        )

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
