"""Tests for the `keys` array parameter on ExecShellInputTool."""

import asyncio
import time

import pytest

from app.tools.builtin.exec_shell import (
    ExecShellInputTool,
    ProcessInfo,
    _KEY_NAME_TO_BYTES,
    _process_registry,
)


class _FakeStdin:
    def __init__(self):
        self.buffer = bytearray()
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)
        self.writes.append(bytes(data))

    async def drain(self) -> None:
        return None


class _FakeProcess:
    def __init__(self):
        self.stdin = _FakeStdin()
        self.returncode = None


@pytest.fixture
def registered_process():
    process_id = f"test-{id(object())}"
    proc = _FakeProcess()
    _process_registry[process_id] = ProcessInfo(
        process=proc,
        created_at=time.time(),
        working_dir=".",
        command="fake",
        is_long_running=True,
    )
    try:
        yield process_id, proc
    finally:
        _process_registry.pop(process_id, None)


def _run(coro):
    return asyncio.run(coro)


def test_keys_array_expands_to_concatenated_escape_codes(registered_process):
    process_id, proc = registered_process
    tool = ExecShellInputTool()

    result = _run(tool.run({
        "process_id": process_id,
        "keys": ["down", "down", "down", "enter"],
    }))

    assert result.structured_content.get("input_sent") is True
    expected = (
        _KEY_NAME_TO_BYTES["down"] * 3 + _KEY_NAME_TO_BYTES["enter"]
    ).encode("utf-8")
    assert bytes(proc.stdin.buffer) == expected


def test_keys_ignores_line_ending(registered_process):
    process_id, proc = registered_process
    tool = ExecShellInputTool()

    _run(tool.run({
        "process_id": process_id,
        "keys": ["enter"],
        "line_ending": "\r\n",
    }))

    assert bytes(proc.stdin.buffer) == b"\r"


def test_unknown_key_name_returns_error_without_writing(registered_process):
    process_id, proc = registered_process
    tool = ExecShellInputTool()

    result = _run(tool.run({
        "process_id": process_id,
        "keys": ["down", "banana", "enter"],
    }))

    assert result.structured_content.get("error") == "Invalid parameters"
    assert "banana" in result.structured_content.get("message", "")
    assert bytes(proc.stdin.buffer) == b""


def test_concatenated_input_text_still_works(registered_process):
    process_id, proc = registered_process
    tool = ExecShellInputTool()

    raw = "\x1b[B\x1b[B\x1b[B\r"
    result = _run(tool.run({
        "process_id": process_id,
        "input_text": raw,
        "line_ending": "none",
    }))

    assert result.structured_content.get("input_sent") is True
    assert bytes(proc.stdin.buffer) == raw.encode("utf-8")


def test_both_input_text_and_keys_is_validation_error(registered_process):
    process_id, proc = registered_process
    tool = ExecShellInputTool()

    result = _run(tool.run({
        "process_id": process_id,
        "input_text": "hi",
        "keys": ["enter"],
    }))

    assert result.structured_content.get("error") == "Invalid parameters"
    assert "exactly one" in result.structured_content.get("message", "").lower()
    assert bytes(proc.stdin.buffer) == b""


def test_neither_input_text_nor_keys_is_validation_error(registered_process):
    process_id, _ = registered_process
    tool = ExecShellInputTool()

    result = _run(tool.run({"process_id": process_id}))

    assert result.structured_content.get("error") == "Invalid parameters"


def test_keys_are_written_one_at_a_time(registered_process):
    process_id, proc = registered_process
    tool = ExecShellInputTool()

    _run(tool.run({
        "process_id": process_id,
        "keys": ["down", "down", "enter"],
    }))

    assert proc.stdin.writes == [
        _KEY_NAME_TO_BYTES["down"].encode("utf-8"),
        _KEY_NAME_TO_BYTES["down"].encode("utf-8"),
        _KEY_NAME_TO_BYTES["enter"].encode("utf-8"),
    ]


def test_input_text_is_written_as_single_call(registered_process):
    process_id, proc = registered_process
    tool = ExecShellInputTool()

    _run(tool.run({
        "process_id": process_id,
        "input_text": "hello",
    }))

    assert proc.stdin.writes == [b"hello\n"]


def test_empty_keys_array_is_validation_error(registered_process):
    process_id, proc = registered_process
    tool = ExecShellInputTool()

    result = _run(tool.run({
        "process_id": process_id,
        "keys": [],
    }))

    assert result.structured_content.get("error") == "Invalid parameters"
    assert bytes(proc.stdin.buffer) == b""
