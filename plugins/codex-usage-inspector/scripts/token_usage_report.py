#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from usage_core import (  # noqa: E402
    DEFAULT_TAIL_LINE_LIMIT,
    build_report,
    load_records,
    local_timezone,
    month_bounds,
    normalize_pricing,
    parse_day,
    resolve_codex_home,
    resolve_period_range,
    session_to_dict,
    serialize_price_profiles,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Summarize Codex token usage from local session logs.",
    )
    parser.add_argument("--codex-home", help="Codex home directory. Defaults to $CODEX_HOME or ~/.codex.")
    parser.add_argument(
        "--period",
        choices=["today", "yesterday", "last7", "this-month", "month", "all"],
        default="this-month",
        help="Preset time range. Use --month or --from-date/--to-date for custom ranges.",
    )
    parser.add_argument("--month", help="Target month in YYYY-MM format.")
    parser.add_argument("--from-date", help="Range start in YYYY-MM-DD format.")
    parser.add_argument("--to-date", help="Range end in YYYY-MM-DD format.")
    parser.add_argument("--top-sessions", type=int, default=10, help="Number of highest-usage sessions to include.")
    parser.add_argument("--tail-line-limit", type=int, default=DEFAULT_TAIL_LINE_LIMIT, help="Tail lines to inspect for the final token snapshot.")
    parser.add_argument("--price-profile", default="gpt-5.5-standard", help="Pricing profile name.")
    parser.add_argument("--no-cost-estimate", action="store_true", help="Skip API-equivalent cost estimation.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of human-readable text.")
    parser.add_argument("--list-price-profiles", action="store_true", help="List built-in pricing profiles as JSON and exit.")
    parser.add_argument("--write-json", help="Write the full result payload to a JSON file.")
    parser.add_argument("--write-daily-csv", help="Write the per-day breakdown to CSV.")
    parser.add_argument("--write-session-csv", help="Write the per-session breakdown to CSV.")
    parser.add_argument("--write-markdown", help="Write a compact Markdown report.")
    return parser.parse_args()


def resolve_date_range(args: argparse.Namespace) -> tuple[object, object, str]:
    tzinfo = local_timezone()

    if args.month:
        start, end = month_bounds(args.month)
        return start, end, args.month

    if args.from_date or args.to_date:
        if not args.from_date or not args.to_date:
            raise ValueError("--from-date and --to-date must be used together.")
        start = parse_day(args.from_date)
        end = parse_day(args.to_date)
        if end < start:
            raise ValueError("--to-date must not be earlier than --from-date.")
        return start, end, f"{start.isoformat()}..{end.isoformat()}"

    if args.period == "month":
        raise ValueError("Use --month YYYY-MM with --period month.")

    return resolve_period_range(args.period, tzinfo)


def write_csv(path: str, rows: list[dict]) -> None:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        target.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: str, payload: dict) -> None:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_markdown(path: str, result: dict) -> None:
    summary = result["summary"]
    lines = [
        "# Codex Usage Report",
        "",
        f"- Range: `{summary['label']}`",
        f"- Sessions: `{summary['session_count']}`",
        f"- Total tokens: `{summary['total_tokens']}`",
        f"- Input tokens: `{summary['input_tokens']}`",
        f"- Cached input tokens: `{summary['cached_input_tokens']}`",
        f"- Non-cached input tokens: `{summary['non_cached_input_tokens']}`",
        f"- Output tokens: `{summary['output_tokens']}`",
        f"- Reasoning output tokens: `{summary['reasoning_output_tokens']}`",
        f"- Cached input ratio: `{summary['cached_input_ratio_pct']}`%",
        "",
    ]
    if "cost_estimate" in result:
        cost = result["cost_estimate"]
        lines.extend(
            [
                "## API-equivalent cost",
                "",
                f"- Profile: `{cost['display_name']}`",
                f"- Input cost: `{cost['input_cost']}` {cost['currency']}",
                f"- Cached input cost: `{cost['cached_input_cost']}` {cost['currency']}",
                f"- Output cost: `{cost['output_cost']}` {cost['currency']}",
                f"- Total cost: `{cost['total_cost']}` {cost['currency']}",
                "",
            ]
        )
    if result["daily"]:
        lines.extend(["## Daily totals", ""])
        for row in result["daily"]:
            lines.append(
                f"- `{row['day']}`: `{row['total_tokens']}` total, `{row['session_count']}` sessions, `{row['cached_input_ratio_pct']}`% cached"
            )
        lines.append("")
    if result["top_sessions"]:
        lines.extend(["## Top sessions", ""])
        for item in result["top_sessions"]:
            lines.append(
                f"- `{item['session_timestamp']}`: `{item['total_tokens']}` total, `{item['cached_input_ratio_pct']}`% cached, `{item['file']}`"
            )
        lines.append("")
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines), encoding="utf-8")


def render_text(payload: dict) -> str:
    summary = payload["summary"]
    lines = [
        f"范围: {summary['label']}",
        f"会话数: {summary['session_count']}",
        f"总 token: {summary['total_tokens']:,}",
        f"输入 token: {summary['input_tokens']:,}",
        f"缓存输入: {summary['cached_input_tokens']:,}",
        f"非缓存输入: {summary['non_cached_input_tokens']:,}",
        f"输出 token: {summary['output_tokens']:,}",
        f"reasoning output: {summary['reasoning_output_tokens']:,}",
        f"缓存占比: {summary['cached_input_ratio_pct']}%",
    ]
    if "cost_estimate" in payload:
        cost = payload["cost_estimate"]
        lines.extend(
            [
                "",
                f"价格档: {cost['display_name']}",
                f"输入成本: {cost['input_cost']} {cost['currency']}",
                f"缓存输入成本: {cost['cached_input_cost']} {cost['currency']}",
                f"输出成本: {cost['output_cost']} {cost['currency']}",
                f"总成本: {cost['total_cost']} {cost['currency']}",
            ]
        )
    if payload["top_sessions"]:
        lines.append("")
        lines.append("Top sessions:")
        for item in payload["top_sessions"]:
            lines.append(
                f"- {item['session_timestamp']}: {item['total_tokens']:,} total, {item['cached_input_ratio_pct']}% cached"
            )
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    if args.list_price_profiles:
        print(json.dumps(serialize_price_profiles(), ensure_ascii=False, indent=2))
        return 0

    try:
        start_day, end_day, label = resolve_date_range(args)
        codex_home = resolve_codex_home(args.codex_home)
        pricing = None if args.no_cost_estimate else normalize_pricing(args.price_profile)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    records = load_records(
        codex_home,
        start_day=start_day,
        end_day=end_day,
        tail_line_limit=args.tail_line_limit,
    )
    result = build_report(records, label, pricing=pricing, top_sessions=args.top_sessions)
    result["meta"] = {
        "codex_home": str(codex_home),
        "range_start": start_day.isoformat() if start_day else None,
        "range_end": end_day.isoformat() if end_day else None,
        "parsed_session_count": len(records),
        "price_profile": args.price_profile if pricing else None,
    }

    if args.write_json:
        write_json(args.write_json, result)
    if args.write_daily_csv:
        write_csv(args.write_daily_csv, result["daily"])
    if args.write_session_csv:
        session_rows = [session_to_dict(record, pricing) for record in sorted(records, key=lambda record: record.total_tokens, reverse=True)]
        write_csv(args.write_session_csv, session_rows)
    if args.write_markdown:
        write_markdown(args.write_markdown, result)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_text(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
