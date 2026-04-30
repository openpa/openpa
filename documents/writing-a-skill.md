---
description: "Step-by-step guide for writing a new OpenPA skill, including the SKILL.md frontmatter format, the skill directory layout, and how to register the skill so the Reasoning Agent can discover it."
---

# Writing a Skill for OpenPA

A *skill* in OpenPA is a folder under `<OPENPA_WORKING_DIR>/<profile>/skills/`
that contains a `SKILL.md` file. Skills inject context into the Reasoning
Agent — they are not callable tools.

## 1. Create the skill directory

```
<OPENPA_WORKING_DIR>/<profile>/skills/my-first-skill/
```

The folder name becomes the skill's stable identifier.

## 2. Write `SKILL.md`

```markdown
---
name: my-first-skill
description: "What this skill is for, in one sentence."
---

# My First Skill

Write the body of the skill here. The agent will see this content as
context when the skill is selected. Keep it focused and concrete —
short examples beat long prose.
```

The frontmatter `name` and `description` are required. Optional keys:

- `environment_variables`: a list of names that should be exposed via
  `scripts/.env`.
- `metadata.long_running_app.command`: a command to register for autostart.

## 3. Add scripts (optional)

If your skill needs to run code, drop scripts under
`<skill-dir>/scripts/`. Reference them from the body of `SKILL.md` so the
agent knows when to call them via the `exec_shell` built-in tool.

## 4. Verify

Restart OpenPA (or wait for the file watcher's debounce, ~1 second) and
open Settings → Tools & Skills → Skills. Your skill should appear,
toggleable, with the description from the frontmatter.

If it doesn't appear, the most common causes are:

1. The frontmatter is malformed — confirm `---` delimiters and a
   non-empty `description`.
2. The skill is in the wrong directory — it must be directly under the
   profile's `skills/` folder, not nested.
3. The watcher hasn't picked up the change yet — give it a second or
   restart the server.
