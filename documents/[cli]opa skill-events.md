---
description: "Complete reference for the `opa skill-events` CLI command (alias `opa events`) — the terminal-side counterpart to the **Skill Events** page in the OpenPA web UI — covering how to list and delete skill event subscriptions for the active profile, simulate an event by dropping a markdown file into the watched folder (`simulate`), tail server-wide events (`stream`) and per-profile notifications (`notifications`) over SSE, browse the events declared by a skill (`events <skill>`), check whether a skill's listener daemon is alive (`listener-status`), and start/resume that daemon as an autostart process (`listener-start`). Documents the streaming output format and the `--since` resume cursor."
---

# `opa skill-events` — Skill Event Subscriptions and Notifications

`opa skill-events` (alias `opa events`) is the CLI for managing the
event-driven side of OpenPA skills. A *skill* (declared by a
`SKILL.md`) can declare *events* it watches for; when an event fires,
it can spawn a conversation, run a script, or notify the user. This
group lets you inspect those subscriptions, tail live events, and
control the listener daemons that emit them.

The group covers four orthogonal concerns:

- **Subscriptions** — `list`, `delete`. Each subscription binds an
  event type to a skill (and optionally to a conversation).
- **Live streaming** — `stream` (admin-wide snapshot) and
  `notifications` (per-profile notifications) emit Server-Sent Events
  until interrupted with Ctrl-C.
- **Skill metadata** — `events <skill>` lists what events a skill
  declares.
- **Listener daemons** — `listener-status <skill>`,
  `listener-start <skill>`. The listener daemon watches the
  filesystem (or other source) and posts events back to the server.
- **Manual testing** — `simulate <id>` writes a markdown file into a
  subscription's watched folder so you can fire an event without
  whatever real-world trigger normally produces it.

## Finding this in the web UI

Every operation in this group has a control on the **Skill Events**
page of the OpenPA web UI:

> **Sidebar → Skill Events**

The page lists subscriptions in the left rail (matching `opa skill-events list`),
shows recent notifications in the main area (matching
`opa skill-events notifications`), and exposes a **Simulate** button on
each subscription row that opens a small editor for the markdown body
(matching `opa skill-events simulate`).

## Streaming output format

`opa skill-events stream` and `opa skill-events notifications` both
maintain a long-lived SSE connection. They print one line per event
in one of two formats:

- **Default (table mode):** `[<event_type>] <raw JSON payload>`.
- **`--json` mode:** the raw JSON payload only (one event per line).
  This is what you want for piping into `jq`.

Press Ctrl-C to exit cleanly; the server connection is dropped.

## Global flags

All `opa skill-events` subcommands accept the root-level `--json`
flag. `OPA_TOKEN` is required for every subcommand.

## Subcommands

### `opa skill-events list`

**Purpose.** List skill event subscriptions belonging to the active
profile.

**Syntax.**

```bash
opa skill-events list
```

**Behavior.** Renders a five-column table:

| Column         | Source                | Meaning                                                |
|----------------|-----------------------|--------------------------------------------------------|
| `ID`           | `id`                  | Subscription id (used by `delete` and `simulate`).     |
| `SKILL`        | `skill_name`          | The skill that owns the subscription.                  |
| `EVENT_TYPE`   | `event_type`          | The skill-declared event type the subscription matches.|
| `CONVERSATION` | `conversation_id`     | Conversation the subscription routes events into. Blank if none. |
| `CONV_TITLE`   | `conversation_title`  | Title of that conversation, for readability.           |

With `--json`, returns the underlying array.

**Example.**

```bash
$ opa skill-events list
ID         SKILL          EVENT_TYPE  CONVERSATION  CONV_TITLE
sub_19a8   daily-brief    morning     c_82bc        Daily Brief
sub_4f02   review-pr      pr-opened
```

### `opa skill-events delete`

**Purpose.** Remove a subscription. Future events of that type for
that skill stop being routed.

**Syntax.**

```bash
opa skill-events delete <id>
```

**Behavior.** Silent on success.

**Example.**

```bash
$ opa skill-events delete sub_19a8
```

### `opa skill-events simulate`

**Purpose.** Drop a markdown file under the subscription's watched
events folder, simulating a real event firing. Useful for development
and dry-running event handlers.

**Syntax.**

```bash
opa skill-events simulate <id> [--filename <name>]      # body read from stdin
```

**Arguments** (required):

- `<id>` — Subscription to simulate against.

**Flags.**

| Flag         | Type   | Default | Meaning                                                                |
|--------------|--------|---------|------------------------------------------------------------------------|
| `--filename` | string | `""`    | Name of the file dropped in the folder. If omitted, a unique `simulate-*.md` name is chosen. |

**Behavior.** Reads the markdown body from **stdin** until EOF and
posts it to the server, which writes it under the subscription's
watched folder. The skill's listener daemon then picks it up and
routes it through the normal event pipeline. Silent on success.

**Examples.**

```bash
# From a file
$ opa skill-events simulate sub_19a8 --filename morning-brief.md < morning.md

# From a heredoc
$ opa skill-events simulate sub_4f02 <<'EOF'
# PR opened
- Author: li
- Repo:  openpa
- URL:   https://github.com/.../pull/42
EOF
```

### `opa skill-events stream`

**Purpose.** Tail the server-wide skill-events admin snapshot stream.
The stream emits one event per change to *any* subscription, listener
status, or routed event.

**Syntax.**

```bash
opa skill-events stream
```

**Behavior.** Long-lived SSE connection. See [Streaming output
format](#streaming-output-format) for the per-line format. Ctrl-C to
exit.

**Example.**

```bash
$ opa skill-events stream
[subscription_added] {"id":"sub_19a8","skill_name":"daily-brief",...}
[event_routed]       {"subscription_id":"sub_19a8","conversation_id":"c_82bc",...}
```

### `opa skill-events notifications`

**Purpose.** Tail just the active profile's per-skill notifications —
the same notification badges that appear on the web UI's sidebar.

**Syntax.**

```bash
opa skill-events notifications [--since <millis>]
```

**Flags.**

| Flag       | Type  | Default | Meaning                                                  |
|------------|-------|---------|----------------------------------------------------------|
| `--since`  | int64 | `0`     | Resume cursor (Unix milliseconds). Replays everything from this timestamp forward, then continues live. |

**Behavior.** Long-lived SSE connection. With `--since`, the server
backfills any notifications recorded at or after the given timestamp
before going live, so you can resume after a disconnect without
losing events.

**Example.**

```bash
$ opa skill-events notifications --since 1746201600000
[notification] {"skill":"daily-brief","summary":"Daily brief ready","ts":1746205200000}
```

### `opa skill-events events`

**Purpose.** List the events a particular skill declares (read from
its `SKILL.md`). This is what the **Subscribe** dialog in the UI
populates from.

**Syntax.**

```bash
opa skill-events events <skill>
```

**Arguments** (required):

- `<skill>` — Skill name (matches `skill_name` from `list`).

**Behavior.** Pretty-prints the JSON document the server returns,
typically an array of `{type, description}` objects. With `--json`,
the same JSON is emitted unindented.

**Example.**

```bash
$ opa skill-events events daily-brief
{
  "skill_name": "daily-brief",
  "events": [
    {"type": "morning", "description": "Fired every weekday at 09:00"},
    {"type": "evening", "description": "Fired every weekday at 18:00"}
  ]
}
```

### `opa skill-events listener-status`

**Purpose.** Check whether a skill's listener daemon is alive, by
asking the server for the daemon's most recent heartbeat.

**Syntax.**

```bash
opa skill-events listener-status <skill>
```

**Behavior.** Prints a key-value table:

| Row              | Meaning                                                          |
|------------------|------------------------------------------------------------------|
| `skill_name`     | The skill being checked.                                         |
| `running`        | `yes` if a heartbeat was received recently, `no` otherwise.      |
| `last_heartbeat` | Server's view of the most recent heartbeat (timestamp or `null`).|
| `autostart_id`   | The autostart-process row backing this listener, if any.         |
| `command`        | The exact command line the listener runs as.                     |

With `--json`, returns the underlying object.

**Example.**

```bash
$ opa skill-events listener-status daily-brief
skill_name      daily-brief
running         yes
last_heartbeat  2026-05-02T14:00:00Z
autostart_id    a_8c14
command         /usr/bin/python /skills/daily-brief/listen.py
```

### `opa skill-events listener-start`

**Purpose.** Start (or resume) a skill's listener daemon as an
autostart process — so it relaunches at server boot.

**Syntax.**

```bash
opa skill-events listener-start <skill>
```

**Behavior.** Spawns the daemon if it is not already running and
records an autostart row. Idempotent: a second call against an
already-running listener returns the existing process and autostart
ids without duplicating.

Prints a key-value table:

| Row             | Meaning                                                    |
|-----------------|------------------------------------------------------------|
| `process_id`    | Live process id (use with `opa proc attach`).              |
| `autostart_id`  | Autostart registration id (use with `opa proc autostart delete` to undo). |

**Example.**

```bash
$ opa skill-events listener-start daily-brief
process_id    p_9d72
autostart_id  a_8c14
```

## Worked examples

### Inspect what a skill exposes and subscribe

```bash
# What events does the skill declare?
$ opa skill-events events daily-brief

# Subscribe via the UI (the CLI doesn't have a `subscribe` subcommand —
# subscriptions are created when a skill is bound to a conversation in
# the web UI).

# Confirm the subscription appears
$ opa skill-events list
```

### Tail notifications and pretty-print as they arrive

```bash
$ opa skill-events notifications --json | jq '.summary'
"Daily brief ready"
"PR opened: openpa#42"
```

### Resume notifications after a disconnect

```bash
# Note the timestamp before disconnecting (or pull it from the last
# event you saw)
$ since=$(date -d '5 minutes ago' +%s%3N)
$ opa skill-events notifications --since "$since"
```

### Manually fire a "morning" event for development

```bash
$ opa skill-events simulate sub_19a8 <<'EOF'
# Morning brief
- Top issue: openpa#42
- Calendar:  09:30 standup
EOF
```

### Bring a stale listener back to life

```bash
$ opa skill-events listener-status daily-brief
skill_name      daily-brief
running         no
...
$ opa skill-events listener-start daily-brief
process_id    p_9d72
autostart_id  a_8c14
$ opa skill-events listener-status daily-brief
running         yes
```

## Troubleshooting

**`stream` / `notifications` exit immediately** — Almost always an
auth issue. Confirm `opa me` works first.

**`simulate` is silent and nothing happens** — The listener for that
skill must be running for the dropped file to be picked up. Check
`opa skill-events listener-status <skill>` and start the listener if
needed.

**No notifications even though the skill should have fired** — Three
common causes: (1) the listener is down (`listener-status`), (2) the
subscription is bound to a conversation that has been deleted (the
event is dropped), (3) the `event_type` declared by the skill changed
and the existing subscription no longer matches — re-create it from
the UI.

**`--since` returns nothing** — Notifications are not retained
indefinitely. If `--since` reaches further back than the server's
retention window, no backfill is produced (the live tail still works).

**Listener won't start** — `listener-start` defers to the autostart
process machinery. Use `opa proc autostart list` to see whether a
duplicate or a stale entry is blocking the new one. If yes, delete the
stale row with `opa proc autostart delete <id>` first.
