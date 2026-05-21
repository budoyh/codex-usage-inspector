---
name: codex-usage-inspector
description: Inspect Codex local token usage, launch a visual dashboard, compare API-equivalent pricing profiles, and export daily or per-session reports. Use when the user asks for token totals, cached input ratio, top sessions, dashboard views, or GPT-5.5 vs DeepSeek cost conversion.
---

# Codex Usage Inspector

Use this plugin when the user wants a visual or exportable view of local Codex token usage.

## What it does

- Reads local Codex logs from `sessions/` and `archived_sessions/`
- Deduplicates repeated sessions
- Summarizes `input`, `cached input`, `non-cached input`, `output`, and `reasoning output`
- Estimates API-equivalent cost for multiple pricing profiles
- Starts a local dashboard server and opens a Browser-friendly panel inside Codex

## Primary commands

Inside Codex, prefer the MCP tools:

- `show_usage_dashboard`
- `get_usage_summary`

Fallback CLI:

```bash
python "plugins/codex-usage-inspector/scripts/token_usage_report.py" --period this-month --json
python "plugins/codex-usage-inspector/scripts/token_usage_report.py" --period last7 --price-profile deepseek-v4-pro-discounted --json
python "plugins/codex-usage-inspector/scripts/token_usage_report.py" --month 2026-05 --write-markdown reports/2026-05.md
```

## Reporting rules

- Always state that the numbers come from local Codex logs, not official account billing.
- Keep `reasoning_output_tokens` as an informative subset of `output_tokens`; do not add it a second time to cost.
- If cached input dominates, say so explicitly and interpret it as repeated reuse of large conversation context.
- If the user asks for a GUI or dashboard, prefer the Browser-friendly local dashboard URL instead of pasting a long text table.

## Scope note

This version provides a Browser-based local dashboard and MCP tools. It still does not inject a permanent native token badge into the Codex desktop shell.
