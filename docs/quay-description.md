# Pharos MCP Server

**48 Kubernetes/OpenShift/Tekton SRE tools** accessible from Claude Code, Cursor, VS Code, JetBrains, Windsurf, or any MCP-compatible AI tool. Multi-cluster support — connect to multiple production clusters and query them in the same conversation.

Multi-arch image: **amd64 + arm64** (Intel and Apple Silicon Macs, Linux). Podman/Docker picks the right one automatically — do not force `--platform`. If you pulled before **2026-08-20**, run `podman pull quay.io/geored/pharos-dev:latest` to get the current image.

## Quick Start (2 minutes)

```bash
# 1. Generate a token
TOKEN=$(openssl rand -hex 32)

# 2. Run the container (no --rm: if it ever crashes, logs stay readable)
podman run -d --name pharos \
  -p 8000:8000 \
  -v ~/.kube/config:/opt/app-root/.kube/config:ro \
  -e KUBECONFIG=/opt/app-root/.kube/config \
  -e LUMINO_HTTP_TOKEN=$TOKEN \
  quay.io/geored/pharos-dev

# 3. Add to Claude Code
claude mcp add --transport http \
  --header "Authorization: Bearer $TOKEN" \
  -s user pharos http://127.0.0.1:8000/mcp

# 4. Test it
claude "List all namespaces in my cluster"
```

## Other AI Tools

All clients connect to `http://127.0.0.1:8000/mcp` with `Authorization: Bearer <token>`.

**Cursor** — `.cursor/mcp.json`:
```json
{"mcpServers":{"pharos":{"url":"http://127.0.0.1:8000/mcp","headers":{"Authorization":"Bearer <token>"}}}}
```

**VS Code (Copilot)** — `.vscode/mcp.json`:
```json
{"servers":{"pharos":{"type":"http","url":"http://127.0.0.1:8000/mcp","headers":{"Authorization":"Bearer <token>"}}}}
```

**Claude Desktop** — `~/Library/Application Support/Claude/claude_desktop_config.json`:
```json
{"mcpServers":{"pharos":{"url":"http://127.0.0.1:8000/mcp","headers":{"Authorization":"Bearer <token>"}}}}
```

**JetBrains** — Settings > Tools > MCP Servers > Add HTTP > URL: `http://127.0.0.1:8000/mcp`

**Windsurf** — `~/.codeium/windsurf/mcp_config.json`:
```json
{"mcpServers":{"pharos":{"serverUrl":"http://127.0.0.1:8000/mcp","headers":{"Authorization":"Bearer <token>"}}}}
```

## What's Included (48 tools)

- **Kubernetes Core (5)** — Namespaces, pods, resources, labels, constraints
- **Log Analysis (10)** — Smart summaries, streaming analysis, anomaly detection, semantic search
- **Tekton Pipelines (8)** — PipelineRun/TaskRun listing, logs, failure analysis, tracing
- **Events & Analysis (5)** — Event analytics, progressive analysis, ML-powered correlation
- **Prometheus & Metrics (3)** — PromQL queries, resource forecasting, bottleneck prediction
- **OpenShift (3)** — Cluster operators, machine config pools, etcd logs
- **Topology & Simulation (3)** — Dependency mapping, what-if scenarios
- **Security & Certs (2)** — Certificate health scanning, TLS issue investigation
- **Namespace Investigation (2)** — Adaptive and conservative namespace analysis
- **Predictive & ML (2)** — Failure prediction, training data management
- **Cluster Management (3)** — Multi-cluster connect, source listing, capability refresh
- **KubeArchive (1)** — Archived resource queries (garbage-collected PLRs)
- **Incident & RCA (1)** — Automated root cause analysis

## Multi-Cluster

To query multiple clusters in one session, create a `lumino.yaml`:

```yaml
sources:
  kubernetes:
    credential_ref_roots:
      - /opt/app-root/.kube
  prometheus: {}
extensions:
  konflux: "on"
  openshift: "on"
  tekton: "on"
```

Mount it and set `LUMINO_CONFIG`:

```bash
podman run -d --name pharos \
  -p 8000:8000 \
  -v ~/.kube/config:/opt/app-root/.kube/config:ro \
  -v ~/lumino.yaml:/opt/app-root/src/lumino.yaml:ro \
  -e KUBECONFIG=/opt/app-root/.kube/config \
  -e LUMINO_CONFIG=/opt/app-root/src/lumino.yaml \
  -e LUMINO_HTTP_TOKEN=<token> \
  quay.io/geored/pharos-dev
```

Then ask the AI: *"What clusters are available?"* > *"Connect to staging"* > *"Check pipeline health on staging"*

Gotcha: when the AI calls `connect_cluster`, the `credential_ref` path must be the **container** path (`kubeconfig:/opt/app-root/.kube/config#<context>`), not your host path — and `<context>` must match a name from `list_sources` exactly. Connected clusters live in memory: after a container restart, connect them again.

## Environment Variables

- **`LUMINO_HTTP_TOKEN`** (required) — Bearer auth token. Container exits without it.
- **`KUBECONFIG`** (required) — Kubeconfig path inside container (e.g. `/opt/app-root/.kube/config`)
- **`LUMINO_CONFIG`** (optional) — Path to lumino.yaml for multi-cluster setup
- **`KUBEARCHIVE_ENABLED`** (optional, default `true`) — Set `false` to skip KubeArchive discovery
- **`LUMINO_BIND_HOST`** (optional, default `0.0.0.0`) — Listen address
- **`LUMINO_BIND_PORT`** (optional, default `8000`) — Listen port

## Updating

```bash
podman pull quay.io/geored/pharos-dev:latest
podman rm -f pharos
# then re-run the podman run command from above
```

Reconnect any named clusters afterwards — `connect_cluster` state does not survive a restart.

## Troubleshooting

- **Container exits immediately** — Missing `LUMINO_HTTP_TOKEN`. Add `-e LUMINO_HTTP_TOKEN=<token>`.
- **Exit code 132 (SIGILL)** — You're running a stale image pulled before 2026-08-20 (arm64-only) on an x86_64 machine, or forcing `--platform` to the wrong architecture. Fix: `podman pull` the current multi-arch image and drop any `--platform` flag.
- **`No module named 'mcp.server.fastmcp'`** — Broken build published briefly on 2026-08-19/20. Fix: `podman pull` again (see Updating).
- **Connection refused** — Container not running. Check `podman ps -a` and `podman logs pharos`.
- **`ref_outside_allowlist`** — Need `lumino.yaml` with `credential_ref_roots`. See Multi-Cluster above.
- **KubeArchive "Error connecting" on first query** — Cold-start probe timeout against a freshly connected cluster; retry the query once.
- **Health check** — `curl http://127.0.0.1:8000/health` (no auth needed).
- **In-container docs** — `podman exec pharos cat /help.1`
