---
description: "Reference for the `opa me` CLI command — a one-shot identity probe that decodes the current `OPA_TOKEN` JWT and prints the profile, subject, issued-at and expires-at timestamps, and the server-side and user-scoped working directories. Useful for confirming which profile a CLI session is acting as, verifying that a token has not expired, and discovering the working directory the agent will use, all without making any state-changing calls."
---

# `opa me` — Identity Info for the Current Token

`opa me` is the simplest authenticated command in the CLI. It asks the
server to decode the token in `OPA_TOKEN` and report what that token
authorizes: which profile it grants, when it expires, and which working
directory the agent operates from. Because the rest of the CLI silently
acts on the active profile resolved from this token, `opa me` is the
fastest way to confirm "who am I to the server right now?".

This command takes no arguments and has no subcommands.

## Finding this in the web UI

There is no dedicated page for the `me` payload, but the same identity
information is shown in the **header / profile badge** of the OpenPA web
UI: the active profile name appears top-right, and hovering it surfaces
the subject and expiry. If you only need to confirm the active profile,
the badge is faster; if you need machine-readable claims (timestamps,
working directories), use `opa me --json`.

## Global flags

`opa me` accepts the root-level `--json` flag to emit the raw token
payload as JSON instead of a human-readable key/value table:

```bash
opa me --json
```

It also obeys the standard CLI environment variables — most importantly
`OPA_TOKEN` (required) and `OPA_SERVER` (default
`http://localhost:10000`).

## Behavior

`opa me` calls the server's identity endpoint with the bearer token and
prints back the decoded claims. It does **not** mutate any state and is
safe to run as often as needed.

In the default (table) view the output is a fixed two-column key/value
list with these rows:

| Row                | Meaning                                                                                       |
|--------------------|-----------------------------------------------------------------------------------------------|
| `profile`          | Profile name the token grants. All other CLI commands act on this profile.                    |
| `subject`          | JWT `sub` claim — the principal id (typically the same as the profile, but server-controlled).|
| `issued_at`        | RFC 3339 timestamp + Unix seconds, e.g. `2026-05-02T14:00:00Z (1746201600)`.                  |
| `expires_at`       | RFC 3339 timestamp + Unix seconds. After this moment, every authenticated command will fail.  |
| `working_dir`      | Server-side working directory used by built-in tools and the agent's filesystem operations.   |
| `user_working_dir` | The profile's preferred user working directory (mirrors the value set during setup).          |

With `--json`, the output is the full JSON object emitted by the
identity endpoint, suitable for piping into `jq`.

## Examples

### Confirm the active profile

```bash
$ opa me
profile           admin
subject           admin
issued_at         2026-05-02T14:00:00Z (1746201600)
expires_at        2026-06-01T14:00:00Z (1748793600)
working_dir       /var/lib/openpa
user_working_dir  /home/li/work
```

### Read just the profile name in a script

```bash
$ opa me --json | jq -r .profile
admin
```

### Confirm the token is still valid

```bash
$ opa me --json | jq -r '.expires_at | todate'
2026-06-01T14:00:00Z
```

## Troubleshooting

**`OPA_TOKEN is required`** — The CLI cannot mint tokens. Either export
a token you already have (`export OPA_TOKEN=<jwt>`), or run
`opa setup complete` to mint one for the first profile.

**`401 Unauthorized` / `token expired`** — The token has passed its
`expires_at` timestamp. Obtain a fresh token (re-run setup, or ask your
admin) and re-export it.

**Wrong profile shown** — Profiles are baked into the token. If `opa me`
reports a profile you did not expect, you are using the wrong token.
List available profiles with `opa profile list` (under a token that has
permission), then export the token for the profile you want.
