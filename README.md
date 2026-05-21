# codex-usage-inspector

Inspect local Codex token usage, compare API-equivalent pricing, and open a local Browser dashboard from inside Codex.

![dashboard preview](plugins/codex-usage-inspector/assets/dashboard-preview.png)

## What this plugin provides

- MCP tools for token usage summaries
- A Browser-friendly local dashboard served from your machine
- Cost estimates for `GPT-5.5`, `DeepSeek V4 Pro (Discounted)`, and `DeepSeek V4 Flash (Discounted)`
- A CLI report generator for JSON, CSV, and Markdown exports

## Current UX

This version does **not** inject a permanent native badge into the Codex desktop shell.

Instead, it does two things:

1. `get_usage_summary` returns direct numeric answers for a time range
2. `show_usage_dashboard` starts a local dashboard server and returns a local URL such as `http://127.0.0.1:8765/dashboard`

That URL is meant to be opened in Codex's Browser surface.

## Install and run

### Plugin files

- MCP config: [plugins/codex-usage-inspector/.mcp.json](plugins/codex-usage-inspector/.mcp.json)
- Plugin manifest: [plugins/codex-usage-inspector/.codex-plugin/plugin.json](plugins/codex-usage-inspector/.codex-plugin/plugin.json)
- MCP entrypoint: [plugins/codex-usage-inspector/scripts/mcp_server.py](plugins/codex-usage-inspector/scripts/mcp_server.py)

### Dependencies

```bash
python -m pip install -r "plugins/codex-usage-inspector/requirements.txt"
```

### MCP tools

- `show_usage_dashboard`
- `get_usage_summary`

### Local HTTP debugging

Run the MCP server directly:

```bash
python "plugins/codex-usage-inspector/scripts/mcp_server.py" --transport streamable-http --port 8766
```

MCP endpoint:

- [http://127.0.0.1:8766/mcp](http://127.0.0.1:8766/mcp)

Run the standalone Browser dashboard server:

```bash
python "plugins/codex-usage-inspector/scripts/serve_dashboard.py" --host 127.0.0.1 --port 8765
```

Dashboard URL:

- [http://127.0.0.1:8765/dashboard](http://127.0.0.1:8765/dashboard)

### CLI reports

```bash
python "plugins/codex-usage-inspector/scripts/token_usage_report.py" --period this-month --json
python "plugins/codex-usage-inspector/scripts/token_usage_report.py" --period last7 --price-profile deepseek-v4-pro-discounted --json
```

## Repository layout

```text
.
|-- .agents/plugins/marketplace.json
|-- plugins/codex-usage-inspector/
|   |-- .app.json
|   |-- .codex-plugin/plugin.json
|   |-- .mcp.json
|   |-- requirements.txt
|   |-- skills/codex-usage-inspector/SKILL.md
|   |-- scripts/
|   |   |-- usage_core.py
|   |   |-- mcp_server.py
|   |   |-- serve_dashboard.py
|   |   `-- token_usage_report.py
|   |-- assets/
|   |   |-- dashboard-preview.png
|   |   `-- usage-widget.html
|   `-- web/
|       |-- app.css
|       |-- app.js
|       `-- index.html
`-- LICENSE
```

## Data model

- Reads only local Codex logs from `sessions/` and `archived_sessions/`
- Deduplicates repeated sessions
- Keeps only the final valid `token_count` snapshot per session
- Treats `reasoning_output_tokens` as a subset of `output_tokens`
- Reports local-log totals, not official vendor billing totals

## Performance notes

- First warmup can take minutes if your local log history is large
- The Browser dashboard now warms the heavy report before returning the URL
- Dashboard payloads and parsed records are cached in memory for faster follow-up views

## Pricing sources

- OpenAI GPT-5.5 standard API pricing: [OpenAI API Pricing](https://openai.com/api/pricing/)
- DeepSeek V4 pricing: [DeepSeek Pricing](https://api-docs.deepseek.com/quick_start/pricing/)

If official pricing changes, update `PRICING_PROFILES` in [plugins/codex-usage-inspector/scripts/usage_core.py](plugins/codex-usage-inspector/scripts/usage_core.py).

## Privacy

This plugin reads local Codex log files only. It does not fetch your vendor billing account and does not upload your log contents.
