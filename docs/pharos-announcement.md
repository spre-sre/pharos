Subject: Pharos MCP Server — now available as a container for the SPRE team

Hi team,

We're shipping **Pharos** — the next generation of the Lumino MCP server, now available as a container image at `quay.io/geored/pharos-dev`. Pharos keeps everything Lumino had and adds multi-cluster fleet operations, new adapters, containerized deployment, and broader AI tool support.

Here's what changed and why it matters for your daily work.

---

### What's new in Pharos vs Lumino

**Multi-cluster dispatch — query any cluster from one session**

Lumino was single-cluster: whichever kubeconfig context was active at startup was the only cluster you could talk to. Pharos lets you connect to multiple clusters in the same conversation and target any of them with `source="cluster-name"`. All 48 tools support it. We've tested it live against 7 Konflux production clusters + the internal-services cluster simultaneously from a single Podman container.

Example workflow:
- "What clusters are available?" — shows all kubeconfig contexts
- "Connect to prd-rh01" — activates extensions (Tekton, OpenShift, Konflux)
- "Compare pipeline success rates between prd-rh01 and p02" — queries both clusters

**Per-source Prometheus routing**

Each connected cluster gets its own Prometheus/Thanos endpoint discovery and bearer token. No cross-cluster metric bleed — querying `prometheus_query(source="prd-rh01")` hits rh01's Thanos, not the default cluster's. Environment variable overrides (`PROMETHEUS_URL`) are confined to the default source only.

**Per-source KubeArchive**

KubeArchive endpoint discovery is now per-cluster. You can query archived PipelineRuns from any connected cluster, not just the default. Bearer tokens are threaded through so auth works inside containers.

**Adapter abstraction for logs**

Instead of tools hardwired to the Kubernetes pod log API, there's now a LogSource protocol layer. The same log analysis tools can pull data from five sources by swapping the adapter:
- **Kubernetes** — pod logs via k8s API (the default, same as Lumino)
- **Loki** — Grafana Loki via LogQL
- **Elasticsearch** — ES clusters via query DSL
- **File** — local log files via glob patterns
- **OTLP** — push-based ingest with a ring buffer receiver (port 4318)

Configure them in `lumino.yaml` and pass `source="my-loki"` to any log tool.

**Container deployment**

Pharos ships as a container image (`quay.io/geored/pharos-dev`) running streamable-http transport. Mount your kubeconfig, set a bearer token, and connect from any MCP client. No Python environment needed on your machine.

**Broader AI tool support**

Works with Claude Code, Claude Desktop, Cursor, VS Code (GitHub Copilot), JetBrains IDEs, Windsurf, and Codex. Configuration snippets for each are in the container image description on Quay.

---

### What stayed the same

- All 48 tools are identical in behavior for single-cluster use
- The Konflux profile with Tekton, OpenShift, and Konflux extensions
- KubeArchive integration with auto port-forward for local dev
- Prometheus/Thanos auto-discovery via OpenShift routes
- The characterization test suite (1,565 tests)

If you're using Lumino today with a single cluster, Pharos is a drop-in replacement. The default path (`source=""`) is byte-identical to Lumino's behavior.

---

### How to try it

```bash
# Generate a token
TOKEN=$(openssl rand -hex 32)

# Run the container
podman run -d --rm --name pharos \
  -p 8000:8000 \
  -v ~/.kube/config:/opt/app-root/.kube/config:ro \
  -e KUBECONFIG=/opt/app-root/.kube/config \
  -e LUMINO_HTTP_TOKEN=$TOKEN \
  quay.io/geored/pharos-dev

# Add to Claude Code
claude mcp add --transport http \
  --header "Authorization: Bearer $TOKEN" \
  -s user pharos http://127.0.0.1:8000/mcp
```

Full setup guide with multi-cluster config and all AI tool snippets is on the Quay image description page.

---

### What's next

- Metric adapters (Mimir, Datadog) — same protocol layer as log adapters
- Trace adapters (Jaeger, Tempo) — OTLP receiver already stubs `/v1/traces`
- Fleet deployment via ArgoCD to spre-automations namespace
- Automated fleet health reporting

Questions or issues — reach out on Slack or file against the repo.

Cheers,
George
