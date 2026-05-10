# OpenPA

Personal AI Assistant — server + CLI.

## Install

```bash
pip install openpa
```

After install, the `opa` command is on your PATH. Run `opa --help` for the
full command tree.

## Run the server

```bash
# Local dev — uses .env / dynaconf settings + SQLite at ~/.openpa/storage/openpa.db
opa serve

# Bind explicitly
opa serve --host 0.0.0.0 --port 1112
```

## Use the CLI against a running server

The CLI is configured via environment variables:

| Variable        | Default                  | Purpose                                |
| --------------- | ------------------------ | -------------------------------------- |
| `OPENPA_SERVER` | `http://localhost:1112`  | Server base URL                        |
| `OPENPA_TOKEN`  | (unset)                  | JWT bearer token for the OpenPA server |
| `OPA_OUTPUT`    | `table`                  | `table` or `json` output mode          |
| `OPA_NO_COLOR`  | (unset)                  | When set, disable ANSI colors          |

Obtain a JWT either from `openpa-ui` after first-run setup, or from
`opa setup complete` (which posts the setup payload and prints a token):

```bash
export OPENPA_SERVER="http://localhost:1112"
export OPENPA_TOKEN="..."

opa me                       # whoami
opa tools list               # list registered tools
opa conv list                # list conversations
opa chat                     # interactive chat REPL
opa proc attach <pid>        # attach to a long-running PTY process
```

## Development setup

```bash
# Install everything for local development.
uv sync --all-groups

# Now you can run both the server and the CLI from the project venv:
uv run opa serve          # in one terminal
uv run opa me             # in another, after exporting OPENPA_TOKEN
```

## DBeaver SQLite Configuration

In DBeaver, foreign key enforcement is off by default for SQLite. To enable
cascade deletes:

1. Right-click your SQLite connection → **Edit Connection**
2. Go to **Connection Settings** → **Initialization**
3. Add `PRAGMA foreign_keys=ON;` to the **Bootstrap queries** (or "Keep-Alive"
   section depending on your DBeaver version)
4. Reconnect

## Architecture

The CLI lives in [`app/cli/`](app/cli/) and ships in the same wheel as the
server. It communicates with the running server over HTTP / SSE /
WebSocket — there is no in-process backdoor for client commands; only
`opa serve` imports the server modules directly.

| Layer                    | Path                                              |
| ------------------------ | ------------------------------------------------- |
| typer entry point        | [`app/cli/main.py`](app/cli/main.py)              |
| Subcommands              | [`app/cli/commands/`](app/cli/commands/)          |
| HTTP / SSE / WS clients  | [`app/cli/client/`](app/cli/client/)              |
| Output (rich / TSV / JSON) | [`app/cli/output/`](app/cli/output/)            |
| Streaming pipeline       | [`app/cli/streaming.py`](app/cli/streaming.py)    |
| Chat TUI (prompt_toolkit) | [`app/cli/tui/`](app/cli/tui/)                  |
| Raw TTY + QR helpers     | [`app/cli/io/`](app/cli/io/)                      |
