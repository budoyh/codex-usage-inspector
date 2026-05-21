#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
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
        self._dashboard_cache: dict[tuple[str, int], dict] = {}
        self._report_cache: dict[tuple[str, str | None, str | None, str | None, str, int], dict] = {}

    def get_records(self, force: bool = False):
        now = time.time()
        if force or self._records is None or now - self._loaded_at > self.cache_ttl_seconds:
            self._records = load_records(
                self.codex_home,
                tail_line_limit=self.tail_line_limit,
            )
            self._loaded_at = now
            self._dashboard_cache.clear()
            self._report_cache.clear()
        return self._records

    def dashboard_payload(self, price_profile: str, force: bool = False) -> dict:
        cache_key = (price_profile, self.top_sessions)
        if not force and cache_key in self._dashboard_cache:
            return self._dashboard_cache[cache_key]
        records = self.get_records(force=force)
        payload = build_dashboard_payload(
            self.codex_home,
            records,
            price_profile_name=price_profile,
            top_sessions=self.top_sessions,
        )
        self._dashboard_cache[cache_key] = payload
        return payload

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

        cache_key = (
            period,
            month,
            from_date,
            to_date,
            price_profile,
            top_sessions or self.top_sessions,
        )
        if not force and cache_key in self._report_cache:
            return self._report_cache[cache_key]

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
        self._report_cache[cache_key] = payload
        return payload


def format_int(value: int | None) -> str:
    if value is None:
        return "-"
    return f"{value:,}"


def format_ratio(summary: dict) -> str:
    ratio = summary.get("cached_input_ratio_pct")
    if ratio is None:
        return "-"
    return f"{ratio:.2f}%"


def format_money(cost: dict | None) -> str:
    if not cost:
        return "-"
    return f"{cost['total_cost']:.2f} {cost['currency']}"


def esc(value: object) -> str:
    return html.escape(str(value))


def render_cost_rows(cost_comparison: dict[str, dict]) -> str:
    rows: list[str] = []
    for cost in cost_comparison.values():
        rows.append(
            "\n".join(
                [
                    "<tr>",
                    f"  <td>{esc(cost['display_name'])}</td>",
                    f"  <td>{esc(format_money(cost))}</td>",
                    f"  <td>{esc(cost['currency'])}</td>",
                    "</tr>",
                ]
            )
        )
    return "\n".join(rows)


def render_session_rows(sessions: list[dict]) -> str:
    if not sessions:
        return '<tr><td colspan="4">No sessions in this range.</td></tr>'

    rows: list[str] = []
    for session in sessions:
        rows.append(
            "\n".join(
                [
                    "<tr>",
                    f"  <td>{esc(session['session_timestamp'])}</td>",
                    f"  <td>{esc(format_int(session['total_tokens']))}</td>",
                    f"  <td>{esc(session.get('cached_input_ratio_pct'))}%</td>",
                    f"  <td>{esc(session['source'])}</td>",
                    "</tr>",
                ]
            )
        )
    return "\n".join(rows)


def render_daily_rows(daily: list[dict]) -> str:
    rows: list[str] = []
    for row in daily[-12:]:
        rows.append(
            "\n".join(
                [
                    "<tr>",
                    f"  <td>{esc(row['day'])}</td>",
                    f"  <td>{esc(format_int(row['total_tokens']))}</td>",
                    f"  <td>{esc(row['session_count'])}</td>",
                    f"  <td>{esc(format_ratio(row))}</td>",
                    "</tr>",
                ]
            )
        )
    return "\n".join(rows)


def render_period_cards(periods: dict[str, dict]) -> str:
    cards: list[str] = []
    for period in periods.values():
        summary = period["summary"]
        cost = period.get("cost_estimate")
        cards.append(
            "\n".join(
                [
                    '<section class="card">',
                    f'  <div class="card-label">{esc(period["display_name"])}</div>',
                    f'  <div class="card-value">{esc(format_int(summary["total_tokens"]))}</div>',
                    f'  <div class="card-sub">sessions: {esc(summary["session_count"])}</div>',
                    f'  <div class="card-sub">cache ratio: {esc(format_ratio(summary))}</div>',
                    f'  <div class="card-sub">cost: {esc(format_money(cost))}</div>',
                    "</section>",
                ]
            )
        )
    return "\n".join(cards)


def render_dashboard_page(payload: dict) -> str:
    this_month = payload["periods"]["this-month"]
    summary = this_month["summary"]
    cost = this_month.get("cost_estimate")
    generated_at = payload["meta"]["generated_at"]
    cost_rows = render_cost_rows(this_month["cost_comparison"])
    session_rows = render_session_rows(this_month["top_sessions"])
    daily_rows = render_daily_rows(payload["charts"]["daily_last_30"])
    period_cards = render_period_cards(payload["periods"])

    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Codex Usage Inspector</title>
    <style>
      :root {{
        color-scheme: dark;
        --bg: #0f1415;
        --panel: #151b1c;
        --panel-2: #1b2325;
        --border: #2b3638;
        --text: #eef5f5;
        --muted: #9db0b3;
        --accent: #2dd4bf;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        background: var(--bg);
        color: var(--text);
        font: 14px/1.5 Inter, "Segoe UI", system-ui, sans-serif;
      }}
      .app {{ padding: 12px; }}
      .panel {{
        background: var(--panel);
        border: 1px solid var(--border);
        border-radius: 10px;
        padding: 14px;
      }}
      h1, h2, p {{ margin: 0; }}
      h1 {{ font-size: 22px; line-height: 1.2; }}
      h2 {{ font-size: 15px; margin-bottom: 10px; }}
      .eyebrow {{
        color: var(--accent);
        font-size: 11px;
        text-transform: uppercase;
        margin-bottom: 8px;
      }}
      .sub {{
        color: var(--muted);
        margin-top: 6px;
      }}
      .hero {{
        display: grid;
        grid-template-columns: 1.6fr 1fr;
        gap: 12px;
        margin-bottom: 14px;
      }}
      .hero-box, .section {{
        background: var(--panel-2);
        border: 1px solid var(--border);
        border-radius: 8px;
        padding: 12px;
      }}
      .hero-value {{
        font-size: 28px;
        line-height: 1.05;
        margin-top: 8px;
      }}
      .metrics {{
        display: grid;
        gap: 8px;
      }}
      .metric {{
        display: flex;
        justify-content: space-between;
        gap: 8px;
      }}
      .metric-label {{ color: var(--muted); }}
      .cards {{
        display: grid;
        grid-template-columns: repeat(5, minmax(0, 1fr));
        gap: 10px;
        margin-bottom: 14px;
      }}
      .card {{
        background: var(--panel-2);
        border: 1px solid var(--border);
        border-radius: 8px;
        padding: 12px;
      }}
      .card-label {{
        color: var(--muted);
        font-size: 12px;
        margin-bottom: 8px;
      }}
      .card-value {{
        font-size: 18px;
        line-height: 1.1;
        margin-bottom: 6px;
      }}
      .card-sub {{
        color: var(--muted);
        font-size: 12px;
      }}
      .grid {{
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 12px;
      }}
      table {{
        width: 100%;
        border-collapse: collapse;
      }}
      th, td {{
        text-align: left;
        padding: 8px 6px;
        border-bottom: 1px solid var(--border);
        vertical-align: top;
      }}
      th {{
        color: var(--muted);
        font-weight: 500;
      }}
      @media (max-width: 860px) {{
        .hero, .grid {{ grid-template-columns: 1fr; }}
        .cards {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      }}
    </style>
  </head>
  <body>
    <main class="app">
      <section class="panel">
        <div class="eyebrow">Codex Usage Inspector</div>
        <div class="hero">
          <div class="hero-box">
            <h1>This month</h1>
            <div class="hero-value">{esc(format_int(summary["total_tokens"]))}</div>
            <p class="sub">Generated from local Codex logs at {esc(generated_at)}</p>
          </div>
          <div class="hero-box metrics">
            <div class="metric"><span class="metric-label">Sessions</span><strong>{esc(summary["session_count"])}</strong></div>
            <div class="metric"><span class="metric-label">Input</span><strong>{esc(format_int(summary["input_tokens"]))}</strong></div>
            <div class="metric"><span class="metric-label">Cached input</span><strong>{esc(format_int(summary["cached_input_tokens"]))}</strong></div>
            <div class="metric"><span class="metric-label">Output</span><strong>{esc(format_int(summary["output_tokens"]))}</strong></div>
            <div class="metric"><span class="metric-label">Cache ratio</span><strong>{esc(format_ratio(summary))}</strong></div>
            <div class="metric"><span class="metric-label">GPT-5.5 cost</span><strong>{esc(format_money(cost))}</strong></div>
          </div>
        </div>

        <div class="cards">
          {period_cards}
        </div>

        <div class="grid">
          <section class="section">
            <h2>Price comparison for this month</h2>
            <table>
              <thead>
                <tr><th>Profile</th><th>Total cost</th><th>Currency</th></tr>
              </thead>
              <tbody>
                {cost_rows}
              </tbody>
            </table>
          </section>

          <section class="section">
            <h2>Top sessions this month</h2>
            <table>
              <thead>
                <tr><th>Session</th><th>Total tokens</th><th>Cache</th><th>Source</th></tr>
              </thead>
              <tbody>
                {session_rows}
              </tbody>
            </table>
          </section>

          <section class="section">
            <h2>Recent daily usage</h2>
            <table>
              <thead>
                <tr><th>Day</th><th>Total tokens</th><th>Sessions</th><th>Cache</th></tr>
              </thead>
              <tbody>
                {daily_rows}
              </tbody>
            </table>
          </section>

          <section class="section">
            <h2>Scope</h2>
            <p class="sub">
              Numbers come from local Codex session logs, not official billing.
              Reasoning output is already included in output tokens and is not added twice.
            </p>
          </section>
        </div>
      </section>
    </main>
  </body>
</html>
"""


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
            if parsed.path == "/healthz":
                self.send_json({"ok": True})
                return
            if parsed.path == "/warmup":
                self.handle_warmup(parsed)
                return
            if parsed.path in {"/", "/dashboard"}:
                self.handle_dashboard_page(parsed)
                return
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

        def handle_dashboard_page(self, parsed) -> None:
            query = parse_qs(parsed.query)
            price_profile = query.get("price_profile", ["gpt-5.5-standard"])[0]
            force = query.get("refresh", ["0"])[0] == "1"
            try:
                payload = state.dashboard_payload(price_profile, force=force)
                body = render_dashboard_page(payload).encode("utf-8")
            except Exception as exc:  # noqa: BLE001
                self.send_json({"error": str(exc)}, status=500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def handle_warmup(self, parsed) -> None:
            query = parse_qs(parsed.query)
            price_profile = query.get("price_profile", ["gpt-5.5-standard"])[0]
            force = query.get("refresh", ["0"])[0] == "1"
            try:
                payload = state.dashboard_payload(price_profile, force=force)
            except Exception as exc:  # noqa: BLE001
                self.send_json({"error": str(exc)}, status=500)
                return
            summary = payload["periods"]["this-month"]["summary"]
            self.send_json(
                {
                    "ok": True,
                    "record_count": payload["meta"]["record_count"],
                    "this_month_total_tokens": summary["total_tokens"],
                }
            )

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
