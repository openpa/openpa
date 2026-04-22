from app.tools.builtin.exec_shell_input_mode import (
    TerminalState,
    detect_input_mode,
    update_terminal_state,
)


def _classify(chunks, is_pty=True):
    state = TerminalState()
    for chunk in chunks:
        update_terminal_state(state, chunk)
    tail = "".join(chunks)
    return detect_input_mode(state, tail, is_pty=is_pty)


# Openclaw / @clack/prompts setup prompt — the case reported in-the-wild that
# was scoring `cursor_hidden` only.  Arrow keys switch rows, so the expected
# classification is `selection`.
CLACK_OPENCLAW_STDOUT = (
    "\x1b[999D\x1b[5A\x1b[1B\x1b[J"
    "\x1b[32m\u25c7\x1b[39m  \x1b[91mI understand this is personal-by-default.\x1b[39m\n"
    "\x1b[90m\u2502\x1b[39m  \x1b[2mYes\x1b[22m\n"
    "\x1b[?25l"
    "\x1b[90m\u2502\x1b[39m\n"
    "\x1b[36m\u25c6\x1b[39m  \x1b[91mSetup mode\x1b[39m\n"
    "\x1b[36m\u2502\x1b[39m  \x1b[32m\u25cf\x1b[39m QuickStart "
    "\x1b[2m(\x1b[31mConfigure details later.\x1b[39m)\x1b[22m\n"
    "\x1b[36m\u2502\x1b[39m  \x1b[2m\u25cb\x1b[22m \x1b[2mManual\x1b[22m\n"
    "\x1b[36m\u2514\x1b[39m\n"
)


def test_clack_single_select_is_selection():
    result = _classify([CLACK_OPENCLAW_STDOUT])
    assert result["input_mode"] == "selection", result
    assert "pointer_glyph" in result["signals"]
    assert "clack_prompt_header" in result["signals"]
    assert "redraw_loop" in result["signals"]
    assert "cursor_hidden" in result["signals"]


# @clack/prompts multi-select uses ◻ / ◼ markers behind the box gutter.
CLACK_MULTISELECT_STDOUT = (
    "\x1b[3A\x1b[J"
    "\x1b[?25l"
    "\x1b[36m\u25c6\x1b[39m  Pick features\n"
    "\x1b[36m\u2502\x1b[39m  \u25fb Option A\n"
    "\x1b[36m\u2502\x1b[39m  \u25fc Option B\n"
    "\x1b[36m\u2502\x1b[39m  \u25fb Option C\n"
    "\x1b[36m\u2514\x1b[39m\n"
)


def test_clack_multiselect_is_selection():
    result = _classify([CLACK_MULTISELECT_STDOUT])
    assert result["input_mode"] == "selection", result
    assert "pointer_glyph" in result["signals"]
    assert "clack_prompt_header" in result["signals"]


# Plain readline-style text prompt — cursor visible, tail ends with ": ".
# Must not be misclassified as selection after the gutter/glyph loosening.
TEXT_PROMPT_STDOUT = "\x1b[?25hProject name: "


def test_text_prompt_is_text():
    result = _classify([TEXT_PROMPT_STDOUT])
    assert result["input_mode"] == "text", result
    assert "prompt_tail" in result["signals"]
