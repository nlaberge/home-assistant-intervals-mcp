#!/usr/bin/env python3
"""Run the local MCP server and OpenAI tunnel client with redacted logs."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

OPTIONS_PATH = Path("/data/options.json")
HEALTH_PATH = Path("/run/ha-intervals/health.json")
MCP_URL = "http://127.0.0.1:8765"
TUNNEL_HEALTH_URL = "http://127.0.0.1:8766"
HEALTH_INTERVAL_SECONDS = 30
TOOL_PROBE_INTERVAL_SECONDS = 900
TOOL_PROBE_MAX_AGE_SECONDS = TOOL_PROBE_INTERVAL_SECONDS * 2
MAX_RESTART_DELAY_SECONDS = 60
MCP_PROTOCOL_VERSION = "2025-03-26"

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
            "MCP_SERVER_URL": f"{MCP_URL}/mcp",
            "HEALTH_LISTEN_ADDR": "127.0.0.1:8766",
            "HEALTH_SHOW_DETAILS": "true",
            "LOG_LEVEL": "info",
            "LOG_FORMAT": "json",
        }
    )
    return env


def redact(text: str, secrets: tuple[str, ...]) -> str:
    for secret in sorted((item for item in secrets if item), key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    return text


def decode_json_or_sse(body: bytes, content_type: str) -> dict[str, Any] | None:
    if not body:
        return None
    text = body.decode("utf-8", errors="replace")
    if "text/event-stream" in content_type:
        for line in text.splitlines():
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload and payload != "[DONE]":
                    parsed = json.loads(payload)
                    if isinstance(parsed, dict):
                        return parsed
        raise ValueError("MCP event stream did not contain a JSON response")
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("MCP response is not a JSON object")
    return parsed


def rpc_request(
    method: str,
    request_id: int | None = None,
    params: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        payload["id"] = request_id
    if params is not None:
        payload["params"] = params

    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    request = Request(
        f"{MCP_URL}/mcp",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urlopen(request, timeout=25) as response:
        body = response.read(1_048_576)
        session = response.headers.get("Mcp-Session-Id") or session_id
        result = decode_json_or_sse(body, response.headers.get("Content-Type", ""))
        return result, session


def probe_mcp_tool() -> None:
    initialized, session_id = rpc_request(
        "initialize",
        request_id=1,
        params={
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "ha-intervals-healthcheck", "version": "1.0.0"},
        },
    )
    if not initialized or "error" in initialized:
        raise ValueError("MCP initialize failed")

    rpc_request("notifications/initialized", params={}, session_id=session_id)
    result, _ = rpc_request(
        "tools/call",
        request_id=2,
        params={"name": "icu_get_athlete_profile", "arguments": {}},
        session_id=session_id,
    )
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
        state.get("mcp_http_healthy") is True,
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
        if name == "mcp":
            process_state["mcp_generation"] = process_state.get("mcp_generation", 0) + 1
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


async def monitor_health(process_state: dict[str, Any], stop: asyncio.Event) -> None:
    previous_summary: tuple[Any, ...] | None = None
    last_tool_attempt = 0.0
    observed_mcp_generation = process_state.get("mcp_generation", 0)
    state: dict[str, Any] = {
        "mcp_process_running": False,
        "mcp_http_healthy": False,
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
        current_mcp_generation = process_state.get("mcp_generation", 0)
        if current_mcp_generation != observed_mcp_generation:
            observed_mcp_generation = current_mcp_generation
            last_tool_attempt = 0.0
            state["mcp_tool_call_succeeded"] = False
            state["mcp_tool_checked_at"] = None
            state["mcp_tool_error"] = "not_checked"

        state["mcp_process_running"] = process_state.get("mcp", False)
        state["tunnel_process_running"] = process_state.get("tunnel", False)

        try:
            status, mcp_health = await asyncio.to_thread(http_json, f"{MCP_URL}/health")
            state["mcp_http_healthy"] = status == 200 and mcp_health is not None and mcp_health.get("status") == "ok"
        except (OSError, URLError, HTTPError, ValueError, TimeoutError):
            state["mcp_http_healthy"] = False

        try:
            status, _ = await asyncio.to_thread(http_json, f"{TUNNEL_HEALTH_URL}/readyz")
            state["tunnel_ready"] = status == 200
        except (OSError, URLError, HTTPError, ValueError, TimeoutError):
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
        if now - last_tool_attempt >= TOOL_PROBE_INTERVAL_SECONDS and state["mcp_http_healthy"]:
            last_tool_attempt = now
            try:
                await asyncio.to_thread(probe_mcp_tool)
            except (OSError, URLError, HTTPError, ValueError, TimeoutError) as exc:
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
            state["mcp_http_healthy"],
            state["tunnel_process_running"],
            state["tunnel_ready"],
            state["tunnel_control_plane_status"],
            state["tunnel_mcp_status"],
            state["mcp_tool_call_succeeded"],
        )
        if summary != previous_summary:
            LOGGER.info(
                "Health: mcp_process=%s mcp_http=%s tunnel_process=%s tunnel_ready=%s "
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

    mcp_command = (
        "/opt/intervals/.venv/bin/python",
        "-m",
        "intervals_icu_mcp.server",
        "--transport",
        "http",
        "--host",
        "127.0.0.1",
        "--port",
        "8765",
    )
    tunnel_command = (
        "/usr/bin/tunnel-client",
        "run",
        "--mcp.server-url",
        env["MCP_SERVER_URL"],
        "--health.listen-addr",
        env["HEALTH_LISTEN_ADDR"],
        "--health.show-details=true",
    )

    LOGGER.info("Starting private Intervals.icu MCP and OpenAI Secure MCP Tunnel")
    LOGGER.info("MCP transport is HTTP on loopback only; no inbound app port is published")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    process_state: dict[str, Any] = {"mcp": False, "tunnel": False, "mcp_generation": 0}
    services = (
        asyncio.create_task(run_service("mcp", mcp_command, env, secrets, process_state, stop)),
        asyncio.create_task(run_service("tunnel", tunnel_command, env, secrets, process_state, stop)),
        asyncio.create_task(monitor_health(process_state, stop)),
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
