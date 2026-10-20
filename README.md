# SaiHarness

A Windows desktop agent harness for local LLMs, built with PySide6. It talks to any OpenAI-compatible server (LM Studio by default, `http://localhost:1234`) and gives the model controlled access to tools: files, shell, web, Office documents, images, memory and MCP servers.

## Features

- Chat UI with multiple chats, model profiles (system prompt, temperature, max tokens) and switchable themes
- Context panel with attached files and a token estimator calibrated against real model measurements
- Tool router with meta-tools (`find_tool`, `describe_tool`, `call_tool`) to keep the prompt small
- Permission dialog before risky tool calls
- File, shell, web (fetch, render, SearXNG search), Office (docx/xlsx/pdf), vision and image generation tools
- Persistent memory panel and MCP server manager
- Local SQLite storage in `data/` (excluded from git)

## Requirements

- Windows 10/11
- Python 3.11+ (3.14 recommended)
- LM Studio or another OpenAI-compatible endpoint

## Quick start

```powershell
Run.cmd        # finds Python, creates .venv, installs dependencies, starts the app
Install.cmd    # same, without starting the app
```

Or run directly:

```powershell
.venv\Scripts\python app.py
```

## Tests

There is no pytest. Each `tests/test_*.py` is a standalone script that prints `OK`/`FAIL` lines.

```powershell
powershell -ExecutionPolicy Bypass -File tests\run_all.ps1          # offline suite
powershell -ExecutionPolicy Bypass -File tests\run_all.ps1 -Gui     # + real Qt window tests
powershell -ExecutionPolicy Bypass -File tests\run_all.ps1 -Live    # + tests against a running LM Studio
```

Manual test plan: `TEST_PLAN.md`. Developer notes for AI coding agents: `CLAUDE.md`.

## Project layout

```
app.py            entry point
worker.py         background LLM / request workers
tools_server.py   tool implementations exposed to the model
core/             LLM client, DB, context builder, tools, web, memory, MCP
ui/               PySide6 windows, panels and themes
scripts/          bootstrap and helper scripts
tests/            standalone test scripts
```
