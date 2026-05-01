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
| `OPA_SERVER` | Server base URL. | `http://localhost:10000` |
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
opa conv get <id> [--detail]                    --detail replays the full thinking trace in a TUI
opa conv history <id> [--limit N] [--offset N]
opa conv send <id> "message" [--raw|--json] [--no-reasoning]
opa conv attach <id>                             Watch a live run (no message sent)
opa conv rename <id> "new title"
opa conv cancel <run_id>
opa conv delete <id>
opa conv delete-all

opa chat [<conversation_id>] [-t <title>]       Interactive TUI REPL

opa proc list | get <pid> | stop <pid>
opa proc stream                                  Live SSE process snapshots
opa proc stdin <pid> [--text … | --keys Up,Enter | <stdin>] [--line-ending lf|crlf|none]
opa proc resize <pid> --cols N --rows N
opa proc attach <pid> [--no-resize]              Interactive PTY attach (Ctrl-\ to detach)
opa proc autostart list | add --pid <pid> [--force] | delete <id> | run <id>

opa skill-events list
opa skill-events delete <id>
opa skill-events simulate <id> [--filename foo.md]   Reads content from stdin
opa skill-events stream                              Admin SSE snapshot
opa skill-events notifications [--since <millis>]    Per-profile notifications
opa skill-events events <skill>                      Events declared by the skill
opa skill-events listener-status <skill>             Heartbeat-derived liveness
opa skill-events listener-start <skill>              Spawn the listener daemon

opa agents list
opa agents add --type a2a|mcp [--url …] [--json-config …]
              [--system-prompt …] [--description …]
              [--llm-provider …] [--llm-model …] [--reasoning-effort …]
opa agents delete <tool_id>
opa agents enable | disable <tool_id>
opa agents reconnect <tool_id>
opa agents auth-url <tool_id> [--return-url …]
opa agents unlink <tool_id>
opa agents config get <tool_id>
opa agents config set <tool_id> [--llm-provider …] [--llm-model …]
                                [--reasoning-effort …] [--full-reasoning …]
                                [--system-prompt …] [--description …]

opa setup status [--profile X]                  Unauthenticated
opa setup complete --profile X --json '{…}'     Unauthenticated; prints the JWT
opa setup complete --json-file path             (or --json-file -, reads stdin)
opa setup reset-orphaned                        Unauthenticated
opa setup reconfigure                           Admin auth
opa setup server-config get [<key>]
opa setup server-config set KEY=VAL [KEY=VAL …]

opa tools register-long-running <tool_id> [--force]
```

`--json` (global) flips any command to machine-readable JSON output. For
`conv send`, three output modes are available:

- **default** — TUI renders the run as two clearly separated sections. The
  **── Thinking Process ──** section shows each ReAct step
  (`◇ Thought`, `→ Action`, `Input` as pretty-printed JSON, `◂ Observation`),
  followed by the **── Response ──** section with the assistant's final
  answer streamed token-by-token. When the run finishes the screen stays up
  with `done · press ESC to exit` until you dismiss it.
- `--raw` — assistant text only, plain stdout. Pipe-friendly:
  `opa conv send $id "explain X" --raw | tee out.md`
- `--json` — each SSE event verbatim as a JSONL line.

`opa conv get <id> --detail` opens the same TUI but replays a *completed*
conversation from history — useful for reviewing the full thinking trace of
an earlier run. Press `ESC` to exit.

## TUI keys (`opa chat`, default `conv send`/`conv attach`, `conv get --detail`)

| Key | Action |
|---|---|
| `Enter` | Send message (interactive `chat` only) |
| `Esc` | Exit the TUI |
| `Ctrl+C` | Cancel current run, or exit if idle |
| `Ctrl+D` | Exit |
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

## Interactive PTY attach

`opa proc attach <pid>` puts the local terminal into raw mode and pumps
keystrokes / output through the same WebSocket the desktop app uses. Ctrl-\
detaches without killing the remote process; the remote sees Ctrl-C, Ctrl-D,
arrow keys, and resize events as if you'd typed them locally.

The bearer token is sent via the `Sec-WebSocket-Protocol` header
(`bearer, <token>`) — matching openpa-ui because browsers can't set
`Authorization` on the native WebSocket constructor.
