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
# Get OPENPA_TOKEN from your OpenPA admin or from openpa-ui. The active profile
# is resolved server-side from the token's claims, so no profile env var is
# needed.
export OPENPA_TOKEN="<your JWT>"

# Sanity check
./opa me
./opa tools list
```

`go build` produces `opa` on Linux/macOS and `opa.exe` on Windows.

During development you can skip the build step entirely with `go run .` —
e.g. `go run . tools list`.

### Windows / PowerShell

If you manage Go versions with [`gvm-windows`](https://github.com/andrewkroh/gvm),
activate the toolchain in the current shell with:

```powershell
gvm --format=powershell 1.25.4 | Invoke-Expression
```

PowerShell 5.1 strips inner double quotes from native-binary arguments,
so flags that take JSON (e.g. `opa channels add --json '{"k":"v"}'`) tend
to fail with `invalid character …`. The CLI exposes quote-safe
alternatives where it matters — see `opa channels add --config k=v`
below, or pipe via `--json-file -` on commands that support it.

## Configuration (environment variables)

| Variable | Purpose | Default |
|---|---|---|
| `OPENPA_TOKEN` | JWT bearer token. Required. Obtain from your OpenPA admin or openpa-ui — the CLI does not mint tokens. The active profile is resolved server-side from the token's claims. | — |
| `OPENPA_SERVER` | Server base URL. | `http://localhost:10000` |
| `OPA_OUTPUT` | `table` (default) or `json`. | `table` |
| `OPA_NO_COLOR` | Set to disable ANSI colors and table borders. | unset |

The token is deliberately **not** a CLI flag — keeping it in an env var
prevents it from leaking into shell history or `ps` output.

## Commands

```
opa me                                          Show identity from current token
opa system-vars                                 List env vars OpenPA injects into exec_shell

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

opa conv list [--limit N] [--offset N] [--channel <type>]
opa conv new [--title "…"]
opa conv get <id> [--detail]                    --detail replays the full thinking trace in a TUI
opa conv history <id> [--limit N] [--offset N]
opa conv send <id> "message" [--raw|--json] [--no-reasoning]
opa conv attach <id>                             Watch a live run (no message sent)
opa conv rename <id> "new title"
opa conv set-id <old_id> <new_id>                Rename id (a-z, 0-9, '-', '_'; resets title to new id)
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

opa channels list                                Configured external channels (Telegram, etc.)
opa channels add --type <kind> [--mode bot|userbot]
                 [--auth-mode none|otp|password]
                 [--response-mode normal|detail]
                 [--enabled true|false]
                 [--no-pair]                     Skip auto-launching pairing flow
                 [--json '<config-object>' | --config key=value ...]
opa channels pair <id>                           Interactive pairing (QR / code / 2FA password)
opa channels delete <id>                         Cascades conversations + senders
opa channels catalog                             Dynamic TOML catalog (per-platform fields)

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

## Channels

The `opa channels` group manages external messaging platforms (Telegram,
WhatsApp, Discord, Messenger, Slack). Each profile owns at most one
channel per type, plus an implicit `main` channel that backs every web
and CLI conversation and is not user-manageable.

External channels are **inbound-only from the platform side**: a user on
Telegram messages your bot, the OpenPA agent runs, and the reply is
forwarded back through the adapter. The OpenPA side is read-only —
`opa conv send` returns `403 Read-only channel` for any conversation
whose `channel_id` resolves to a non-`main` channel. Use
`opa conv list --channel <type>` to filter the conversation list to a
specific platform, and `opa conv get <id> --detail` to replay the
agent's reasoning trace.

`opa channels add` takes platform-specific config either as a JSON blob
via `--json '{...}'` or as repeatable `--config key=value` pairs (values
treated as strings; mutually exclusive with `--json`). The expected
fields per type live in `opa channels catalog` (sourced from
`app/config/channels/*.toml`). Bot tokens, passwords, and other secrets
are redacted to `***` in `opa channels list` / `--json` responses.

On Windows PowerShell prefer `--config` — PowerShell strips inner
double quotes from native-binary arguments, so
`--json '{"phone":"+84..."}'` arrives as `--json {phone:+84...}` and
fails to parse. `--config phone=+84...` (repeat for multiple keys)
sidesteps quoting entirely.

`opa channels delete <id>` **cascades** to every conversation that lived
on that channel and every per-sender authentication row. To pause a
channel without losing it, toggle `enabled=false` from the web UI
(Settings → Channels) instead.

Two modes per platform:

- **`bot`** — a separate bot account replies (BotFather token for
  Telegram, Bot Token for Discord, Page Access Token for Messenger,
  xoxb- token for Slack).
- **`userbot`** — your own user account auto-replies on your behalf,
  similar to a forwarder. WhatsApp's `userbot` mode is the one shipping
  today (linked-device pairing via QR). Telegram/Discord/Messenger/Slack
  declare a `userbot` mode in the catalog as scaffolding but the adapter
  implementations are not yet shipped — selecting them returns HTTP 400
  with a "Mode not implemented" error.

Note that user-account automation violates the TOS of Discord and
Messenger and may get your account banned; the catalog instructions
flag this. Telegram explicitly permits userbots (Telethon / Pyrogram);
Slack permits user OAuth tokens but workspace admins can restrict them.

Adapter implementation status today:

- Telegram bot ✅
- Telegram userbot ✅ (via Telethon — interactive code + 2FA pairing)
- WhatsApp userbot ✅ (via Baileys sidecar)
- Discord (both modes), Messenger (both modes), Slack (both modes) —
  declared in the catalog, not yet implemented. Selecting them surfaces
  `Mode not implemented` from the API and a "coming soon" tag in the
  web UI's mode picker.

Telegram userbot setup: visit https://my.telegram.org/auth, create an
app to get an API ID and API Hash, then `opa channels add --type
telegram --mode userbot --json '{"api_id":"...","api_hash":"...","phone":"+..."}'`.
After save, open the web UI's Channels page (Settings → Channels) and
enter the verification code Telegram sends through the Telegram app
itself. If 2FA is enabled, you'll also be prompted for the cloud
password. Session is stored at
`<working_dir>/<profile>/telegram/<channel_id>/session.session`.

`opa channels pair <id>` runs the same pairing flow directly in the
terminal — handy when you don't have the web UI open. WhatsApp's
linked-device QR is rendered as Unicode-block characters via
`mdp/qrterminal`, scannable from your phone like any other QR; for
Telegram userbot the command prompts for the verification code (and
the cloud password if 2FA is on, typed without echo). Press Ctrl-C
to abort. The QR refreshes itself every ~20s while waiting for a
scan.

`opa channels add` auto-launches the same pairing flow when the chosen
mode declares interactive setup (WhatsApp, Telegram userbot) — so a
single `opa channels add --type whatsapp --mode userbot --json '{...}'`
both registers the channel and walks you through scanning the QR.
Pass `--no-pair` to skip; root `--json` also suppresses auto-pairing
because it implies a non-interactive caller.

WhatsApp is integrated through a Node.js sidecar
(`app/channels/sidecars/whatsapp/`) using
[`@whiskeysockets/baileys`](https://github.com/WhiskeySockets/Baileys).
Prereqs (one-time): Node 18+ on PATH, then `npm install` inside that
directory. After `opa channels add --type whatsapp ...`, scan the QR
that appears on the web UI's Channels page (Settings → Channels) with
your phone's WhatsApp → Linked Devices. The paired session is stored
under `<working_dir>/<profile>/whatsapp/<channel_id>/session/` so it
survives server restarts.
