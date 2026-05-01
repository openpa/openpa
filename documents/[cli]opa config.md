---
description: "Complete reference for the `opa config` CLI command — how to inspect, set, and reset per-profile OpenPA agent settings using the four subcommands `opa config schema`, `opa config get`, `opa config set`, and `opa config reset`, including the full list of configurable keys (agent, history, skill_classifier, summarizer) with their types, defaults, and allowed ranges."
---

# `opa config` — Per-Profile Settings Reference

`opa config` is the CLI for inspecting and changing per-profile settings
that control OpenPA's reasoning agent loop, conversation-history budgets,
skill classifier, and trace summarizer.

Every setting has a built-in **default**. When you change a setting with
`opa config set`, your value becomes an **override** that takes priority
over the default for your profile only; the override persists across
restarts. `opa config reset` removes the override and the setting goes
back to its default.

## Global flags

All `opa config` subcommands accept the root-level `--json` flag, which
forces JSON output instead of the default human-readable table:

```bash
opa config get --json
```

## Subcommands

`opa config` has four subcommands. Each is documented below with its
purpose, syntax, and worked examples.

### `opa config schema`

**Purpose.** Print every configurable group and key, with each key's
type, default value, and allowed range. This is the answer to "what can
I configure?".

**Syntax.**

```bash
opa config schema [--json]
```

**Behavior.** In the default (table) view, output is grouped by config
group, with the group's label and description, followed by one line per
key showing its dotted name, type, and default. With `--json`, the
schema is emitted as a machine-readable JSON document that also
includes each key's `min`, `max`, `step`, `label`, and `description`.

**Example (default output, abbreviated).**

```bash
$ opa config schema
[agent] Reasoning Agent
  Controls the ReAct loop's iteration limits and per-call LLM parameters.
  agent.max_llm_retries  type=number default=2
  agent.max_steps  type=number default=40
  agent.reasoning_max_tokens  type=number default=32768
  agent.reasoning_retry  type=number default=3
  agent.reasoning_temperature  type=number default=1
  ...
```

### `opa config get`

**Purpose.** Show the *current* value of one or all settings for the
active profile, alongside the declared default so you can see at a
glance which keys you have overridden.

**Syntax.**

```bash
opa config get [<group.key>]
```

**Arguments.**

- `<group.key>` *(optional)* — A dotted key path such as
  `agent.max_steps`. If omitted, every key is displayed.

**Behavior.** Without an argument, the CLI renders a three-column
table sorted by key, with an empty `VALUE` column whenever no override
exists. With a single argument, the CLI prints just the resolved value —
the override if one is set, otherwise the default.

**Examples.**

```bash
# All keys with their override and default values
$ opa config get
KEY                              VALUE   DEFAULT
agent.max_llm_retries                    2
agent.max_steps                  80      40
agent.reasoning_max_tokens               32768
...

# A single key
$ opa config get agent.max_steps
80

# JSON output (full structure including both maps)
$ opa config get --json
{"values":{"agent.max_steps":80},"defaults":{"agent.max_steps":40, ...}}
```

### `opa config set`

**Purpose.** Override a single config key for the active profile. The
override persists across restarts until you change it again or reset it.

**Syntax.**

```bash
opa config set <group.key> <value>
```

**Arguments** (both required):

- `<group.key>` — A dotted key path such as `agent.max_steps`.
- `<value>` — The new value as a string. The CLI converts the string
  to the appropriate type before submitting it, and the value is then
  validated against the key's declared type and allowed range.

**Type coercion.** The string-to-type rules are:

- `"true"` or `"false"` (case-insensitive) → boolean
- An integer literal (e.g. `40`, `-3`) → integer
- A decimal or exponential literal (e.g. `0.5`, `1e-3`) → float
- Anything else → string

If the coerced value violates the schema (wrong type, out of range,
unknown key) the override is **not** applied and an error is reported.

**Examples.**

```bash
# Integer
opa config set agent.max_steps 80

# Float
opa config set agent.reasoning_temperature 0.5

# Boolean (none of the current keys are boolean, but the coercion works)
opa config set some.flag true
```

### `opa config reset`

**Purpose.** Drop a single override and revert that key to its declared
default.

**Syntax.**

```bash
opa config reset <group.key>
```

**Arguments** (required):

- `<group.key>` — A dotted key path such as `agent.max_steps`.

**Behavior.** Removes the override for the given key on the active
profile. The next read returns the key's declared default.

**Example.**

```bash
opa config reset agent.max_steps
```

## Available config keys

The defaults shown below are the values that apply when no override
has been set. Run `opa config schema --json` to confirm the live
values for your installation.

### Group `agent` — Reasoning Agent

Controls the ReAct loop's iteration limits and per-call LLM parameters.

| Key                            | Type   | Default | Range          | Meaning                                                                             |
|--------------------------------|--------|---------|----------------|-------------------------------------------------------------------------------------|
| `agent.max_steps`              | number | `40`    | 1 – 200        | Maximum ReAct iterations before the agent stops a turn.                             |
| `agent.max_llm_retries`        | number | `2`     | 0 – 10         | How many times the loop retries after an LLM error before giving up.                |
| `agent.reasoning_temperature`  | number | `1.0`   | 0 – 2 (±0.1)   | Sampling temperature for the main reasoning LLM call.                               |
| `agent.reasoning_max_tokens`   | number | `32768` | 256 – 131072   | Output token cap for the reasoning LLM call.                                        |
| `agent.reasoning_retry`        | number | `3`     | 0 – 10         | How many times an individual reasoning LLM call retries on transient errors.        |
| `agent.steps_length`           | number | `80`    | 5 – 500        | Maximum number of recent ReAct step entries kept in the prompt context. Older entries are dropped once this is exceeded. |

### Group `history` — Conversation History

Token-budget limits applied when assembling the message window for each
LLM call.

| Key                              | Type   | Default | Range          | Meaning                                                                |
|----------------------------------|--------|---------|----------------|------------------------------------------------------------------------|
| `history.max_tokens_total`       | number | `5000`  | 500 – 200000   | Maximum total tokens of past messages included in each prompt.         |
| `history.max_tokens_per_message` | number | `500`   | 50 – 20000     | Each message is truncated to at most this many tokens before assembly. |

### Group `skill_classifier` — Skill Classifier

Lightweight LLM that decides whether a request maps to a registered
skill.

| Key                          | Type   | Default | Range          | Meaning                                                              |
|------------------------------|--------|---------|----------------|----------------------------------------------------------------------|
| `skill_classifier.temperature` | number | `0.0`   | 0 – 2 (±0.1)   | Sampling temperature; keep low for deterministic classification.   |
| `skill_classifier.max_tokens`  | number | `64`    | 8 – 2048       | Output token cap for the classifier call.                          |
| `skill_classifier.retry`       | number | `2`     | 0 – 10         | Retries on transient classifier LLM errors.                        |

### Group `summarizer` — Trace Summarizer

LLM that compresses long reasoning traces back into the conversation
history.

| Key                       | Type   | Default | Range          | Meaning                                                  |
|---------------------------|--------|---------|----------------|----------------------------------------------------------|
| `summarizer.temperature`  | number | `0.3`   | 0 – 2 (±0.1)   | Sampling temperature for the summarization call.         |
| `summarizer.max_tokens`   | number | `1024`  | 128 – 8192     | Output token cap for the summary.                        |
| `summarizer.retry`        | number | `2`     | 0 – 10         | Retries on transient summarizer LLM errors.              |

## Worked examples

### Inspect everything

```bash
$ opa config get
```

### Raise the ReAct step ceiling for long tasks

```bash
$ opa config set agent.max_steps 80
$ opa config get agent.max_steps
80
```

### Undo the override

```bash
$ opa config reset agent.max_steps
$ opa config get agent.max_steps
40
```

### Tighten the history budget for a short-context model

```bash
$ opa config set history.max_tokens_total 2000
$ opa config set history.max_tokens_per_message 250
```

### Pipe the schema into `jq`

```bash
$ opa config schema --json | jq '.groups.agent.fields | keys'
[
  "max_llm_retries",
  "max_steps",
  "reasoning_max_tokens",
  "reasoning_retry",
  "reasoning_temperature",
  "steps_length"
]
```

## Troubleshooting

**`opa config set` is rejected as a bad request** — The value failed
validation. Common causes:

- The value is out of the key's declared `min`/`max` range (see the
  keys table above).
- The dotted key is misspelled or refers to a non-existent group/key.
- The coerced type does not match the key's declared type (e.g.
  setting a `number` key to a non-numeric string).

Run `opa config schema` to confirm the exact key name and allowed
range, then retry.

**Override "doesn't seem to apply"** — Overrides are stored per profile.
An override set under one profile is invisible to another; if you have
switched profiles, you are reading a different set of overrides.
