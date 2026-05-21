#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parent
WEB_ROOT = PLUGIN_ROOT / "web"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from usage_core import (  # noqa: E402
    DEFAULT_TAIL_LINE_LIMIT,
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


class DashboardState:
    def __init__(self, codex_home: Path, tail_line_limit: int, top_sessions: int, cache_ttl_seconds: int) -> None:
        self.codex_home = codex_home
        self.tail_line_limit = tail_line_limit
        self.top_sessions = top_sessions
        self.cache_ttl_seconds = cache_ttl_seconds
        self._records = None
        self._loaded_at = 0.0

    def get_records(self, force: bool = False):
        now = time.time()
        if force or self._records is None or now - self._loaded_at > self.cache_ttl_seconds:
            self._records = load_records(
                self.codex_home,
                tail_line_limit=self.tail_line_limit,
            )
            self._loaded_at = now
        return self._records

    def dashboard_payload(self, price_profile: str, force: bool = False) -> dict:
        records = self.get_records(force=force)
        return build_dashboard_payload(
            self.codex_home,
            records,
            price_profile_name=price_profile,
            top_sessions=self.top_sessions,
        )

    def report_payload(
        self,
        *,
        period: str,
        month: str | None,
        from_date: str | None,
        to_date: str | None,
        price_profile: str,
        top_sessions: int | None,
        force: bool = False,
    ) -> dict:
        pricing = normalize_pricing(price_profile)
        if pricing is None:
            raise ValueError("Missing price profile.")

        tzinfo = local_timezone()
        records = self.get_records(force=force)
        if month:
            start_day, end_day = month_bounds(month)
            label = month
        elif from_date or to_date:
            if not from_date or not to_date:
                raise ValueError("from_date and to_date must be used together.")
            start_day = parse_day(from_date)
            end_day = parse_day(to_date)
            if end_day < start_day:
                raise ValueError("to_date must not be earlier than from_date.")
            label = f"{start_day.isoformat()}..{end_day.isoformat()}"
        else:
            start_day, end_day, label = resolve_period_range(period, tzinfo)

        filtered = filter_records(records, start_day, end_day)
        payload = build_report(
            filtered,
            label,
            pricing=pricing,
            top_sessions=top_sessions or self.top_sessions,
        )
        payload["meta"] = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "codex_home": str(self.codex_home),
            "price_profile": price_profile,
            "range_start": start_day.isoformat() if start_day else None,
            "range_end": end_day.isoformat() if end_day else None,
            "parsed_session_count": len(filtered),
        }
        return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the Codex Usage Inspector dashboard.")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind.")
    parser.add_argument("--port", type=int, default=8765, help="Port to bind.")
    parser.add_argument("--codex-home", help="Codex home directory. Defaults to $CODEX_HOME or ~/.codex.")
    parser.add_argument("--tail-line-limit", type=int, default=DEFAULT_TAIL_LINE_LIMIT, help="Tail lines to inspect for the final token snapshot.")
    parser.add_argument("--top-sessions", type=int, default=8, help="Top sessions shown in the dashboard.")
    parser.add_argument("--cache-ttl-seconds", type=int, default=300, help="How long the parsed record cache stays hot.")
    return parser.parse_args()


def make_handler(state: DashboardState):
    class UsageHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

        def log_message(self, format: str, *args) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/dashboard":
                self.handle_dashboard(parsed)
                return
            if parsed.path == "/api/report":
                self.handle_report(parsed)
                return
            super().do_GET()

        def handle_dashboard(self, parsed) -> None:
            query = parse_qs(parsed.query)
            price_profile = query.get("price_profile", ["gpt-5.5-standard"])[0]
            force = query.get("refresh", ["0"])[0] == "1"
            try:
                payload = state.dashboard_payload(price_profile, force=force)
            except Exception as exc:  # noqa: BLE001
                self.send_json({"error": str(exc)}, status=500)
                return
            self.send_json(payload)

        def handle_report(self, parsed) -> None:
            query = parse_qs(parsed.query)
            period = query.get("period", ["this-month"])[0]
            month = query.get("month", [None])[0]
            from_date = query.get("from_date", [None])[0]
            to_date = query.get("to_date", [None])[0]
            price_profile = query.get("price_profile", ["gpt-5.5-standard"])[0]
            top_sessions_raw = query.get("top_sessions", [None])[0]
            top_sessions = int(top_sessions_raw) if top_sessions_raw else None
            force = query.get("refresh", ["0"])[0] == "1"
            try:
                payload = state.report_payload(
                    period=period,
                    month=month,
                    from_date=from_date,
                    to_date=to_date,
                    price_profile=price_profile,
                    top_sessions=top_sessions,
                    force=force,
                )
            except Exception as exc:  # noqa: BLE001
                self.send_json({"error": str(exc)}, status=400)
                return
            self.send_json(payload)

        def send_json(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return UsageHandler


def main() -> int:
    args = parse_args()
    codex_home = resolve_codex_home(args.codex_home)
    state = DashboardState(
        codex_home=codex_home,
        tail_line_limit=args.tail_line_limit,
        top_sessions=args.top_sessions,
        cache_ttl_seconds=args.cache_ttl_seconds,
    )
    handler = make_handler(state)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Serving Codex Usage Inspector at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
