#!/usr/bin/env python3
"""
MCP server health checker.

Supported transports (auto-detected unless config specifies otherwise):
  - streamable-http   POST → JSON response           (MCP spec 2024-11-05+)
  - streamable-sse    POST → text/event-stream        (MCP spec 2024-11-05+)
  - legacy-sse        GET /sse → endpoint event → POST (pre-2024-11-05)
"""
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
import yaml

ROOT = Path(__file__).parent.parent
CONFIG = ROOT / "config" / "servers.yml"
DATA_DIR = ROOT / "docs" / "data"
STATUS_FILE   = DATA_DIR / "status.json"
HISTORY_FILE  = DATA_DIR / "history.json"
INCIDENTS_FILE = DATA_DIR / "incidents.json"
DAILY_FILE    = DATA_DIR / "daily.json"

# ---------------------------------------------------------------------------
# MCP protocol helpers
# ---------------------------------------------------------------------------

MCP_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "mcp-status-checker", "version": "1.0.0"},
    },
}

HEADERS_POST = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

TIMEOUT = 12  # seconds


def _is_valid_mcp_response(body: dict) -> bool:
    return isinstance(body, dict) and ("result" in body or "error" in body)


def _parse_sse_stream(resp) -> dict | None:
    """Read lines from a streaming response and return the first MCP JSON payload."""
    data_buf = ""
    for raw in resp.iter_lines(decode_unicode=True):
        line = raw.strip() if isinstance(raw, str) else raw.decode().strip()
        if line.startswith("data:"):
            data_buf = line[5:].strip()
        elif line == "" and data_buf:
            # blank line = end of event
            try:
                return json.loads(data_buf)
            except ValueError:
                data_buf = ""
                continue
            data_buf = ""
    return None


# ---------------------------------------------------------------------------
# Transport checkers
# ---------------------------------------------------------------------------

def _check_streamable_http(url: str) -> dict:
    """POST initialize; handle JSON or SSE response."""
    resp = requests.post(url, json=MCP_INIT, headers=HEADERS_POST,
                         timeout=TIMEOUT, stream=True)
    ct = resp.headers.get("content-type", "")

    if "text/event-stream" in ct:
        body = _parse_sse_stream(resp)
        if body is None:
            return {"ok": False, "error": "SSE stream contained no data events"}
        if _is_valid_mcp_response(body):
            return {"ok": True}
        return {"ok": False, "error": "unexpected SSE payload shape"}

    if resp.status_code in (401, 403):
        # Auth required but server is alive
        return {"ok": True, "note": f"HTTP {resp.status_code} (auth required)"}

    if resp.status_code != 200:
        return {"ok": False, "error": f"HTTP {resp.status_code}"}

    try:
        body = resp.json()
    except ValueError:
        return {"ok": False, "error": "non-JSON response (HTTP 200)"}

    if _is_valid_mcp_response(body):
        return {"ok": True}
    return {"ok": False, "error": "unexpected JSON response shape"}


def _check_legacy_sse(url: str) -> dict:
    """
    Legacy SSE transport:
      1. GET <url>  →  SSE stream containing  event: endpoint / data: <path>
      2. POST to that path with the initialize payload
      3. Expect a JSON-RPC response to arrive on the SSE stream
    """
    # Normalise to a /sse path if not already
    sse_url = url if url.endswith("/sse") else url.rstrip("/") + "/sse"

    resp = requests.get(sse_url,
                        headers={"Accept": "text/event-stream"},
                        timeout=TIMEOUT, stream=True)

    if "text/event-stream" not in resp.headers.get("content-type", ""):
        return {"ok": False, "error": "GET /sse did not return SSE stream"}

    # Read SSE until we find the endpoint event
    endpoint_path = None
    current_event = None
    for raw in resp.iter_lines(decode_unicode=True):
        line = raw.strip() if isinstance(raw, str) else raw.decode().strip()
        if line.startswith("event:"):
            current_event = line[6:].strip()
        elif line.startswith("data:") and current_event == "endpoint":
            endpoint_path = line[5:].strip()
            break

    if not endpoint_path:
        return {"ok": False, "error": "no endpoint event in SSE stream"}

    msg_url = urljoin(url.rstrip("/sse").rstrip("/") + "/", endpoint_path.lstrip("/"))

    post_resp = requests.post(
        msg_url, json=MCP_INIT,
        headers={"Content-Type": "application/json"},
        timeout=TIMEOUT,
    )

    if post_resp.status_code not in (200, 202):
        return {"ok": False, "error": f"POST to message endpoint returned HTTP {post_resp.status_code}"}

    # For legacy SSE the response arrives on the original SSE stream, not the POST body.
    # Receiving 200/202 without an error body is sufficient evidence the server is alive.
    return {"ok": True, "note": "legacy SSE transport"}


def check_server(server: dict) -> dict:
    """
    Run the appropriate check, auto-detecting transport when not specified.
    Returns: {status, latency_ms, error?, note?, transport?}
    """
    url = server["url"]
    transport = server.get("transport", "auto")
    start = time.time()

    def elapsed():
        return round((time.time() - start) * 1000)

    try:
        if transport in ("http", "auto"):
            result = _check_streamable_http(url)
            if result["ok"]:
                return {"status": "up", "latency_ms": elapsed(),
                        "note": result.get("note"), "transport": "streamable-http"}
            # If auto and streamable-http failed with a connectivity-style error,
            # fall through to legacy SSE. If it was a 405 or similar, also try.
            if transport == "auto":
                error_hint = result.get("error", "")
                if any(k in error_hint for k in ("405", "404", "non-JSON", "unexpected")):
                    result2 = _check_legacy_sse(url)
                    if result2["ok"]:
                        return {"status": "up", "latency_ms": elapsed(),
                                "note": result2.get("note"), "transport": "legacy-sse"}
            return {"status": "down", "latency_ms": elapsed(), "error": result.get("error")}

        if transport in ("sse", "legacy-sse"):
            result = _check_legacy_sse(url)
            if result["ok"]:
                return {"status": "up", "latency_ms": elapsed(),
                        "note": result.get("note"), "transport": "legacy-sse"}
            return {"status": "down", "latency_ms": elapsed(), "error": result.get("error")}

        return {"status": "down", "latency_ms": elapsed(), "error": f"unknown transport: {transport}"}

    except requests.Timeout:
        return {"status": "down", "latency_ms": elapsed(), "error": "timeout"}
    except requests.ConnectionError:
        return {"status": "down", "latency_ms": elapsed(), "error": "connection refused"}
    except Exception as e:
        return {"status": "down", "latency_ms": elapsed(), "error": str(e)[:140]}


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def load_json(path: Path, default):
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


MAX_CHECKS = 90  # raw checks kept per server


def update_history(history: dict, name: str, entry: dict) -> list:
    history.setdefault(name, [])
    history[name].append(entry)
    history[name] = history[name][-MAX_CHECKS:]
    return history[name]


def update_daily(daily: dict, name: str, date_str: str, status: str):
    daily.setdefault(name, {})
    day = daily[name].setdefault(date_str, {"up": 0, "degraded": 0, "down": 0, "total": 0})
    day["total"] += 1
    if status in day:
        day[status] += 1
    # Keep last 90 days
    if len(daily[name]) > 90:
        oldest = sorted(daily[name])[0]
        del daily[name][oldest]


def update_incidents(incidents: dict, name: str, now: str, status: str):
    incidents.setdefault(name, [])
    lst = incidents[name]
    last = lst[-1] if lst else None

    if status == "down":
        # Open a new incident if there isn't one already open
        if last is None or last.get("resolved_at") is not None:
            lst.append({"started_at": now, "resolved_at": None, "duration_min": None})
    elif status in ("up", "degraded"):
        # Close any open incident
        if last and last.get("resolved_at") is None:
            last["resolved_at"] = now
            start_dt = datetime.fromisoformat(last["started_at"].replace("Z", "+00:00"))
            end_dt   = datetime.fromisoformat(now.replace("Z", "+00:00"))
            last["duration_min"] = round((end_dt - start_dt).total_seconds() / 60)

    # Keep last 50 incidents per server
    incidents[name] = lst[-50:]


def uptime_pct(checks: list) -> float:
    if not checks:
        return 0.0
    return round(sum(1 for c in checks if c["status"] == "up") / len(checks) * 100, 1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    with open(CONFIG) as f:
        config = yaml.safe_load(f)

    servers = config["servers"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = now[:10]

    history   = load_json(HISTORY_FILE,   {})
    incidents = load_json(INCIDENTS_FILE, {})
    daily     = load_json(DAILY_FILE,     {})

    results = []
    any_down = False

    for server in servers:
        name = server["name"]
        print(f"  Checking {name} ...", end=" ", flush=True)
        result = check_server(server)
        status = result["status"]
        print(status, f"({result.get('latency_ms', '?')}ms)",
              f"[{result.get('transport', '?')}]",
              f"  {result.get('error') or result.get('note') or ''}")

        entry = {"ts": now, **result}
        checks = update_history(history, name, entry)
        update_daily(daily, name, today, status)
        update_incidents(incidents, name, now, status)

        if status == "down":
            any_down = True

        results.append({
            "name": name,
            "url": server["url"],
            "tags": server.get("tags", []),
            "status": status,
            "latency_ms": result.get("latency_ms"),
            "transport": result.get("transport"),
            "error": result.get("error"),
            "note": result.get("note"),
            "uptime_90": uptime_pct(checks),
            "recent_checks": [{"ts": c["ts"], "status": c["status"], "latency_ms": c.get("latency_ms")} for c in checks],
            "checked_at": now,
        })

    status_doc = {"generated_at": now, "servers": results}
    save_json(STATUS_FILE,   status_doc)
    save_json(HISTORY_FILE,  history)
    save_json(INCIDENTS_FILE, incidents)
    save_json(DAILY_FILE,    daily)

    print(f"\nWrote data to {DATA_DIR}/")
    if any_down:
        sys.exit(1)


if __name__ == "__main__":
    main()
