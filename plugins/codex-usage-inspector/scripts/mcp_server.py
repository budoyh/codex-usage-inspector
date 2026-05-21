#!/usr/bin/env python3
from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Literal

import mcp.types as types
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from usage_core import (
    build_dashboard_payload,
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


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
WIDGET_TEMPLATE_PATH = PLUGIN_ROOT / "assets" / "usage-widget.html"
WIDGET_TEMPLATE_URI = "ui://widget/codex-usage-inspector.html"
WIDGET_MIME_TYPE = "text/html+skybridge"
DEFAULT_TOP_SESSIONS = 8
DEFAULT_CACHE_TTL_SECONDS = 300


class UsageCache:
    def __init__(self, codex_home: Path, cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS) -> None:
        self.codex_home = codex_home
        self.cache_ttl_seconds = cache_ttl_seconds
        self._records = None
        self._loaded_at = 0.0

    def get_records(self, force_refresh: bool = False):
        now = time.time()
        if (
            force_refresh
            or self._records is None
            or now - self._loaded_at > self.cache_ttl_seconds
        ):
            self._records = load_records(self.codex_home)
            self._loaded_at = now
        return self._records


def load_widget_html() -> str:
    return WIDGET_TEMPLATE_PATH.read_text(encoding="utf-8")


def tool_meta(invocation: str) -> dict[str, object]:
    return {
        "openai/outputTemplate": WIDGET_TEMPLATE_URI,
        "openai/toolInvocation/invoking": "正在整理 Codex token 用量面板",
        "openai/toolInvocation/invoked": "Codex token 面板已就绪",
        "openai/widgetAccessible": True,
        "invocation": invocation,
    }


def compact_dashboard_payload(raw_payload: dict) -> dict:
    periods = {}
    for key, period in raw_payload["periods"].items():
        periods[key] = {
            "key": period["key"],
            "display_name": period["display_name"],
            "range_start": period["range_start"],
            "range_end": period["range_end"],
            "summary": period["summary"],
            "cost_estimate": period.get("cost_estimate"),
            "cost_comparison": period["cost_comparison"],
            "top_sessions": [
                {
                    "session_timestamp": session["session_timestamp"],
                    "total_tokens": session["total_tokens"],
                    "cached_input_ratio_pct": session["cached_input_ratio_pct"],
                    "source": session["source"],
                    "plan_type": session["plan_type"],
                }
                for session in period["top_sessions"]
            ],
        }

    return {
        "meta": {
            **raw_payload["meta"],
            "default_top_sessions": DEFAULT_TOP_SESSIONS,
        },
        "periods": periods,
        "charts": raw_payload["charts"],
    }


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
            raise ValueError("from_date 和 to_date 必须同时提供。")
        start_day = parse_day(from_date)
        end_day = parse_day(to_date)
        if end_day < start_day:
            raise ValueError("to_date 不能早于 from_date。")
        label = f"{start_day.isoformat()}..{end_day.isoformat()}"
    else:
        start_day, end_day, label = resolve_period_range(period, tzinfo)

    filtered = filter_records(records, start_day, end_day)
    pricing = normalize_pricing(price_profile)
    if pricing is None:
        raise ValueError("price_profile 无效。")
    report = build_report(filtered, label, pricing=pricing, top_sessions=top_sessions)
    report["meta"] = {
        "price_profile": price_profile,
        "range_start": start_day.isoformat() if start_day else None,
        "range_end": end_day.isoformat() if end_day else None,
        "parsed_session_count": len(filtered),
        "timezone": str(tzinfo),
    }
    return report


def format_cached_ratio(summary: dict) -> str:
    pct = summary.get("cached_input_ratio_pct")
    if pct is None:
        return "—"
    return f"{pct}%"


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
mcp = FastMCP(
    name="codex-usage-inspector",
    instructions=(
        "Use show_usage_dashboard when the user wants a visual Codex token panel or pricing comparison. "
        "Use get_usage_summary when the user wants a direct numeric answer for a period."
    ),
    host=args.host,
    port=args.port,
    stateless_http=True,
    streamable_http_path="/mcp",
)


@mcp.resource(
    WIDGET_TEMPLATE_URI,
    name="Codex Usage Inspector widget",
    title="Codex Usage Inspector widget",
    mime_type=WIDGET_MIME_TYPE,
)
async def usage_widget_template() -> str:
    return load_widget_html()


@mcp.tool()
async def show_usage_dashboard(
    top_sessions: int = Field(default=DEFAULT_TOP_SESSIONS, ge=3, le=20, description="Top sessions shown in the widget."),
    force_refresh: bool = Field(default=False, description="Whether to bypass the in-memory cache and rescan local logs."),
) -> types.CallToolResult:
    records = cache.get_records(force_refresh=force_refresh)
    payload = build_dashboard_payload(
        codex_home=codex_home,
        records=records,
        price_profile_name="gpt-5.5-standard",
        top_sessions=top_sessions,
    )
    compact_payload = compact_dashboard_payload(payload)
    return types.CallToolResult(
        content=[
            types.TextContent(
                type="text",
                text="已渲染 Codex token 用量面板。数据来自本地 Codex 日志，不等同于官方账单。",
            )
        ],
        structuredContent=compact_payload,
        _meta=tool_meta("show_usage_dashboard"),
        isError=False,
    )


@mcp.tool()
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
        f"{summary['label']}：总 token {summary['total_tokens']:,}，"
        f"输入 {summary['input_tokens']:,}，缓存输入 {summary['cached_input_tokens']:,}，"
        f"输出 {summary['output_tokens']:,}，缓存占比 {format_cached_ratio(summary)}，"
        f"{cost['display_name']} 等价成本约 {cost['total_cost']} {cost['currency']}。"
    )
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structuredContent=payload,
        isError=False,
    )


if __name__ == "__main__":
    mcp.run(args.transport, mount_path="/mcp" if args.transport == "streamable-http" else None)
