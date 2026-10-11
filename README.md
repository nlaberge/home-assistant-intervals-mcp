# Home Assistant Intervals.icu MCP Secure Tunnel

> **Vibe-coded project:** This repository was created with AI assistance. Review and test the code and configuration carefully before using it, especially before enabling write-capable Intervals.icu tools.

A personal Home Assistant app that runs the upstream [Intervals.icu MCP server](https://github.com/hhopke/intervals-icu-mcp) over stateless Streamable HTTP on container loopback, with the official [OpenAI Secure MCP Tunnel client](https://github.com/openai/tunnel-client) forwarding to it. Stateless mode lets the MCP server handle individual calls without depending on a previous initialization request or session. A small compatibility middleware maps the observed incoming MCP protocol header `2026-07-28` to the pinned server's supported `2025-11-25`; JSON-RPC methods and tool arguments are not modified. Other unsupported protocol versions are not rewritten. The tunnel client makes outbound HTTPS connections to OpenAI. The upstream server is used unmodified, retaining its activities, wellness, calendar/planned-workout, and structured workout tools.

This app is designed to reuse an existing OpenAI tunnel ID. It does not create a tunnel, change a ChatGPT connector/plugin, or require a public MCP URL, inbound port forwarding, privileged host access, Docker installation on Home Assistant OS, or changes to existing Mac services.

## Before installation

The source is configured for `nlaberge/home-assistant-intervals-mcp`:

1. Use the existing GitHub repository `nlaberge/home-assistant-intervals-mcp`.
2. Enable GitHub Actions and GitHub Container Registry package publishing for the repository. Set the GHCR package visibility to **public** so Home Assistant can pull it without registry credentials.
3. Publish a GitHub Release whose tag exactly matches `intervals-mcp/config.yaml`'s version (currently `0.1.3`, without a leading `v`). The workflow builds and publishes the `amd64` and `aarch64` images and a multi-architecture manifest. Pull requests and pushes to `main` build without publishing.
4. Add `https://github.com/nlaberge/home-assistant-intervals-mcp` as a custom app repository in Home Assistant: **Settings → Apps → App store → ⋮ → Repositories**.

Home Assistant Green uses the app architecture `aarch64`; the workflow also builds `amd64` for local development. The upstream tunnel-client release publishes Linux ARM64 images. This repository has not been installed or tested on a physical Home Assistant Green; a successful local or GitHub build is not a device test.

## Configure and install

1. Install **Intervals.icu MCP Secure Tunnel** from the custom repository.
   **Important:** Keep only one active tunnel-client connected to this tunnel ID. Stop the tunnel-client on your Mac before starting this app, and keep it stopped while the Home Assistant app uses that tunnel. Do not run both clients concurrently or during overlapping restarts.
2. In the app configuration, enter:
   - **OpenAI tunnel ID**: the exact ID of your existing tunnel. Do not create a replacement.
   - **OpenAI runtime API key**: a runtime key with Tunnels **Read** and **Use** permissions. Do not use an OpenAI admin key.
   - **Intervals.icu API key**.
   - **Intervals.icu athlete ID**.
   - **Intervals.icu delete mode**: leave as `safe` unless you have a deliberate reason to change it. The upstream server controls availability of destructive tools through this setting.
3. Save, then start the app.
4. Confirm that startup logs report both child processes running, the tunnel ready, and a successful MCP read-only tool probe.
5. Use your existing ChatGPT connector/plugin as before. No connector-side URL or tunnel changes are required.

The release workflow reads exact dependency revisions from [`versions.env`](./versions.env) and passes them into the image build:

- Intervals.icu MCP `5.6.0`, source commit `92a630bfc4ffe7906c9bf8d2280c1ea41a561d66`
- OpenAI tunnel-client `v0.0.16`, source commit `5f99daabd4aa4a77049e6d81d54a0d8c18335397`
- uv `0.13.0`

The Intervals package requires Python 3.11 or newer. Its `INTERVALS_ICU_API_KEY` and `INTERVALS_ICU_ATHLETE_ID` environment settings are the supported non-interactive authentication method. Its interactive setup command can write a `.env`, but this app does not use that file: Supervisor app options persist the credentials and the app injects them at runtime. The app never writes those credentials to its persistent data directory.

## Credential handling

The API keys use Home Assistant app `password` schema fields; the tunnel ID and athlete ID are ordinary configuration fields. Home Assistant persists app options across restarts and app upgrades. Password fields are masked in the configuration UI, but app options and Home Assistant backups are not a dedicated encrypted secret vault: restrict access to the Home Assistant host and treat backups as containing credentials.

Secrets are passed to the two processes as environment variables and are not built into the image. The supervisor does not print option values or environment variables and redacts configured key values from child-process log output. Do not paste secrets into issues, logs, or source files. Rotate a credential in its issuing service if it was exposed.

The Intervals.icu MCP server does not provide its own authentication. It listens for stateless Streamable HTTP only on `127.0.0.1:8765` inside the app container; that port is not published by Home Assistant and cannot be reached from the LAN. The tunnel client's health listener also binds to loopback and is not published. The app requires outbound HTTPS to `api.openai.com:443` and outbound Intervals.icu API access.

## Health checks and logs

The image health check and startup logs distinguish:

- `mcp_process`: the separately supervised Intervals.icu MCP server process is running.
- `mcp_http_listener`: the local, loopback-only MCP HTTP listener accepts connections.
- `tunnel_process`: the tunnel-client process is running.
- `tunnel_ready`: tunnel-client reports ready through `/readyz`.
- `control_plane`: tunnel-client's local control-plane health observation, including its `ok`/degraded state.
- `tunnel_mcp`: OpenAI client observation of MCP channel discovery/health. It can remain `unknown` until a request has traversed the existing tunnel.
- `mcp_tool_call`: a successful read-only `icu_get_athlete_profile` MCP tool call to Intervals.icu.

The real tool probe runs every 15 minutes while the MCP listener is available. It sends a read-only athlete-profile tool call directly to the stateless local HTTP server, verifying the MCP request path and Intervals.icu credentials; it does not send a request through the OpenAI tunnel or verify ChatGPT end-to-end. `tunnel_mcp` separately reports tunnel-client MCP channel observations, and the final end-to-end check is a read-only tool call through your existing ChatGPT connector. A failed health result does not automatically restart otherwise-running processes: each service is restarted only if its process exits. This avoids turning temporary OpenAI or Intervals.icu outages into restart loops. Health output contains state and error classes, not API response bodies or secrets.

To inspect logs, open **Settings → Apps → Intervals.icu MCP Secure Tunnel → Logs**. Look for missing-option messages, process exit/restart notices, `tunnel_ready`, `control_plane`, and `MCP read-only tool probe` status. The app log is the supported first diagnostic surface; there is no need to install Docker on Home Assistant OS.

### Troubleshooting

- **App exits immediately with required options empty**: configure all four required IDs/keys in the app options, save, then start it again.
- **Tunnel process runs but is not ready**: verify the tunnel ID is unchanged and correct, the runtime key is valid and has Tunnels Read + Use permissions, and outbound TCP 443 to `api.openai.com` is allowed. Do not use an admin key as the runtime key.
- **MCP process is healthy but the tool probe fails**: verify the Intervals.icu API key and athlete ID, and check that the Intervals.icu API is reachable. The probe is a read-only athlete profile query.
- **Tunnel ready but no MCP observations**: check `mcp_http_listener` and `tunnel_mcp_status`, and ensure the Mac tunnel-client is stopped. `tunnel_mcp_status` can remain `unknown` until a request traverses the existing tunnel.
- **MCP calls arrive without initialization or session state**: version `0.1.3` serves stateless Streamable HTTP, so each request is handled independently; the incoming call does not need to share an earlier initialization session.
- **A child is repeatedly restarted**: inspect the preceding child log lines for its non-secret startup error and confirm the image's dependency pins have not been changed.
- **Credentials appear in a log or backup**: stop the app and rotate the exposed key at its issuer before restarting with the replacement.

## Upgrade and rollback

Dependency changes are explicit; the workflow does not watch upstream releases or update automatically.

1. Change the dependency versions and commit pins in [`versions.env`](./versions.env). The release workflow passes those values to Docker; keep Dockerfile defaults in sync for direct builds.
2. Review upstream release notes, build both architectures, and run the tests. Do not update the app's Home Assistant version until you intend to publish that image.
3. Increment `version` in `intervals-mcp/config.yaml`, push changes, and publish a release with the same tag (no leading `v`). The workflow rejects a release whose tag and app version do not match.
4. Confirm the GHCR multi-architecture manifest is present before updating Home Assistant.
5. In Home Assistant, update the app to the new version and review its logs and health status.

To roll back, restore the previous source pins and app version in Git, publish a new release using a higher app version and those previous pins, and update Home Assistant to that release. Alternatively, select a retained prior app image/version in the app store if available. Do not overwrite a previously published version tag; keep app version increments monotonic. Home Assistant backups include app configuration, but the app does not maintain a local credential file or database.

## Development and tests

Run the app tests from the repository root:

```sh
python3.11 -m unittest discover -s intervals-mcp/tests -v
```

Build a local image on a Docker-capable development machine:

```sh
sh scripts/build-image.sh
```

To test the ARM64 image from an x86_64 machine, use a Buildx/QEMU-enabled Docker setup:

```sh
PLATFORM=linux/arm64 sh scripts/build-image.sh
```

To exercise the app locally through your existing tunnel:

1. Stop the Home Assistant app and ensure the Mac tunnel-client is stopped. Only one client may use this tunnel ID at a time.
2. Copy `docker-options.example.json` to a file outside the repository, for example `/tmp/ha-intervals-mcp-options.json`. Replace the placeholders in the copy with your existing tunnel ID and credentials; leave `intervals_icu_delete_mode` set to `safe`. Do not edit the example with real credentials or paste credentials into chat.
3. Restrict the copy to your account:

   ```sh
   chmod 600 /tmp/ha-intervals-mcp-options.json
   ```

4. Run the locally built image without publishing ports:

   ```sh
   docker run --rm --name intervals-mcp-local \
     --mount type=bind,src=/tmp/ha-intervals-mcp-options.json,dst=/data/options.json,readonly \
     ha-intervals-mcp:local
   ```

5. While the container runs, try one read-only request through the existing ChatGPT connector. Watch the container output for the stateless HTTP server startup, tunnel status, and MCP tool probe result. Press `Ctrl+C` to stop the test, then remove the credential copy and restart the Home Assistant app.

The example file contains placeholders only. The real credential file should remain outside the repository and must never be committed or shared. This validates the app image and live tunnel path, but not installation or operation on a physical Home Assistant Green.
