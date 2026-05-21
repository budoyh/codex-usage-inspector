#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Literal

import mcp.types as types
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from usage_core import (
    build_report,
    filter_records,
    load_records,
    local_timezone,
    month_bounds,
    normalize_pricing,
    parse_day,
    resolve_codex_home,
    resolve_period_range,
)


SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parent
SERVE_SCRIPT = SCRIPT_DIR / "serve_dashboard.py"
DEFAULT_TOP_SESSIONS = 8
DEFAULT_CACHE_TTL_SECONDS = 300
DEFAULT_BROWSER_HOST = "127.0.0.1"
DEFAULT_BROWSER_PORT = 8765
PORT_SCAN_LIMIT = 12


class UsageCache:
    def __init__(self, codex_home: Path, cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS) -> None:
        self.codex_home = codex_home
        self.cache_ttl_seconds = cache_ttl_seconds
        self._records = None
        self._loaded_at = 0.0

    def get_records(self, force_refresh: bool = False):
        now = time.time()
        if force_refresh or self._records is None or now - self._loaded_at > self.cache_ttl_seconds:
            self._records = load_records(self.codex_home)
            self._loaded_at = now
        return self._records


def append_debug_log(codex_home: Path, message: str) -> None:
    try:
        log_path = codex_home / "codex-usage-inspector-debug.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().isoformat()
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {message}\n")
    except Exception:
        pass


def build_summary_payload(
    records,
    *,
    period: Literal["today", "yesterday", "last7", "this-month", "all"] = "this-month",
    month: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    price_profile: str = "gpt-5.5-standard",
    top_sessions: int = DEFAULT_TOP_SESSIONS,
) -> dict:
    tzinfo = local_timezone()
    if month:
        start_day, end_day = month_bounds(month)
        label = month
    elif from_date or to_date:
        if not from_date or not to_date:
            raise ValueError("from_date and to_date must be provided together.")
        start_day = parse_day(from_date)
        end_day = parse_day(to_date)
        if end_day < start_day:
            raise ValueError("to_date must be on or after from_date.")
        label = f"{start_day.isoformat()}..{end_day.isoformat()}"
    else:
        start_day, end_day, label = resolve_period_range(period, tzinfo)

    filtered = filter_records(records, start_day, end_day)
    pricing = normalize_pricing(price_profile)
    if pricing is None:
        raise ValueError("Invalid price_profile.")

    report = build_report(filtered, label, pricing=pricing, top_sessions=top_sessions)
    report["meta"] = {
        "price_profile": price_profile,
        "range_start": start_day.isoformat() if start_day else None,
        "range_end": end_day.isoformat() if end_day else None,
        "parsed_session_count": len(filtered),
        "timezone": str(tzinfo),
    }
    return report


def dashboard_state_path(codex_home: Path) -> Path:
    return codex_home / "codex-usage-inspector-browser.json"


def load_dashboard_state(codex_home: Path) -> dict | None:
    path = dashboard_state_path(codex_home)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_dashboard_state(codex_home: Path, payload: dict) -> None:
    path = dashboard_state_path(codex_home)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def is_port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def request_ok(url: str, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def dashboard_urls(host: str, port: int) -> tuple[str, str, str, str]:
    base = f"http://{host}:{port}"
    return f"{base}/dashboard", f"{base}/api/dashboard", f"{base}/healthz", f"{base}/warmup"


def dashboard_server_healthy(host: str, port: int) -> bool:
    _, _, health_url, _ = dashboard_urls(host, port)
    return request_ok(health_url, timeout=1.0)


def warmup_dashboard_server(host: str, port: int, force_refresh: bool) -> bool:
    _, _, _, warmup_url = dashboard_urls(host, port)
    query = urllib.parse.urlencode({"refresh": "1" if force_refresh else "0"})
    return request_ok(f"{warmup_url}?{query}", timeout=240.0)


def find_candidate_port(host: str) -> int:
    for offset in range(PORT_SCAN_LIMIT):
        port = DEFAULT_BROWSER_PORT + offset
        if not is_port_open(host, port):
            return port
    raise RuntimeError("No free port found for the local dashboard.")


def launch_dashboard_server(codex_home: Path, host: str, port: int, top_sessions: int) -> int:
    log_path = codex_home / "codex-usage-inspector-browser-server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("a", encoding="utf-8")

    cmd = [
        sys.executable,
        "-u",
        str(SERVE_SCRIPT),
        "--host",
        host,
        "--port",
        str(port),
        "--codex-home",
        str(codex_home),
        "--top-sessions",
        str(top_sessions),
    ]
    creationflags = 0
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )

    process = subprocess.Popen(
        cmd,
        cwd=str(PLUGIN_ROOT),
        stdout=log_handle,
        stderr=log_handle,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=False if os.name == "nt" else True,
    )
    return process.pid


def ensure_browser_dashboard(
    codex_home: Path,
    *,
    top_sessions: int = DEFAULT_TOP_SESSIONS,
    host: str = DEFAULT_BROWSER_HOST,
    force_refresh: bool = False,
) -> dict:
    state = load_dashboard_state(codex_home)
    if state:
        saved_host = state.get("host") or host
        saved_port = int(state.get("port") or DEFAULT_BROWSER_PORT)
        if dashboard_server_healthy(saved_host, saved_port):
            warmup_dashboard_server(saved_host, saved_port, force_refresh)
            page_url, api_url, _, _ = dashboard_urls(saved_host, saved_port)
            return {
                "host": saved_host,
                "port": saved_port,
                "page_url": page_url,
                "api_url": api_url,
                "started": False,
                "pid": state.get("pid"),
            }

    for offset in range(PORT_SCAN_LIMIT):
        port = DEFAULT_BROWSER_PORT + offset
        if dashboard_server_healthy(host, port):
            warmup_dashboard_server(host, port, force_refresh)
            page_url, api_url, _, _ = dashboard_urls(host, port)
            save_dashboard_state(codex_home, {"host": host, "port": port, "pid": None})
            return {
                "host": host,
                "port": port,
                "page_url": page_url,
                "api_url": api_url,
                "started": False,
                "pid": None,
            }

    port = find_candidate_port(host)
    pid = launch_dashboard_server(codex_home, host, port, top_sessions)
    page_url, api_url, _, _ = dashboard_urls(host, port)

    deadline = time.time() + 30.0
    while time.time() < deadline:
        if dashboard_server_healthy(host, port):
            if not warmup_dashboard_server(host, port, force_refresh):
                raise RuntimeError(f"Dashboard server started but warmup failed at {page_url}.")
            save_dashboard_state(codex_home, {"host": host, "port": port, "pid": pid})
            return {
                "host": host,
                "port": port,
                "page_url": page_url,
                "api_url": api_url,
                "started": True,
                "pid": pid,
            }
        time.sleep(0.4)

    raise RuntimeError(f"Dashboard server did not become ready at {page_url}.")


def format_ratio(summary: dict) -> str:
    ratio = summary.get("cached_input_ratio_pct")
    if ratio is None:
        return "-"
    return f"{ratio:.2f}%"


parser = argparse.ArgumentParser(description="Codex Usage Inspector MCP server.")
parser.add_argument("--codex-home", help="Codex home directory. Defaults to $CODEX_HOME or ~/.codex.")
parser.add_argument(
    "--transport",
    choices=["stdio", "streamable-http"],
    default="stdio",
    help="Transport to run. stdio is for Codex plugin use; streamable-http is for local debugging.",
)
parser.add_argument("--host", default="127.0.0.1", help="Host used with streamable-http.")
parser.add_argument("--port", type=int, default=8766, help="Port used with streamable-http.")
parser.add_argument("--cache-ttl-seconds", type=int, default=DEFAULT_CACHE_TTL_SECONDS)
args, _ = parser.parse_known_args()

codex_home = resolve_codex_home(args.codex_home)
cache = UsageCache(codex_home=codex_home, cache_ttl_seconds=args.cache_ttl_seconds)
append_debug_log(
    codex_home,
    f"server_start pid={os.getpid()} transport={args.transport} file={Path(__file__).resolve()} cwd={Path.cwd()}",
)
mcp = FastMCP(
    name="codex-usage-inspector",
    instructions=(
        "Use show_usage_dashboard when the user wants a browser-based Codex token panel or pricing comparison. "
        "Use get_usage_summary when the user wants a direct numeric answer for a period."
    ),
    host=args.host,
    port=args.port,
    stateless_http=True,
    streamable_http_path="/mcp",
)


@mcp.tool(
    description="Start the local token dashboard server and return a browser URL inside Codex.",
)
async def show_usage_dashboard(
    top_sessions: int = Field(default=DEFAULT_TOP_SESSIONS, ge=3, le=20, description="Top sessions shown in the dashboard."),
    force_refresh: bool = Field(default=False, description="Whether to bypass the in-memory cache and rescan local logs before reporting."),
) -> types.CallToolResult:
    append_debug_log(
        codex_home,
        f"show_usage_dashboard top_sessions={top_sessions} force_refresh={force_refresh}",
    )
    records = cache.get_records(force_refresh=force_refresh)
    summary_payload = build_summary_payload(
        records,
        period="this-month",
        price_profile="gpt-5.5-standard",
        top_sessions=top_sessions,
    )
    dashboard = ensure_browser_dashboard(
        codex_home,
        top_sessions=top_sessions,
        force_refresh=force_refresh,
    )
    summary = summary_payload["summary"]
    cost = summary_payload.get("cost_estimate")
    append_debug_log(
        codex_home,
        f"browser_dashboard_ready url={dashboard['page_url']} started={dashboard['started']} pid={dashboard['pid']}",
    )
    text = (
        f"Browser dashboard ready: {dashboard['page_url']}\n"
        f"This month: {summary['total_tokens']:,} tokens, cache ratio {format_ratio(summary)}, "
        f"GPT-5.5 cost about {cost['total_cost']} {cost['currency']}."
    )
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structuredContent={
            "dashboard_url": dashboard["page_url"],
            "dashboard_api_url": dashboard["api_url"],
            "started_server": dashboard["started"],
            "summary": summary,
            "cost_estimate": cost,
            "source": "local_codex_logs",
        },
        isError=False,
    )


@mcp.tool(
    description="Summarize Codex token usage for a requested time range.",
)
async def get_usage_summary(
    period: Literal["today", "yesterday", "last7", "this-month", "all"] = Field(
        default="this-month",
        description="Named time range.",
    ),
    month: str | None = Field(
        default=None,
        description="Target month in YYYY-MM format. Overrides period when provided.",
    ),
    from_date: str | None = Field(
        default=None,
        description="Range start in YYYY-MM-DD format. Must be used with to_date.",
    ),
    to_date: str | None = Field(
        default=None,
        description="Range end in YYYY-MM-DD format. Must be used with from_date.",
    ),
    price_profile: Literal[
        "gpt-5.5-standard",
        "deepseek-v4-pro-discounted",
        "deepseek-v4-flash-discounted",
    ] = Field(default="gpt-5.5-standard", description="API-equivalent pricing profile."),
    top_sessions: int = Field(default=5, ge=1, le=20, description="How many top sessions to include."),
    force_refresh: bool = Field(default=False, description="Whether to bypass the in-memory cache and rescan local logs."),
) -> types.CallToolResult:
    append_debug_log(
        codex_home,
        (
            f"get_usage_summary period={period} month={month} from_date={from_date} "
            f"to_date={to_date} price_profile={price_profile} top_sessions={top_sessions} "
            f"force_refresh={force_refresh}"
        ),
    )
    records = cache.get_records(force_refresh=force_refresh)
    payload = build_summary_payload(
        records,
        period=period,
        month=month,
        from_date=from_date,
        to_date=to_date,
        price_profile=price_profile,
        top_sessions=top_sessions,
    )
    summary = payload["summary"]
    cost = payload["cost_estimate"]
    text = (
        f"{summary['label']}: total tokens {summary['total_tokens']:,}, "
        f"input {summary['input_tokens']:,}, cached input {summary['cached_input_tokens']:,}, "
        f"output {summary['output_tokens']:,}, cache ratio {format_ratio(summary)}, "
        f"{cost['display_name']} cost about {cost['total_cost']} {cost['currency']}."
    )
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structuredContent=payload,
        isError=False,
    )


if __name__ == "__main__":
    mcp.run(args.transport, mount_path="/mcp" if args.transport == "streamable-http" else None)
