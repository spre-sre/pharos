# Pharos MCP Server — Container Getting Started

This guide covers everything an SPRE team member needs to run Pharos as a local Podman or Docker container and connect it from their AI tool. You should be able to go from zero to working queries in under five minutes using the Quick Start section.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Connect from Your AI Tool](#connect-from-your-ai-tool)
- [Multi-Cluster Setup](#multi-cluster-setup)
- [Environment Variables](#environment-variables)
- [Troubleshooting](#troubleshooting)

---

## Prerequisites

You need three things before starting:

**Container runtime.** Either Podman or Docker installed and working. Verify with:

```bash
podman version
# or
docker version
```

**Cluster access.** A kubeconfig that works for your target cluster. Verify with:

```bash
kubectl get namespaces
# or
oc get namespaces
```

If neither command returns results, run `oc login` or update `~/.kube/config` before continuing.

**A token.** Choose any random secret string to use as a bearer token. The container refuses to start when bound to `0.0.0.0` without one. Generate one with:

```bash
openssl rand -hex 32
```

---

## Quick Start

This covers the common case: one cluster, default kubeconfig location, Konflux profile (all 48 tools).

Pull and run the container:

```bash
podman run -d --rm --name pharos \
  -p 8000:8000 \
  -v ~/.kube/config:/opt/app-root/.kube/config:ro \
  -e KUBECONFIG=/opt/app-root/.kube/config \
  -e LUMINO_HTTP_TOKEN=<your-token> \
  quay.io/geored/pharos
```

Replace `<your-token>` with the secret you generated above.

Verify the container is running and healthy:

```bash
podman ps
curl http://127.0.0.1:8000/health
```

The health endpoint returns `200 OK` with no auth required. If it does, the server is ready.

Add Pharos to Claude Code:

```bash
claude mcp add --transport http \
  --header "Authorization: Bearer <your-token>" \
  -s user pharos http://127.0.0.1:8000/mcp
```

Then test it:

```bash
claude "List all namespaces in my cluster"
```

---

## Connect from Your AI Tool

The server speaks MCP over HTTP. All clients connect to `http://127.0.0.1:8000/mcp` with an `Authorization: Bearer <your-token>` header. The snippets below are drop-in configs — substitute your actual token for `<your-token>` in each one.

### Claude Code CLI

**Option A — via CLI command** (sets it globally for your user):

```bash
claude mcp add --transport http \
  --header "Authorization: Bearer <your-token>" \
  -s user pharos http://127.0.0.1:8000/mcp
```

**Option B — via `.mcp.json`** (project-local, checked into the repo):

```json
{
  "mcpServers": {
    "pharos": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp",
      "headers": {"Authorization": "Bearer <your-token>"}
    }
  }
}
```

Place this file in your project root, then restart Claude Code.

### Claude Desktop

Config file location:
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "pharos": {
      "url": "http://127.0.0.1:8000/mcp",
      "headers": {"Authorization": "Bearer <your-token>"}
    }
  }
}
```

Restart Claude Desktop after editing. Look for the hammer icon in the chat window to confirm the tools loaded.

### Cursor

Create `.cursor/mcp.json` in your project root:

```json
{
  "mcpServers": {
    "pharos": {
      "url": "http://127.0.0.1:8000/mcp",
      "headers": {"Authorization": "Bearer <your-token>"}
    }
  }
}
```

### VS Code (GitHub Copilot)

Create `.vscode/mcp.json` in your project root:

```json
{
  "servers": {
    "pharos": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp",
      "headers": {"Authorization": "Bearer <your-token>"}
    }
  }
}
```

### JetBrains IDEs

Go to Settings > Tools > MCP Servers > Add. Set the type to HTTP, the URL to `http://127.0.0.1:8000/mcp`, and add a custom header with key `Authorization` and value `Bearer <your-token>`.

### Windsurf

Config file: `~/.codeium/windsurf/mcp_config.json`

```json
{
  "mcpServers": {
    "pharos": {
      "serverUrl": "http://127.0.0.1:8000/mcp",
      "headers": {"Authorization": "Bearer <your-token>"}
    }
  }
}
```

### Codex

Pass the server URL and header via CLI flags, or add to your `codex.json`:

```json
{
  "mcpServers": {
    "pharos": {
      "url": "http://127.0.0.1:8000/mcp",
      "headers": {"Authorization": "Bearer <your-token>"}
    }
  }
}
```

---

## Multi-Cluster Setup

The default Quick Start gives the AI access to whichever context is active in your kubeconfig at startup. For multi-cluster work — querying staging and production in the same conversation, or switching between clusters on demand — you need to mount a `lumino.yaml` config file that opens the credential allowlist.

### Step 1 — Create lumino.yaml

Create this file anywhere on your host (for example, `~/pharos/lumino.yaml`):

```yaml
profile: konflux

sources:
  kubernetes:
    discover_contexts: true
    credential_ref_roots:
      - /opt/app-root/.kube
  prometheus: {}

extensions:
  konflux: "on"
  openshift: "on"
  tekton: "on"
```

The `credential_ref_roots` entry is what unlocks `connect_cluster`. Without it, every `connect_cluster` call is rejected with `ref_outside_allowlist` regardless of the path you pass.

The path `/opt/app-root/.kube` is the container path — it must match where your kubeconfig lands inside the container, not the host path.

### Step 2 — Run with the config mounted

```bash
podman run -d --rm --name pharos \
  -p 8000:8000 \
  -v ~/.kube/config:/opt/app-root/.kube/config:ro \
  -v ~/pharos/lumino.yaml:/opt/app-root/src/lumino.yaml:ro \
  -e KUBECONFIG=/opt/app-root/.kube/config \
  -e LUMINO_CONFIG=/opt/app-root/src/lumino.yaml \
  -e LUMINO_HTTP_TOKEN=<your-token> \
  quay.io/geored/pharos
```

If you have multiple kubeconfigs, mount the directory instead:

```bash
-v ~/.kube:/opt/app-root/.kube:ro
```

Then set `KUBECONFIG` to the specific file or colon-separated list you want active at startup.

### Step 3 — Work across clusters in conversation

Once the container is running with the config above, your AI tool can discover and switch between clusters through normal conversation:

**Discover what is available:**

> "What clusters are available?"

The AI calls `list_sources`, which shows all kubeconfig contexts discovered at startup. Contexts appear as named lazy instances — no connections are made until you ask for one.

**Connect to a specific cluster:**

> "Connect to the staging cluster"

The AI calls `connect_cluster` with the context name from your kubeconfig:

```
connect_cluster(
  name="staging",
  credential_ref="kubeconfig:/opt/app-root/.kube/config#<context-name>"
)
```

The `credential_ref` path must be the **container** path (`/opt/app-root/.kube/config`), not your host path (`~/.kube/config`). The context name matches exactly what appears in your kubeconfig — check with `kubectl config get-contexts`.

Extension tools (Tekton, OpenShift, Konflux) become available for that named source after `connect_cluster` succeeds.

**Query a connected cluster:**

> "Check pipeline health on staging"

The AI calls tools with `source="staging"` to target that cluster specifically.

**Compare two clusters in one conversation:**

> "Compare pipeline failure rates between staging and production"

The AI queries both sources in the same conversation, using `source="staging"` and `source="production"` in separate tool calls.

---

## Environment Variables

These are the container-relevant variables. Set them with `-e VAR=value` on the `podman run` command. Transport variables are environment-only — they are not configurable through `lumino.yaml`.

| Variable | Required | Default | Description |
|---|---|---|---|
| `LUMINO_HTTP_TOKEN` | Yes (for container) | none | Bearer token for authentication. The container exits at startup if this is missing when `LUMINO_BIND_HOST` is `0.0.0.0`. |
| `KUBECONFIG` | Yes | none | Path to the kubeconfig **inside the container** (e.g. `/opt/app-root/.kube/config`). |
| `LUMINO_BIND_HOST` | No | `0.0.0.0` (baked into image) | Listen address. The image bakes in `0.0.0.0`; override with `127.0.0.1` for no-auth local dev. |
| `LUMINO_BIND_PORT` | No | `8000` | Listen port. If you change this, also update the health check command or the built-in health check will silently fail. |
| `LUMINO_CONFIG` | No | none | Path to `lumino.yaml` **inside the container**. Required for multi-cluster setup. When absent, the built-in `konflux` profile is used (48 tools, single cluster). |
| `KUBEARCHIVE_ENABLED` | No | `true` | Set to `false` to disable KubeArchive discovery. Disabling removes the archived-resource fallback but speeds up startup if KubeArchive is not deployed. |
| `LUMINO_TRANSPORT` | No | `streamable-http` (baked into image) | Transport mode. The image bakes in `streamable-http`. |
| `LUMINO_STATELESS_HTTP` | No | `true` (baked into image) | Stateless mode for container deployments. Appropriate for Kubernetes Route and Ingress deployments. |

---

## Troubleshooting

**Container exits immediately**

The container refused to start because `LUMINO_HTTP_TOKEN` is not set and `LUMINO_BIND_HOST` is `0.0.0.0`. This is intentional — the server is fail-closed on non-localhost binds without a token.

Fix: add `-e LUMINO_HTTP_TOKEN=<your-token>` to the run command.

If you specifically want no-auth local dev (traffic stays on localhost only), override the bind host instead:

```bash
podman run -d --rm --name pharos \
  -p 127.0.0.1:8000:8000 \
  -v ~/.kube/config:/opt/app-root/.kube/config:ro \
  -e KUBECONFIG=/opt/app-root/.kube/config \
  -e LUMINO_BIND_HOST=127.0.0.1 \
  quay.io/geored/pharos
```

**Connection refused on port 8000**

The container is not running, or it crashed after starting. Check:

```bash
podman ps                 # is the container listed?
podman logs pharos        # what did it print before exiting?
```

Also check for port conflicts — if something else is already using port 8000, map to a different host port with `-p 8001:8000` and update your MCP client config to match.

**`ref_outside_allowlist` error on connect_cluster**

`credential_ref_roots` is empty or not set in `lumino.yaml`. The server rejects all path-based credential references unless the path falls inside a declared root.

Fix: add the container kubeconfig path to `credential_ref_roots` in your `lumino.yaml`:

```yaml
sources:
  kubernetes:
    credential_ref_roots:
      - /opt/app-root/.kube
```

Then remount the config file and restart the container.

**"Unknown kubernetes instance" / tools fail with unknown source**

You need to call `connect_cluster` first. Kubeconfig contexts are discovered at startup and shown in `list_sources`, but they are not connected until you explicitly register them. Ask the AI: "Connect to the \<context-name\> cluster."

**Extension tools not available after connect_cluster**

Extension tools (Tekton, OpenShift, Konflux) only activate for a named source after `connect_cluster` succeeds for that source. If `list_sources` shows the source but the tools are not working, confirm that `connect_cluster` returned successfully and that the extensions are set to `"on"` (not `"auto"`) in your `lumino.yaml`.

**Health check**

The `/health` endpoint is always unauthenticated:

```bash
curl http://127.0.0.1:8000/health
```

For in-container documentation:

```bash
podman exec pharos cat /help.1
```
