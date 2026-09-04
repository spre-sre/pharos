# Pharos MCP Server

**Multi-cluster SRE diagnostics for Kubernetes, OpenShift, and Tekton — 49 read-only tools served over the Model Context Protocol.** Connect Claude Code, Cursor, VS Code, JetBrains, Windsurf, or any MCP-compatible AI client, and investigate your whole fleet in one conversation.

- **Source:** <https://github.com/spre-sre/pharos> · Apache-2.0 · derived from [lumino-mcp-server](https://github.com/spre-sre/lumino-mcp-server) (see NOTICE)
- **Releases:** this repository. Built by CI from tagged releases — [changelog](https://github.com/spre-sre/pharos/blob/main/CHANGELOG.md)
- **Multi-arch:** amd64 + arm64 (Intel/Apple Silicon, Linux). The manifest picks your platform — do not force `--platform`.
- `quay.io/geored/pharos-dev` is the **development/testing** repo; use this one for real setups.

## Tags

- `1.0.0`, `1.0` — immutable release tags — **pin these** (or a digest) for anything durable
- `latest` — newest release
- `main`, `<sha>` — CI builds of every main-branch commit (dev convenience, not for production)

## Quick Start — local (2 minutes)

```bash
# 1. Generate an auth token (the server refuses to start on 0.0.0.0 without one)
TOKEN=$(openssl rand -hex 32)

# 2. Run (docker works identically — swap the command name)
podman run -d --name pharos \
  -p 8000:8000 \
  -v ~/.kube/config:/opt/app-root/.kube/config:ro \
  -e KUBECONFIG=/opt/app-root/.kube/config \
  -e LUMINO_HTTP_TOKEN=$TOKEN \
  quay.io/geored/pharos:1.0.0

# 3. Health check
curl -fsS http://127.0.0.1:8000/health   # {"status":"ok"}

# 4. Add to Claude Code
claude mcp add --transport http \
  --header "Authorization: Bearer $TOKEN" \
  -s user pharos http://127.0.0.1:8000/mcp

# 5. Test
claude "List all namespaces in my cluster"
```

Your kubeconfig is mounted **read-only**; Pharos talks to whatever contexts it holds. No `--rm`: if the container crashes, logs stay readable via `podman logs pharos`.

## Other MCP clients

All clients connect to `http://127.0.0.1:8000/mcp` with header `Authorization: Bearer <token>`.

**Cursor** — `.cursor/mcp.json`
```json
{"mcpServers":{"pharos":{"url":"http://127.0.0.1:8000/mcp","headers":{"Authorization":"Bearer <token>"}}}}
```
**VS Code (Copilot)** — `.vscode/mcp.json`
```json
{"servers":{"pharos":{"type":"http","url":"http://127.0.0.1:8000/mcp","headers":{"Authorization":"Bearer <token>"}}}}
```
**Claude Desktop** — `claude_desktop_config.json`
```json
{"mcpServers":{"pharos":{"url":"http://127.0.0.1:8000/mcp","headers":{"Authorization":"Bearer <token>"}}}}
```
**JetBrains** — Settings → Tools → MCP Servers → Add HTTP → `http://127.0.0.1:8000/mcp` · **Windsurf** — `~/.codeium/windsurf/mcp_config.json` with `serverUrl`.

## Multi-cluster fleet dispatch

One Pharos serves many clusters. Every tool accepts `source=<cluster>`:

1. `list_sources` — shows every kubeconfig context Pharos discovered.
2. `connect_cluster(name="prod-1", credential_ref="kubeconfig:/opt/app-root/.kube/config#<exact-context-name>")` — registers a named cluster. Paths are **container** paths; raw tokens are rejected by design.
3. Any tool: `list_pipelineruns(namespace="team-x", source="prod-1")`.

Ask your AI: *"Connect prod-1 and prod-2, then compare failed PipelineRuns across both."*

For a declarative setup, mount a `lumino.yaml` (`-e LUMINO_CONFIG=/opt/app-root/src/lumino.yaml -v ./lumino.yaml:/opt/app-root/src/lumino.yaml:ro`):

```yaml
sources:
  kubernetes:
    credential_ref_roots:
      - /opt/app-root/.kube
  prometheus:
    adapter: prometheus
extensions:
  konflux: "on"
  openshift: "on"
  tekton: "on"
```

## Deploying on Kubernetes / OpenShift

Pharos runs as a standard Deployment and supports **in-cluster ServiceAccount auth** for its home cluster — no kubeconfig required for local diagnostics. The repo ships a reviewed least-privilege manifest: [`deploy/rbac-readonly.yaml`](https://github.com/spre-sre/pharos/blob/main/deploy/rbac-readonly.yaml) (verbs `get`+`list` only; secrets access is a deliberately separate opt-in ClusterRole; Prometheus/Thanos metrics come via a binding to OpenShift's `cluster-monitoring-view`, never by reading Secrets).

Minimal shape:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: pharos
spec:
  replicas: 1
  selector: {matchLabels: {app: pharos}}
  template:
    metadata: {labels: {app: pharos}}
    spec:
      serviceAccountName: pharos          # bind via deploy/rbac-readonly.yaml
      containers:
        - name: pharos
          image: quay.io/geored/pharos@sha256:<pin-a-digest>
          ports: [{containerPort: 8000}]
          env:
            - name: LUMINO_HTTP_TOKEN
              valueFrom: {secretKeyRef: {name: pharos-token, key: token}}
            - name: KUBEARCHIVE_ENABLED
              value: "true"
          readinessProbe:
            httpGet: {path: /health, port: 8000}
---
apiVersion: v1
kind: Service
metadata: {name: pharos}
spec:
  selector: {app: pharos}
  ports: [{port: 8000, targetPort: 8000}]
```

- Expose via Route/Ingress with TLS if remote MCP clients connect; in-cluster consumers use `http://pharos:8000/mcp`.
- **Multi-cluster from in-cluster:** mount per-cluster credentials (e.g., a kubeconfig built from remote ServiceAccount tokens) as a Secret and point `credential_ref` at the mounted path — same `connect_cluster` flow as local.
- Deploy through your GitOps tooling (ArgoCD etc.) and pin images by digest.

## Configuration reference

- `LUMINO_HTTP_TOKEN` — **required in network mode**: bearer token for `/mcp`; the server **fails closed** on 0.0.0.0 without it
- `KUBECONFIG` — kubeconfig mode: container path of the mounted kubeconfig
- `LUMINO_CONFIG` — optional: path to `lumino.yaml` (sources, extensions, profiles)
- `KUBEARCHIVE_ENABLED` — optional: enable archived-resource queries (KubeArchive)

Endpoints: `/mcp` (streamable HTTP, MCP protocol) · `/health`.

## What's included (49 tools)

Kubernetes core (namespaces, pods, resources, labels, constraints) · log analysis (smart summaries, hybrid/streaming analysis, semantic search, anomaly detection) · Tekton pipelines (runs, logs, failure RCA, tracing) · events & correlation · Prometheus/PromQL + bottleneck forecasting · OpenShift (cluster operators, MachineConfigPools, etcd) · certificates & TLS investigation · topology & what-if simulation · adaptive namespace investigation · predictive/ML analysis · KubeArchive queries for pruned resources · automated triage/RCA reports · multi-cluster management (`list_sources`, `connect_cluster`, `refresh_capabilities`).

Full tool reference: [github.com/spre-sre/pharos](https://github.com/spre-sre/pharos).

## Security model

- **Read-only by design** — no tool mutates cluster state; RBAC manifests grant `get`/`list` only.
- **Fail-closed auth** — network mode refuses to start without a bearer token.
- **No LLM traffic** — Pharos is a pure data layer; all AI reasoning happens in your MCP client. No API keys enter the container.
- **Credential hygiene** — kubeconfig mounted read-only; `connect_cluster` accepts credential *references*, never raw tokens; metrics auth via projected SA tokens.

## Support

Issues and feature requests: <https://github.com/spre-sre/pharos/issues> · Maintained by the SPRE team (Red Hat) as part of the Agentic SRE initiative.
