#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DEFAULT_TAIL_LINE_LIMIT = 500
DEFAULT_TAIL_CHUNK_SIZE = 64 * 1024
TOKEN_COUNT_EVENT = "token_count"
SESSION_META_EVENT = "session_meta"
JSON_SUFFIX = ".jsonl"
FILENAME_TS_RE = re.compile(
    r"^rollout-(?P<day>\d{4}-\d{2}-\d{2})T(?P<hour>\d{2})-(?P<minute>\d{2})-(?P<second>\d{2})-"
)

PERIOD_DISPLAY_NAMES = {
    "today": "今天",
    "yesterday": "昨天",
    "last7": "最近 7 天",
    "this-month": "本月",
    "all": "全部历史",
}

PRICING_PROFILES: dict[str, dict[str, Any]] = {
    "gpt-5.5-standard": {
        "display_name": "GPT-5.5 Standard",
        "input_per_million": 5.0,
        "cached_input_per_million": 0.5,
        "output_per_million": 30.0,
        "currency": "USD",
        "source": "Configured from official OpenAI GPT-5.5 standard API pricing on 2026-05-20.",
        "notes": [
            "Reasoning output tokens are treated as a subset of output tokens.",
            "Does not model long-context surcharges or tool-specific fees.",
        ],
    },
    "deepseek-v4-pro-discounted": {
        "display_name": "DeepSeek V4 Pro (Discounted)",
        "input_per_million": 0.435,
        "cached_input_per_million": 0.003625,
        "output_per_million": 0.87,
        "currency": "USD",
        "source": "Configured from official DeepSeek V4 Pro discounted pricing on 2026-05-20.",
        "notes": [
            "Reflects the discounted profile, not the standard non-promo rate.",
            "Verify current DeepSeek pricing before making billing decisions.",
        ],
    },
    "deepseek-v4-flash-discounted": {
        "display_name": "DeepSeek V4 Flash (Discounted)",
        "input_per_million": 0.14,
        "cached_input_per_million": 0.0028,
        "output_per_million": 0.28,
        "currency": "USD",
        "source": "Configured from official DeepSeek V4 Flash discounted pricing on 2026-05-20.",
        "notes": [
            "Reflects the discounted profile, not the standard non-promo rate.",
            "Verify current DeepSeek pricing before making billing decisions.",
        ],
    },
}


@dataclass
class SessionRecord:
    session_id: str
    file: str
    source: str
    session_timestamp: str
    local_day: str
    total_tokens: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    model_context_window: int | None
    plan_type: str | None

    @property
    def non_cached_input_tokens(self) -> int:
        return max(self.input_tokens - self.cached_input_tokens, 0)

    @property
    def cached_input_ratio(self) -> float | None:
        if self.input_tokens <= 0:
            return None
        return self.cached_input_tokens / float(self.input_tokens)


def resolve_codex_home(raw_path: str | None) -> Path:
    if raw_path:
        return Path(raw_path).expanduser().resolve()
    env_path = os.environ.get("CODEX_HOME")
    if env_path:
        return Path(env_path).expanduser().resolve()
    return (Path.home() / ".codex").resolve()


def local_timezone() -> timezone:
    return datetime.now().astimezone().tzinfo or timezone.utc


def parse_day(value: str) -> date:
    return date.fromisoformat(value)


def month_bounds(value: str) -> tuple[date, date]:
    year_str, month_str = value.split("-", 1)
    year = int(year_str)
    month = int(month_str)
    start = date(year, month, 1)
    if month == 12:
        end = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        end = date(year, month + 1, 1) - timedelta(days=1)
    return start, end


def resolve_period_range(period: str, tzinfo: timezone) -> tuple[date | None, date | None, str]:
    today = datetime.now(tzinfo).date()

    if period == "today":
        return today, today, PERIOD_DISPLAY_NAMES["today"]
    if period == "yesterday":
        yesterday = today - timedelta(days=1)
        return yesterday, yesterday, PERIOD_DISPLAY_NAMES["yesterday"]
    if period == "last7":
        start = today - timedelta(days=6)
        return start, today, PERIOD_DISPLAY_NAMES["last7"]
    if period == "this-month":
        start = date(today.year, today.month, 1)
        return start, today, PERIOD_DISPLAY_NAMES["this-month"]
    if period == "all":
        return None, None, PERIOD_DISPLAY_NAMES["all"]
    raise ValueError(f"Unsupported period: {period}")


def months_in_range(start_day: date, end_day: date) -> list[tuple[int, int]]:
    cursor = date(start_day.year, start_day.month, 1)
    result: list[tuple[int, int]] = []
    while cursor <= end_day:
        result.append((cursor.year, cursor.month))
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    return result


def candidate_files(codex_home: Path, start_day: date | None, end_day: date | None) -> list[Path]:
    files: list[Path] = []
    sessions_root = codex_home / "sessions"
    archived_root = codex_home / "archived_sessions"

    if start_day is None or end_day is None:
        if sessions_root.exists():
            files.extend(sorted(sessions_root.rglob(f"*{JSON_SUFFIX}")))
        if archived_root.exists():
            files.extend(sorted(archived_root.glob(f"*{JSON_SUFFIX}")))
        return [path for path in files if path.is_file()]

    for year, month in months_in_range(start_day, end_day):
        session_month_root = sessions_root / f"{year:04d}" / f"{month:02d}"
        if session_month_root.exists():
            files.extend(sorted(session_month_root.rglob(f"*{JSON_SUFFIX}")))
        if archived_root.exists():
            files.extend(sorted(archived_root.glob(f"rollout-{year:04d}-{month:02d}*{JSON_SUFFIX}")))
    return [path for path in files if path.is_file()]


def read_first_line(path: Path) -> str:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        return handle.readline()


def read_tail_lines(path: Path, line_limit: int) -> list[str]:
    if line_limit <= 0:
        return []

    newline_target = line_limit + 1
    data = bytearray()
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        file_size = handle.tell()
        while file_size > 0 and data.count(b"\n") < newline_target:
            read_size = min(DEFAULT_TAIL_CHUNK_SIZE, file_size)
            file_size -= read_size
            handle.seek(file_size)
            data = handle.read(read_size) + data
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if len(lines) > line_limit:
        return lines[-line_limit:]
    return lines


def parse_json_line(line: str) -> dict[str, Any] | None:
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if isinstance(obj, dict):
        return obj
    return None


def parse_timestamp_from_filename(path: Path, tzinfo: timezone) -> datetime | None:
    match = FILENAME_TS_RE.match(path.stem)
    if not match:
        return None
    local_dt = datetime.strptime(
        f"{match.group('day')} {match.group('hour')}:{match.group('minute')}:{match.group('second')}",
        "%Y-%m-%d %H:%M:%S",
    ).replace(tzinfo=tzinfo)
    return local_dt


def parse_timestamp(text: str, tzinfo: timezone) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tzinfo)
    return parsed.astimezone(tzinfo)


def parse_session_file(path: Path, tzinfo: timezone, line_limit: int) -> SessionRecord | None:
    first_line = read_first_line(path)
    tail_lines = read_tail_lines(path, line_limit)

    session_meta: dict[str, Any] | None = None
    first_obj = parse_json_line(first_line)
    if first_obj and first_obj.get("type") == SESSION_META_EVENT:
        payload = first_obj.get("payload")
        if isinstance(payload, dict):
            session_meta = payload

    last_payload: dict[str, Any] | None = None
    for line in reversed(tail_lines):
        if TOKEN_COUNT_EVENT not in line:
            continue
        obj = parse_json_line(line)
        if not obj or obj.get("type") != "event_msg":
            continue
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("type") != TOKEN_COUNT_EVENT:
            continue
        info = payload.get("info")
        if isinstance(info, dict):
            last_payload = payload
            break

    if not last_payload:
        return None

    session_id = path.stem
    session_ts = None
    if session_meta:
        raw_session_id = session_meta.get("id")
        if isinstance(raw_session_id, str) and raw_session_id:
            session_id = raw_session_id
        raw_ts = session_meta.get("timestamp")
        if isinstance(raw_ts, str) and raw_ts:
            session_ts = parse_timestamp(raw_ts, tzinfo)
    if session_ts is None:
        session_ts = parse_timestamp_from_filename(path, tzinfo)
    if session_ts is None:
        return None

    info = last_payload["info"]
    usage = info.get("total_token_usage")
    if not isinstance(usage, dict):
        return None
    rate_limits = last_payload.get("rate_limits")
    plan_type = None
    if isinstance(rate_limits, dict):
        raw_plan = rate_limits.get("plan_type")
        if isinstance(raw_plan, str):
            plan_type = raw_plan

    return SessionRecord(
        session_id=session_id,
        file=str(path),
        source="archived" if "archived_sessions" in str(path) else "sessions",
        session_timestamp=session_ts.isoformat(),
        local_day=session_ts.date().isoformat(),
        total_tokens=int(usage.get("total_tokens", 0)),
        input_tokens=int(usage.get("input_tokens", 0)),
        cached_input_tokens=int(usage.get("cached_input_tokens", 0)),
        output_tokens=int(usage.get("output_tokens", 0)),
        reasoning_output_tokens=int(usage.get("reasoning_output_tokens", 0)),
        model_context_window=int(info.get("model_context_window")) if info.get("model_context_window") else None,
        plan_type=plan_type,
    )


def dedupe_sessions(records: list[SessionRecord]) -> list[SessionRecord]:
    buckets: dict[str, list[SessionRecord]] = defaultdict(list)
    for record in records:
        buckets[record.session_id].append(record)

    deduped: list[SessionRecord] = []
    for session_id in sorted(buckets):
        best = sorted(
            buckets[session_id],
            key=lambda record: (record.total_tokens, record.session_timestamp, record.file),
            reverse=True,
        )[0]
        deduped.append(best)
    return deduped


def filter_records(records: list[SessionRecord], start_day: date | None, end_day: date | None) -> list[SessionRecord]:
    if start_day is None or end_day is None:
        return list(records)
    return [
        record
        for record in records
        if start_day <= date.fromisoformat(record.local_day) <= end_day
    ]


def load_records(
    codex_home: Path,
    *,
    start_day: date | None = None,
    end_day: date | None = None,
    tail_line_limit: int = DEFAULT_TAIL_LINE_LIMIT,
    tzinfo: timezone | None = None,
) -> list[SessionRecord]:
    tzinfo = tzinfo or local_timezone()
    files = candidate_files(codex_home, start_day, end_day)
    records = [
        record
        for record in (
            parse_session_file(path, tzinfo, tail_line_limit)
            for path in files
        )
        if record is not None
    ]
    records = dedupe_sessions(records)
    records = filter_records(records, start_day, end_day)
    records.sort(key=lambda record: (record.session_timestamp, record.file))
    return records


def sum_field(records: list[SessionRecord], field_name: str) -> int:
    return sum(int(getattr(record, field_name)) for record in records)


def build_summary(records: list[SessionRecord], label: str) -> dict[str, Any]:
    input_tokens = sum_field(records, "input_tokens")
    cached_input_tokens = sum_field(records, "cached_input_tokens")
    output_tokens = sum_field(records, "output_tokens")
    reasoning_output_tokens = sum_field(records, "reasoning_output_tokens")
    total_tokens = sum_field(records, "total_tokens")
    non_cached_input_tokens = max(input_tokens - cached_input_tokens, 0)
    cache_ratio = (cached_input_tokens / float(input_tokens)) if input_tokens else None

    return {
        "label": label,
        "session_count": len(records),
        "total_tokens": total_tokens,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "non_cached_input_tokens": non_cached_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        "cached_input_ratio": cache_ratio,
        "cached_input_ratio_pct": round(cache_ratio * 100.0, 2) if cache_ratio is not None else None,
    }


def build_daily(records: list[SessionRecord]) -> list[dict[str, Any]]:
    buckets: dict[str, list[SessionRecord]] = defaultdict(list)
    for record in records:
        buckets[record.local_day].append(record)

    rows: list[dict[str, Any]] = []
    for day in sorted(buckets):
        group = buckets[day]
        row = build_summary(group, day)
        row["day"] = day
        rows.append(row)
    return rows


def build_monthly(records: list[SessionRecord]) -> list[dict[str, Any]]:
    buckets: dict[str, list[SessionRecord]] = defaultdict(list)
    for record in records:
        month = record.local_day[:7]
        buckets[month].append(record)

    rows: list[dict[str, Any]] = []
    for month in sorted(buckets):
        row = build_summary(buckets[month], month)
        row["month"] = month
        rows.append(row)
    return rows


def normalize_pricing(price_profile_name: str | None) -> dict[str, Any] | None:
    if not price_profile_name:
        return None
    if price_profile_name not in PRICING_PROFILES:
        known_profiles = ", ".join(sorted(PRICING_PROFILES))
        raise ValueError(f"Unknown price profile {price_profile_name!r}. Known profiles: {known_profiles}.")
    profile = dict(PRICING_PROFILES[price_profile_name])
    profile["name"] = price_profile_name
    return profile


def cost_estimate_from_summary(summary: dict[str, Any], pricing: dict[str, Any]) -> dict[str, Any]:
    input_cost = summary["non_cached_input_tokens"] / 1_000_000.0 * pricing["input_per_million"]
    cached_input_cost = summary["cached_input_tokens"] / 1_000_000.0 * pricing["cached_input_per_million"]
    output_cost = summary["output_tokens"] / 1_000_000.0 * pricing["output_per_million"]
    total_cost = input_cost + cached_input_cost + output_cost
    return {
        "price_profile": pricing["name"],
        "display_name": pricing["display_name"],
        "currency": pricing["currency"],
        "source": pricing["source"],
        "notes": pricing["notes"],
        "input_price_per_million": pricing["input_per_million"],
        "cached_input_price_per_million": pricing["cached_input_per_million"],
        "output_price_per_million": pricing["output_per_million"],
        "input_cost": round(input_cost, 2),
        "cached_input_cost": round(cached_input_cost, 2),
        "output_cost": round(output_cost, 2),
        "total_cost": round(total_cost, 2),
    }


def session_to_dict(record: SessionRecord, pricing: dict[str, Any] | None) -> dict[str, Any]:
    item = asdict(record)
    item["non_cached_input_tokens"] = record.non_cached_input_tokens
    item["cached_input_ratio"] = record.cached_input_ratio
    item["cached_input_ratio_pct"] = round(record.cached_input_ratio * 100.0, 2) if record.cached_input_ratio is not None else None
    if pricing:
        item["cost_estimate"] = cost_estimate_from_summary(
            {
                "non_cached_input_tokens": record.non_cached_input_tokens,
                "cached_input_tokens": record.cached_input_tokens,
                "output_tokens": record.output_tokens,
            },
            pricing,
        )
    return item


def build_price_comparison(summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    comparison: dict[str, dict[str, Any]] = {}
    for name in sorted(PRICING_PROFILES):
        pricing = normalize_pricing(name)
        assert pricing is not None
        comparison[name] = cost_estimate_from_summary(summary, pricing)
    return comparison


def build_report(
    records: list[SessionRecord],
    label: str,
    *,
    pricing: dict[str, Any] | None = None,
    top_sessions: int = 10,
) -> dict[str, Any]:
    summary = build_summary(records, label)
    report: dict[str, Any] = {
        "summary": summary,
        "daily": build_daily(records),
        "top_sessions": sorted(
            (session_to_dict(record, pricing) for record in records),
            key=lambda item: item["total_tokens"],
            reverse=True,
        )[: max(top_sessions, 0)],
        "plan_type_counts": dict(Counter(record.plan_type or "unknown" for record in records)),
        "cost_comparison": build_price_comparison(summary),
    }
    if pricing:
        report["cost_estimate"] = cost_estimate_from_summary(summary, pricing)
    return report


def serialize_price_profiles() -> dict[str, dict[str, Any]]:
    payload: dict[str, dict[str, Any]] = {}
    for name in sorted(PRICING_PROFILES):
        profile = dict(PRICING_PROFILES[name])
        profile["name"] = name
        payload[name] = profile
    return payload


def build_dashboard_payload(
    codex_home: Path,
    records: list[SessionRecord],
    *,
    price_profile_name: str = "gpt-5.5-standard",
    top_sessions: int = 8,
    tzinfo: timezone | None = None,
) -> dict[str, Any]:
    tzinfo = tzinfo or local_timezone()
    pricing = normalize_pricing(price_profile_name)
    assert pricing is not None

    periods: dict[str, Any] = {}
    for period_key in ["today", "yesterday", "last7", "this-month", "all"]:
        start_day, end_day, label = resolve_period_range(period_key, tzinfo)
        filtered = filter_records(records, start_day, end_day)
        report = build_report(filtered, label, pricing=pricing, top_sessions=top_sessions)
        report["key"] = period_key
        report["display_name"] = PERIOD_DISPLAY_NAMES[period_key]
        report["range_start"] = start_day.isoformat() if start_day else None
        report["range_end"] = end_day.isoformat() if end_day else None
        periods[period_key] = report

    daily_last_30 = sorted(build_daily(filter_records(records, datetime.now(tzinfo).date() - timedelta(days=29), datetime.now(tzinfo).date())), key=lambda row: row["day"])
    monthly = build_monthly(records)

    return {
        "meta": {
            "generated_at": datetime.now(tzinfo).isoformat(),
            "timezone": str(tzinfo),
            "codex_home": str(codex_home),
            "record_count": len(records),
            "active_price_profile": price_profile_name,
            "available_price_profiles": serialize_price_profiles(),
        },
        "periods": periods,
        "charts": {
            "daily_last_30": daily_last_30,
            "monthly": monthly,
        },
    }
