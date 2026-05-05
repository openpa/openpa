---
description: "Complete reference for the `opa setup` CLI command — the headless equivalent of the OpenPA first-run wizard — covering how to check setup status, mint the very first admin JWT (`opa setup complete`), recover from an orphaned half-setup (`reset-orphaned`), trigger a re-run of the wizard (`reconfigure`), and read or write server-wide non-secret config (`server-config get/set`). Spells out which subcommands need an `OPENPA_TOKEN` and which are deliberately unauthenticated, documents the JSON payload schema accepted by `setup complete`, and gives admin-friendly examples for scripted bootstrap and reconfiguration."
---

# `opa setup` — First-Run Setup and Server Configuration

`opa setup` is the CLI entry point for bootstrapping a fresh OpenPA
server, recovering a half-finished setup, and editing server-wide
configuration after the fact. It exposes the same actions that the
**Setup Wizard** in the web UI walks an admin through, but in a form
that can be scripted into infrastructure automation.

The group has two halves with different auth requirements:

- **Unauthenticated bootstrap** — `status`, `complete`, `reset-orphaned`.
  These do **not** require `OPENPA_TOKEN` because they are how you obtain
  one in the first place.
- **Admin-authenticated maintenance** — `reconfigure`,
  `server-config get`, `server-config set`. These need a valid
  `OPENPA_TOKEN` from an admin profile.

A typical first-run flow is: `opa setup status` → `opa setup complete`
(which prints a JWT) → `export OPENPA_TOKEN=...` → use the rest of the CLI.

## Finding this in the web UI

The unauthenticated subcommands (`status`, `complete`,
`reset-orphaned`) correspond to the **Setup Wizard** that the web UI
shows on first load when no profiles exist:

> **Web UI → Setup Wizard** (auto-displayed before login)

The admin-authenticated subcommands map to the admin settings page:

> **Sidebar → Settings → Admin → Server config**

That page shows non-secret server settings as a key/value editor, and
exposes a "Reset setup" action that mirrors `opa setup reconfigure`.

## Global flags and auth

All `opa setup` subcommands respect the root-level `--json` flag.

Unlike most CLI commands, `status`, `complete`, and `reset-orphaned`
are deliberately **unauthenticated** — they ignore `OPENPA_TOKEN`. The
remaining subcommands (`reconfigure`, `server-config *`) **require**
`OPENPA_TOKEN` to belong to an admin-capable profile.

`OPENPA_SERVER` (default `http://localhost:10000`) controls which server
the CLI talks to.

## Subcommands

### `opa setup status`

**Purpose.** Check whether the server has finished its first-run setup,
and optionally whether a particular profile already exists.

**Syntax.**

```bash
opa setup status [--profile <name>]
```

**Flags.**

| Flag        | Type   | Default | Meaning                                                |
|-------------|--------|---------|--------------------------------------------------------|
| `--profile` | string | `""`    | Also check whether this specific profile is registered.|

**Behavior.** Calls the unauthenticated status endpoint. Prints a
key-value table:

| Row              | Meaning                                                                     |
|------------------|-----------------------------------------------------------------------------|
| `setup_complete` | Whether the server has been bootstrapped at least once.                     |
| `profile_exists` | (Only when `--profile` was passed.) Whether that named profile exists.      |
| `has_profiles`   | (Only when present in the response.) Whether *any* profile exists.          |

With `--json`, returns the raw object exactly as the server emitted it.

**Example.**

```bash
$ opa setup status --profile admin
setup_complete  true
profile_exists  true
```

### `opa setup complete`

**Purpose.** POST a setup payload — the JSON the wizard would have
collected — and receive a freshly minted admin JWT. This is the **only
unauthenticated way to mint a token**; every other path requires an
existing token.

**Syntax.**

```bash
opa setup complete [--profile <name>] (--json '<inline>' | --json-file <path|->)
```

**Flags.**

| Flag           | Type   | Default | Meaning                                                                      |
|----------------|--------|---------|------------------------------------------------------------------------------|
| `--profile`    | string | `""`    | Overrides the `profile` field inside the JSON payload.                       |
| `--json`       | string | `""`    | Setup payload as an inline JSON object.                                      |
| `--json-file`  | string | `""`    | Path to a JSON file containing the payload. Use `-` to read stdin.           |

If neither `--json` nor `--json-file` is given, the payload is read
from **stdin**. `--json` and `--json-file` are mutually exclusive.

**Payload schema.** The body matches the openpa-ui wizard one-for-one:

```json
{
  "profile": "admin",
  "server_config": {
    "jwt_secret": "...",
    "user_working_dir": "..."
  },
  "llm_config": {
    "anthropic.api_key": "sk-...",
    "auth_method": "anthropic"
  },
  "tool_configs": {
    "<tool_id>": { "_enabled": "true", "VAR_NAME": "value" }
  },
  "agent_configs": {
    "<tool_id>": {
      "llm_provider": "anthropic",
      "llm_model": "claude-..."
    }
  }
}
```

The first profile must be named `admin`. A `profile` field is required
either inside the JSON or via `--profile`.

**Behavior.** On success, prints a key-value table to stdout containing
`profile`, `expires_at`, and `token`, then writes the recommended
`export OPENPA_TOKEN=...` line to **stderr** so users can copy/paste it.
With `--json`, the full response object is emitted to stdout instead.

**Examples.**

```bash
# Minimal: just the profile
$ echo '{"profile":"admin"}' | opa setup complete
profile     admin
expires_at  2026-06-01T14:00:00Z
token       eyJhbGciOi...

Export the token to use the CLI:
  export OPENPA_TOKEN=eyJhbGciOi...

# From a file, with a profile override
$ opa setup complete --profile admin --json-file ./bootstrap.json

# Capture the token directly
$ export OPENPA_TOKEN=$(opa setup complete --json-file ./bootstrap.json --json | jq -r .token)
```

### `opa setup reset-orphaned`

**Purpose.** Recover from a partially-completed setup where
`setup_complete` is true but no profiles exist (for example, after
manually wiping the profile table). Clears the flag so `setup complete`
can run again.

**Syntax.**

```bash
opa setup reset-orphaned
```

**Behavior.** Unauthenticated. Silent on success. The server itself
verifies that no profiles exist — the call is rejected if the database
is in any state other than "orphaned".

**Example.**

```bash
$ opa setup reset-orphaned
$ opa setup status
setup_complete  false
```

### `opa setup reconfigure`

**Purpose.** Reset `setup_complete` from a healthy server so the wizard
runs again on next boot. Used to onboard the server through a fresh
admin payload, e.g. after rotating the JWT secret.

**Syntax.**

```bash
opa setup reconfigure
```

**Behavior.** **Requires admin auth.** Silent on success. The next
startup of openpa-ui will display the Setup Wizard, and `opa setup
status` will report `setup_complete=false` until a new `setup complete`
call lands.

**Example.**

```bash
$ opa setup reconfigure
$ opa setup status
setup_complete  false
```

### `opa setup server-config get`

**Purpose.** Read non-secret server-wide settings — the same key/value
pairs accepted by `server_config` in the setup payload.

**Syntax.**

```bash
opa setup server-config get [<key>]
opa setup server server-config get [<key>]   # alias
```

**Arguments** (optional):

- `<key>` — When given, prints just that key's value. When omitted,
  prints every key as a key/value table.

**Behavior.** **Requires admin auth.** With `--json`, output is the
full JSON object (or `{<key>: <value>}` for the single-key form).

**Examples.**

```bash
# Everything
$ opa setup server-config get
user_working_dir   /home/li/work
jwt_issuer         openpa
log_level          info

# A single key (handy for scripts)
$ opa setup server-config get user_working_dir
/home/li/work
```

### `opa setup server-config set`

**Purpose.** Write one or more server-wide config keys.

**Syntax.**

```bash
opa setup server-config set KEY=VALUE [KEY=VALUE...]
```

**Arguments** (at least one required):

- `KEY=VALUE` — Repeatable. Each pair is split on the first `=`. The
  value side may contain further `=` characters.

**Behavior.** **Requires admin auth.** All updates are sent in a single
PATCH; if any one is rejected, none are applied. Silent on success.

**Examples.**

```bash
$ opa setup server-config set log_level=debug user_working_dir=/srv/openpa
$ opa setup server-config get log_level
debug
```

## Worked examples

### Headless first-run bootstrap

```bash
# 1. Confirm the server is fresh
$ opa setup status
setup_complete  false

# 2. POST the wizard payload, capture the token
$ cat > bootstrap.json <<'EOF'
{
  "profile": "admin",
  "server_config": { "user_working_dir": "/srv/openpa" },
  "llm_config":    { "anthropic.api_key": "sk-...", "auth_method": "anthropic" }
}
EOF
$ export OPENPA_TOKEN=$(opa setup complete --json-file bootstrap.json --json | jq -r .token)

# 3. Verify identity is now usable
$ opa me
profile  admin
...
```

### Re-run the wizard from an existing admin

```bash
$ opa setup reconfigure        # invalidates setup_complete
$ unset OPENPA_TOKEN              # no longer needed
$ opa setup complete --json-file bootstrap.json
```

### Rotate the configured working directory

```bash
$ opa setup server-config set user_working_dir=/mnt/openpa
$ opa setup server-config get user_working_dir
/mnt/openpa
```

## Troubleshooting

**`a 'profile' field is required`** — `setup complete` saw neither a
`profile` key in the JSON nor a `--profile` flag. Add one or the
other.

**`--json and --json-file are mutually exclusive`** — Pick one source
for the payload. To read from stdin, omit both flags or pass
`--json-file -`.

**`401 Unauthorized` on `reconfigure` / `server-config`** — These
subcommands require admin auth. Make sure `OPENPA_TOKEN` belongs to an
admin profile (`opa me` to confirm).

**`setup complete` reports `setup already complete`** — The server has
been bootstrapped before. Either use `opa profile create` for new
profiles (with an existing admin token), or run `opa setup reconfigure`
first to allow the wizard to run again.

**`reset-orphaned` rejected** — The recovery path only fires when the
database is genuinely orphaned (`setup_complete=true` but no profiles).
Run `opa setup status` first to confirm.

**Token printed to stdout but not exported** — `setup complete` cannot
mutate your shell environment. Either copy the printed export line
(emitted to stderr), or capture the token in a subshell:
`export OPENPA_TOKEN=$(opa setup complete --json-file b.json --json | jq -r .token)`.
