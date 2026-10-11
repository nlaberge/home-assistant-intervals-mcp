#!/usr/bin/env python3
"""Run the local MCP server and OpenAI tunnel client with redacted logs."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

OPTIONS_PATH = Path("/data/options.json")
HEALTH_PATH = Path("/run/ha-intervals/health.json")
TUNNEL_HEALTH_URL = "http://127.0.0.1:8766"
MCP_HTTP_URL = "http://127.0.0.1:8765/mcp"
MCP_HTTP_HOST = "127.0.0.1"
MCP_HTTP_PORT = 8765
HEALTH_INTERVAL_SECONDS = 30
TOOL_PROBE_INTERVAL_SECONDS = 900
TOOL_PROBE_MAX_AGE_SECONDS = TOOL_PROBE_INTERVAL_SECONDS * 2
MAX_RESTART_DELAY_SECONDS = 60
MCP_PROTOCOL_VERSION = "2025-11-25"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
LOGGER = logging.getLogger("ha-intervals")


class ConfigurationError(ValueError):
    """An app option is missing or invalid."""


def load_options(path: Path = OPTIONS_PATH) -> dict[str, Any]:
    try:
        options = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError("Home Assistant app options file is missing") from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError("Home Assistant app options file is invalid JSON") from exc

    if not isinstance(options, dict):
        raise ConfigurationError("Home Assistant app options must be a JSON object")

    required = (
        "openai_tunnel_id",
        "openai_runtime_api_key",
        "intervals_icu_api_key",
        "intervals_icu_athlete_id",
    )
    missing = [key for key in required if not isinstance(options.get(key), str) or not options[key].strip()]
    if missing:
        raise ConfigurationError(f"Required app options are empty: {', '.join(missing)}")
    return options


def build_environment(options: dict[str, Any], base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    env.update(
        {
            "CONTROL_PLANE_TUNNEL_ID": options["openai_tunnel_id"],
            "CONTROL_PLANE_API_KEY": options["openai_runtime_api_key"],
            "INTERVALS_ICU_API_KEY": options["intervals_icu_api_key"],
            "INTERVALS_ICU_ATHLETE_ID": options["intervals_icu_athlete_id"],
            "INTERVALS_ICU_DELETE_MODE": options.get("intervals_icu_delete_mode", "safe"),
            "HEALTH_LISTEN_ADDR": "127.0.0.1:8766",
            "HEALTH_SHOW_DETAILS": "true",
            "LOG_LEVEL": "info",
            "LOG_FORMAT": "json",
        }
    )
    return env


def tunnel_command(env: dict[str, str]) -> tuple[str, ...]:
    return (
        "/usr/bin/tunnel-client",
        "run",
        "--mcp.server-url",
        f"url={MCP_HTTP_URL}",
        "--mcp.extra-headers",
        "Content-Type: application/json",
        "--mcp.extra-headers",
        "Accept: application/json, text/event-stream",
        "--mcp.startup-wait-timeout",
        "60s",
        "--health.listen-addr",
        env["HEALTH_LISTEN_ADDR"],
        "--health.show-details=true",
    )


def mcp_server_command() -> tuple[str, ...]:
    return ("/opt/intervals/.venv/bin/python", "/opt/intervals/run_mcp_server.py")


def mcp_http_listener_healthy() -> bool:
    try:
        with socket.create_connection((MCP_HTTP_HOST, MCP_HTTP_PORT), timeout=2):
            return True
    except OSError:
        return False


def redact(text: str, secrets: tuple[str, ...]) -> str:
    for secret in sorted((item for item in secrets if item), key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    return text


def validate_tool_response(result: dict[str, Any] | None) -> None:
    if not result or "error" in result:
        raise ValueError("MCP tool call failed")
    tool_result = result.get("result")
    if not isinstance(tool_result, dict) or tool_result.get("isError") is True:
        raise ValueError("MCP tool returned an error")

    content = tool_result.get("content", [])
    if not isinstance(content, list) or not content:
        raise ValueError("MCP tool returned no content")
    successful_payload = False
    for item in content:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        try:
            decoded = json.loads(item["text"])
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict) and "error" in decoded:
            raise ValueError("Intervals.icu API returned a tool error")
        if isinstance(decoded, dict) and isinstance(decoded.get("data"), dict):
            successful_payload = True
    if not successful_payload:
        raise ValueError("MCP tool did not return a successful data response")


def probe_mcp_tool() -> None:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "icu_get_athlete_profile", "arguments": {}},
    }
    request = Request(
        MCP_HTTP_URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        },
    )
    try:
        with urlopen(request, timeout=25) as response:
            if response.status != 200:
                raise ValueError("MCP HTTP health probe returned an unexpected status")
            content_type = response.headers.get("Content-Type", "")
            body = response.read(1_048_576).decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise ValueError("MCP HTTP health probe failed") from None

    if "text/event-stream" in content_type:
        data_lines = [line[5:].lstrip() for line in body.splitlines() if line.startswith("data:")]
        body = "\n".join(data_lines)
    try:
        result = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError("MCP HTTP health probe returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise ValueError("MCP HTTP health probe returned an invalid response")
    validate_tool_response(result)


def http_json(url: str, timeout: float = 5) -> tuple[int, dict[str, Any] | None]:
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read(1_048_576)
            if not body:
                return response.status, None
            data = json.loads(body)
            return response.status, data if isinstance(data, dict) else None
    except HTTPError as exc:
        return exc.code, None


def http_status(url: str, timeout: float = 5) -> int:
    request = Request(url)
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status
    except HTTPError as exc:
        return exc.code


def write_health(state: dict[str, Any]) -> None:
    HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = HEALTH_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    temporary.replace(HEALTH_PATH)


def healthcheck(path: Path = HEALTH_PATH) -> tuple[bool, dict[str, Any]]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, {"status": "starting", "detail": "No complete health snapshot yet"}

    if not isinstance(state, dict):
        return False, {"status": "error", "detail": "Health snapshot is not an object"}
    return healthcheck_from_state(state), state


def healthcheck_from_state(state: dict[str, Any]) -> bool:
    last_tool_check = state.get("mcp_tool_checked_at")
    tool_fresh = False
    if isinstance(last_tool_check, (int, float)):
        tool_fresh = time.time() - last_tool_check <= TOOL_PROBE_MAX_AGE_SECONDS
    required = (
        state.get("mcp_process_running") is True,
        state.get("mcp_http_listener") is True,
        state.get("tunnel_process_running") is True,
        state.get("tunnel_ready") is True,
        state.get("tunnel_control_plane_status") == "ok",
        state.get("mcp_tool_call_succeeded") is True,
        tool_fresh,
    )
    return all(required)


async def read_child_logs(name: str, process: asyncio.subprocess.Process, secrets: tuple[str, ...]) -> None:
    assert process.stdout is not None
    while line := await process.stdout.readline():
        text = line.decode("utf-8", errors="replace").rstrip()
        LOGGER.info("%s: %s", name, redact(text, secrets)[:4096])


async def run_service(
    name: str,
    command: tuple[str, ...],
    env: dict[str, str],
    secrets: tuple[str, ...],
    process_state: dict[str, Any],
    stop: asyncio.Event,
) -> None:
    delay = 1
    while not stop.is_set():
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                limit=65_536,
            )
        except OSError as exc:
            LOGGER.error("%s failed to start (%s); retrying in %ss", name, type(exc).__name__, delay)
            process_state[name] = False
            await wait_or_stop(stop, delay)
            delay = min(delay * 2, MAX_RESTART_DELAY_SECONDS)
            continue

        process_state[name] = True
        generation_key = f"{name}_generation"
        process_state[generation_key] = process_state.get(generation_key, 0) + 1
        LOGGER.info("%s process started", name)
        log_task = asyncio.create_task(read_child_logs(name, process, secrets))
        wait_task = asyncio.create_task(process.wait())
        stop_task = asyncio.create_task(stop.wait())
        _, pending = await asyncio.wait(
            {wait_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()

        if stop.is_set():
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            await log_task
            process_state[name] = False
            return

        await log_task
        return_code = process.returncode
        process_state[name] = False
        LOGGER.warning("%s exited with status %s; restarting in %ss", name, return_code, delay)
        await wait_or_stop(stop, delay)
        delay = min(delay * 2, MAX_RESTART_DELAY_SECONDS)


async def wait_or_stop(stop: asyncio.Event, delay: int) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=delay)
    except TimeoutError:
        pass


async def monitor_health(
    process_state: dict[str, Any],
    env: dict[str, str],
    stop: asyncio.Event,
) -> None:
    previous_summary: tuple[Any, ...] | None = None
    last_tool_attempt = 0.0
    observed_tunnel_generation = process_state.get("tunnel_generation", 0)
    observed_mcp_generation = process_state.get("mcp_generation", 0)
    state: dict[str, Any] = {
        "mcp_process_running": False,
        "mcp_http_listener": False,
        "tunnel_process_running": False,
        "tunnel_ready": False,
        "tunnel_control_plane_status": "unknown",
        "tunnel_mcp_status": "unknown",
        "mcp_tool_call_succeeded": False,
        "mcp_tool_checked_at": None,
        "mcp_tool_error": "not_checked",
        "updated_at": datetime.now(UTC).isoformat(),
    }

    while not stop.is_set():
        current_tunnel_generation = process_state.get("tunnel_generation", 0)
        current_mcp_generation = process_state.get("mcp_generation", 0)
        if (
            current_tunnel_generation != observed_tunnel_generation
            or current_mcp_generation != observed_mcp_generation
        ):
            observed_tunnel_generation = current_tunnel_generation
            observed_mcp_generation = current_mcp_generation
            last_tool_attempt = 0.0
            state["mcp_tool_call_succeeded"] = False
            state["mcp_tool_checked_at"] = None
            state["mcp_tool_error"] = "not_checked"

        state["mcp_process_running"] = process_state.get("mcp", False)
        state["mcp_http_listener"] = await asyncio.to_thread(mcp_http_listener_healthy)
        state["tunnel_process_running"] = process_state.get("tunnel", False)

        try:
            status = await asyncio.to_thread(http_status, f"{TUNNEL_HEALTH_URL}/readyz")
            state["tunnel_ready"] = status == 200
        except (OSError, URLError, HTTPError, TimeoutError):
            state["tunnel_ready"] = False

        try:
            status, control_plane = await asyncio.to_thread(
                http_json,
                f"{TUNNEL_HEALTH_URL}/health/control-plane",
            )
            if status == 200 and control_plane:
                state["tunnel_control_plane_status"] = str(control_plane.get("status", "unknown"))
            else:
                state["tunnel_control_plane_status"] = f"http_{status}"
        except (OSError, URLError, HTTPError, ValueError, TimeoutError) as exc:
            state["tunnel_control_plane_status"] = type(exc).__name__

        try:
            status, mcp_component = await asyncio.to_thread(
                http_json,
                f"{TUNNEL_HEALTH_URL}/health/mcp",
            )
            if status == 200 and mcp_component:
                state["tunnel_mcp_status"] = str(mcp_component.get("status", "unknown"))
            else:
                state["tunnel_mcp_status"] = f"http_{status}"
        except (OSError, URLError, HTTPError, ValueError, TimeoutError) as exc:
            state["tunnel_mcp_status"] = type(exc).__name__

        now = time.time()
        if now - last_tool_attempt >= TOOL_PROBE_INTERVAL_SECONDS and state["mcp_http_listener"]:
            last_tool_attempt = now
            try:
                await asyncio.to_thread(probe_mcp_tool)
            except (
                OSError,
                URLError,
                HTTPError,
                ValueError,
                TimeoutError,
            ) as exc:
                state["mcp_tool_call_succeeded"] = False
                state["mcp_tool_error"] = type(exc).__name__
                LOGGER.warning("MCP read-only tool probe failed (%s)", type(exc).__name__)
            else:
                state["mcp_tool_call_succeeded"] = True
                state["mcp_tool_error"] = None
                LOGGER.info("MCP read-only tool probe succeeded")
            state["mcp_tool_checked_at"] = time.time()

        state["updated_at"] = datetime.now(UTC).isoformat()
        write_health(state)
        summary = (
            state["mcp_process_running"],
            state["mcp_http_listener"],
            state["tunnel_process_running"],
            state["tunnel_ready"],
            state["tunnel_control_plane_status"],
            state["tunnel_mcp_status"],
            state["mcp_tool_call_succeeded"],
        )
        if summary != previous_summary:
            LOGGER.info(
                "Health: mcp_process=%s mcp_http_listener=%s tunnel_process=%s tunnel_ready=%s "
                "control_plane=%s tunnel_mcp=%s mcp_tool_call=%s",
                *summary,
            )
            previous_summary = summary

        await wait_or_stop(stop, HEALTH_INTERVAL_SECONDS)


async def run() -> int:
    try:
        options = load_options()
    except ConfigurationError as exc:
        LOGGER.error("%s", exc)
        return 2

    env = build_environment(options)
    secrets = (
        options["openai_runtime_api_key"],
        options["intervals_icu_api_key"],
        options["openai_tunnel_id"],
    )
    delete_mode = options.get("intervals_icu_delete_mode", "safe")
    if delete_mode not in {"safe", "full", "none"}:
        LOGGER.error("intervals_icu_delete_mode must be safe, full, or none")
        return 2

    client_command = tunnel_command(env)

    LOGGER.info("Starting stateless HTTP Intervals.icu MCP server and OpenAI Secure MCP Tunnel")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    process_state: dict[str, Any] = {
        "mcp": False,
        "mcp_generation": 0,
        "tunnel": False,
        "tunnel_generation": 0,
    }
    services = (
        asyncio.create_task(run_service("mcp", mcp_server_command(), env, secrets, process_state, stop)),
        asyncio.create_task(run_service("tunnel", client_command, env, secrets, process_state, stop)),
        asyncio.create_task(monitor_health(process_state, env, stop)),
    )
    await stop.wait()
    LOGGER.info("Shutting down app processes")
    await asyncio.gather(*services)
    return 0


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "healthcheck":
        healthy, state = healthcheck()
        print(json.dumps(state, sort_keys=True))
        return 0 if healthy else 1
    if len(sys.argv) != 2 or sys.argv[1] != "run":
        print("Usage: supervisor.py run|healthcheck", file=sys.stderr)
        return 2
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
