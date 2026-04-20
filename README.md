# NoContext

> Uptime monitoring for public MCP servers — because when your server is down, your AI has no context.

**[View live status page →](https://loving-kyle.github.io/nocontext/)**

---

## How it works

1. GitHub Actions runs `scripts/check.py` every 15 minutes.
2. The script sends an MCP `initialize` handshake (JSON-RPC 2.0) to each server.
3. Results are written to `docs/data/status.json` and committed back to the repo.
4. GitHub Pages serves `docs/index.html`, which fetches the JSON at runtime.

No external services, no database — just git and GitHub Actions.

## Adding a server

Edit [`config/servers.yml`](config/servers.yml):

```yaml
servers:
  - name: "My MCP Server"
    url: "https://my-mcp-server.example.com"
    transport: http
    tags: [custom]
```

Open a PR and it will be included in the next check run.

## What counts as "up"?

The checker POSTs an MCP `initialize` request and expects a valid JSON-RPC 2.0 response (`result` or `error` at the top level). Servers that respond with HTTP 401/403 (auth required) are treated as **up** — they're reachable, just need credentials.

| Status | Meaning |
|--------|---------|
| 🟢 up | Valid MCP response received |
| 🟡 degraded | Reachable but response is malformed |
| 🔴 down | Timeout, connection refused, or HTTP error |

## Local development

```bash
pip install requests pyyaml
python scripts/check.py
# opens docs/index.html in your browser to preview
```

## Setup for your own fork

1. Fork this repo.
2. Go to **Settings → Pages** → set source to `docs/` on `main`.
3. Go to **Settings → Actions → General** → enable "Read and write permissions" for the Actions workflow.
4. The first run will populate `docs/data/status.json` and the status page will go live.

## Contributing

PRs to add new public MCP servers are welcome. Please verify the server is publicly accessible before submitting.
