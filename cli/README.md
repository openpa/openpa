# `opa` — OpenPA Command-Line Client

A lightweight Go CLI that hits the same APIs as `openpa-ui`. Useful when you
want terminal access to OpenPA without firing up the desktop app: list and
configure tools/skills, manage LLM providers, run conversations with streamed
thinking, and tail processes and skill events.

The binary is named `opa`.

## Quick start

```bash
cd cli
go mod tidy
go build -o opa .

# Provide credentials via an environment variable — the CLI does not mint tokens.
# Get OPA_TOKEN from your OpenPA admin or from openpa-ui. The active profile
# is resolved server-side from the token's claims, so no profile env var is
# needed.
export OPA_TOKEN="<your JWT>"

# Sanity check
./opa me
./opa tools list
```

`go build` produces `opa` on Linux/macOS and `opa.exe` on Windows.

During development you can skip the build step entirely with `go run .` —
e.g. `go run . tools list`.

## Configuration (environment variables)

| Variable | Purpose | Default |
|---|---|---|
| `OPA_TOKEN` | JWT bearer token. Required. Obtain from your OpenPA admin or openpa-ui — the CLI does not mint tokens. The active profile is resolved server-side from the token's claims. | — |
| `OPA_SERVER` | Server base URL. | `http://localhost:8000` |
| `OPA_OUTPUT` | `table` (default) or `json`. | `table` |
| `OPA_NO_COLOR` | Set to disable ANSI colors and table borders. | unset |

The token is deliberately **not** a CLI flag — keeping it in an env var
prevents it from leaking into shell history or `ps` output.

## Commands

```
opa me                                          Show identity from current token

opa profile list | get <n> | create <n> | delete <n>
opa profile persona get|set <n>                 Set reads from stdin
opa profile skill-mode get|set <n> manual|automatic

opa tools list [--type built-in|mcp|a2a|skill|intrinsic]
opa tools get <tool_id>
opa tools enable | disable <tool_id>
opa tools set-var <tool_id> KEY=VAL [KEY=VAL …]
opa tools set-args <tool_id> --json '<JSON object>'
opa tools set-llm <tool_id> [--provider X] [--model Y]
                            [--reasoning-effort low|medium|high]
                            [--full-reasoning true|false]
opa tools reset-llm <tool_id> <key> [key …]

opa llm providers list | models <name>
opa llm providers configure <name> [--api-key …] [--auth-method …] [--json '<extra>']
opa llm providers delete-config <name>
opa llm model-groups get | set [--high <model>] [--low <model>] [--default-provider X]
opa llm device-code start                       GitHub Copilot device-code flow
opa llm device-code poll <device_code>          Polls until complete

opa config schema | get [<key>] | set <key> <value> | reset <key>

opa conv list [--limit N] [--offset N]
opa conv new [--title "…"]
opa conv get <id>
opa conv history <id> [--limit N] [--offset N]
opa conv send <id> "message" [--raw|--json] [--no-reasoning]
opa conv attach <id>                             Watch a live run (no message sent)
opa conv cancel <run_id>
opa conv delete <id>
opa conv delete-all

opa chat [<conversation_id>] [-t <title>]       Interactive TUI REPL

opa proc list | get <pid> | stop <pid>
opa proc stream                                  Live SSE process snapshots

opa skill-events list
opa skill-events delete <id>
opa skill-events simulate <id> [--filename foo.md]   Reads content from stdin
opa skill-events stream                              Admin SSE snapshot
opa skill-events notifications [--since <millis>]    Per-profile notifications
```

`--json` (global) flips any command to machine-readable JSON output. For
`conv send`, three output modes are available:

- **default** — TUI renders thinking, text, terminal output, and other events
  with color and a status bar.
- `--raw` — assistant text only, plain stdout. Pipe-friendly:
  `opa conv send $id "explain X" --raw | tee out.md`
- `--json` — each SSE event verbatim as a JSONL line.

## TUI keys (`opa chat` and the default `conv send` view)

| Key | Action |
|---|---|
| `Enter` | Send message (interactive `chat` only) |
| `Ctrl+C` | Cancel current run, or quit if idle |
| `Ctrl+D` | Quit |
| `PgUp` / `PgDn` | Scroll history |

## Layout

```
cli/
├── main.go                         entry point
├── cmd/                            cobra commands (one file per group)
└── internal/
    ├── client/                     HTTP + SSE client wrapping the OpenPA API
    ├── config/                     env-var configuration
    ├── output/                     table/JSON output helpers
    ├── stream/                     subscribe-first → POST → render pipeline
    └── tui/                        bubbletea chat view
```

## Streaming sequence

For `conv send`, `conv attach`, and `chat`, the CLI mirrors the openpa-ui
flow: open the SSE stream first, wait for the `ready` event, then POST the
message. The server's stream bus replays in-progress events before the `ready`
marker, so subscribe-after-POST also works for normal-length runs — but
subscribe-first eliminates the rare race where a fast run completes between
POST and the SSE connection establishing.

## Deferred to a future version

- `opa proc attach <pid>` — WebSocket PTY attach (raw-mode terminal handling).
- `opa agents …` — A2A/MCP agent registration (`/api/agents`).
- `opa setup` — first-run wizard (`/api/config/setup`); for now, run setup
  through openpa-ui and then use the resulting token here.
