#!/usr/bin/env python3
"""Agent eXperience Interface (AXI) output layer.

Provides:
  1. TOON (Token-Oriented Object Notation) encoding on stdout (~40% token savings).
  2. AXI-compliant structured errors on stdout with actionable hints.
  3. Consistent exit codes: 0 (success/no-op), 1 (blocked/error), 2 (usage error).
  4. AxiParser: argparse that fails loud on stdout with valid flags listed.
"""

import argparse
import math
import os
import re
import sys
from typing import Any

VERSION = "2.0.0"
INDENT = 2
DELIMITER = ","

_KEY_OK = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_NUMERIC_LIKE = re.compile(r"^[+-]?[0-9]+(?:\.[0-9]+)?(?:e[+-]?[0-9]+)?$", re.IGNORECASE)
_STRUCTURAL = set(':"\\[]{}')
_CONTROL = frozenset(chr(c) for c in range(0x20))


def _is_primitive(value: Any) -> bool:
    return value is None or isinstance(value, (bool, int, float, str))


def _escape(text: str) -> str:
    out = []
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ch in _CONTROL:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return "".join(out)


def _encode_string(text: str, root: bool = False) -> str:
    if text == "":
        return '""'
    if text != text.strip():
        return '"' + _escape(text) + '"'
    if text in ("true", "false", "null"):
        return '"' + text + '"'
    if _NUMERIC_LIKE.match(text):
        return '"' + text + '"'
    if any(ch in _STRUCTURAL for ch in text):
        return '"' + _escape(text) + '"'
    if any(ch in _CONTROL for ch in text):
        return '"' + _escape(text) + '"'
    if DELIMITER in text:
        return '"' + _escape(text) + '"'
    if text[0] in "-#":
        return '"' + _escape(text) + '"'
    if root and text[0] == "\ufeff":
        return '"' + _escape(text) + '"'
    return text


def _encode_primitive(value: Any, root: bool = False) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return "null"
        return repr(value)
    return _encode_string(str(value), root=root)


def _encode_key(key: str) -> str:
    if _KEY_OK.match(key):
        return key
    return '"' + _escape(key) + '"'


def _plan_fields(items: list[dict], keys: list[str]) -> list[tuple[str, Any]] | None:
    plan = []
    for key in keys:
        column = [item.get(key) for item in items]
        if all(_is_primitive(v) for v in column):
            plan.append((key, None))
            continue
        if all(isinstance(v, dict) and v for v in column):
            subkeys = list(column[0].keys())
            if all(set(v.keys()) == set(subkeys) for v in column):
                subplan = _plan_fields(column, subkeys)
                if subplan is not None:
                    plan.append((key, subplan))
                    continue
        return None
    return plan


def _field_list(plan: list[tuple[str, Any]]) -> list[str]:
    parts = []
    for name, subplan in plan:
        if subplan is None:
            parts.append(_encode_key(name))
        else:
            parts.append(_encode_key(name) + "{" + DELIMITER.join(_field_list(subplan)) + "}")
    return parts


def _cells(obj: dict, plan: list[tuple[str, Any]]) -> list[str]:
    out = []
    for name, subplan in plan:
        value = obj.get(name)
        if subplan is None:
            out.append(_encode_primitive(value))
        else:
            out.extend(_cells(value, subplan))
    return out


def _tabular_plan(items: list) -> list[tuple[str, Any]] | None:
    if not items or not all(isinstance(it, dict) and it for it in items):
        return None
    keys = list(items[0].keys())
    if not keys:
        return None
    keyset = set(keys)
    if any(set(it.keys()) != keyset for it in items):
        return None
    return _plan_fields(items, keys)


def _keyed_plan(obj: dict) -> list[tuple[str, Any]] | None:
    if len(obj) < 2:
        return None
    values = list(obj.values())
    if not all(isinstance(v, dict) and v for v in values):
        return None
    subkeys = list(values[0].keys())
    if not all(set(v.keys()) == set(subkeys) for v in values):
        return None
    return _plan_fields(values, subkeys)


def _keyed_lines(key: str, obj: dict, plan: list[tuple[str, Any]], depth: int) -> list[str]:
    ind = " " * (INDENT * depth)
    header = DELIMITER.join(_field_list(plan))
    prefix = f"{_encode_key(key)}" if key else ""
    lines = [f"{ind}{prefix}[{len(obj)}:]{{{header}}}:"]
    row_ind = " " * (INDENT * (depth + 1))
    for entry_key, value in obj.items():
        cells = _cells(value, plan)
        lines.append(f"{row_ind}{_encode_key(str(entry_key))}: " + DELIMITER.join(cells))
    return lines


def _item_lines(obj: dict, depth: int) -> list[str]:
    ind = " " * (INDENT * depth)
    if not obj:
        return [f"{ind}-"]
    entries = list(obj.items())
    first = _field_lines(entries[0][0], entries[0][1], depth + 1)
    lines = [ind + "- " + first[0].lstrip()]
    lines.extend(first[1:])
    for key, value in entries[1:]:
        lines.extend(_field_lines(key, value, depth + 1))
    return lines


def _list_items(items: list, depth: int) -> list[str]:
    ind = " " * (INDENT * depth)
    lines = []
    for item in items:
        if _is_primitive(item):
            lines.append(f"{ind}- {_encode_primitive(item)}")
        elif isinstance(item, list):
            if all(_is_primitive(v) for v in item):
                if item:
                    cells = DELIMITER.join(_encode_primitive(v) for v in item)
                    lines.append(f"{ind}- [{len(item)}]: {cells}")
                else:
                    lines.append(f"{ind}- [0]:")
            else:
                lines.append(f"{ind}- [{len(item)}]:")
                lines.extend(_list_items(item, depth + 1))
        else:
            lines.extend(_item_lines(item, depth))
    return lines


def _array_field_lines(key: str, arr: list, depth: int) -> list[str]:
    ind = " " * (INDENT * depth)
    encoded_key = _encode_key(key)
    if not arr:
        return [f"{ind}{encoded_key}: []"]
    plan = _tabular_plan(arr)
    if plan is not None:
        header = DELIMITER.join(_field_list(plan))
        lines = [f"{ind}{encoded_key}[{len(arr)}]{{{header}}}:"]
        row_ind = " " * (INDENT * (depth + 1))
        for item in arr:
            lines.append(row_ind + DELIMITER.join(_cells(item, plan)))
        return lines
    if all(_is_primitive(v) for v in arr):
        cells = DELIMITER.join(_encode_primitive(v) for v in arr)
        return [f"{ind}{encoded_key}[{len(arr)}]: {cells}"]
    lines = [f"{ind}{encoded_key}[{len(arr)}]:"]
    lines.extend(_list_items(arr, depth + 1))
    return lines


def _field_lines(key: str, value: Any, depth: int) -> list[str]:
    ind = " " * (INDENT * depth)
    encoded_key = _encode_key(key)
    if isinstance(value, dict):
        if not value:
            return [f"{ind}{encoded_key}:"]
        plan = _keyed_plan(value)
        if plan is not None:
            return _keyed_lines(encoded_key, value, plan, depth)
        lines = [f"{ind}{encoded_key}:"]
        lines.extend(_object_lines(value, depth + 1))
        return lines
    if isinstance(value, list):
        return _array_field_lines(encoded_key, value, depth)
    return [f"{ind}{encoded_key}: {_encode_primitive(value)}"]


def _object_lines(obj: dict, depth: int) -> list[str]:
    lines = []
    for key, value in obj.items():
        lines.extend(_field_lines(key, value, depth))
    return lines


def _root_array_lines(arr: list) -> list[str]:
    if not arr:
        return ["[]"]
    plan = _tabular_plan(arr)
    if plan is not None:
        header = DELIMITER.join(_field_list(plan))
        lines = [f"[{len(arr)}]{{{header}}}:"]
        for item in arr:
            lines.append(DELIMITER.join(_cells(item, plan)))
        return lines
    if all(_is_primitive(v) for v in arr):
        cells = DELIMITER.join(_encode_primitive(v) for v in arr)
        return [f"[{len(arr)}]: {cells}"]
    lines = [f"[{len(arr)}]:"]
    lines.extend(_list_items(arr, 1))
    return lines


def toon(data: Any) -> str:
    """Encodes a JSON-like value as TOON text without trailing newline."""
    if isinstance(data, dict):
        if not data:
            return ""
        plan = _keyed_plan(data)
        if plan is not None:
            return "\n".join(_keyed_lines("", data, plan, 0))
        return "\n".join(_object_lines(data, 0))
    if isinstance(data, list):
        return "\n".join(_root_array_lines(data))
    return _encode_primitive(data, root=True)


def emit(data: Any, hints: list[str] | None = None) -> None:
    """Prints TOON output to stdout, followed by next-step hints."""
    text = toon(data)
    if text:
        print(text)
    if hints:
        print_hints(hints)


def print_hints(lines: list[str]) -> None:
    """Prints AXI help hints array."""
    valid_hints = [line for line in lines if line]
    if not valid_hints:
        return
    print(f"help[{len(valid_hints)}]:")
    for line in valid_hints:
        print(f"  {line}")


def fail(message: str, hint: str | None = None, code: int = 1) -> None:
    """Outputs a structured error to stdout and exits."""
    print(f"error: {message}")
    if hint:
        print(f"help: {hint}")
    sys.exit(code)


def usage_error(message: str, hint: str | None = None) -> None:
    """Usage error (bad flag, missing argument) on stdout with exit code 2."""
    fail(message, hint=hint, code=2)


class AxiParser(argparse.ArgumentParser):
    """ArgumentParser that outputs structured errors to stdout with valid flags listed."""

    def error(self, message: str) -> None:
        flags = []
        positional = []
        for action in self._actions:
            for option in action.option_strings:
                if option not in ("-h", "--help"):
                    flags.append(option)
            if not action.option_strings and not isinstance(action, argparse._SubParsersAction):
                positional.append(action.dest)
        hint = f"valid flags for `{self.prog}`: {', '.join(flags)}" if flags else None
        if positional:
            hint = (hint + "; " if hint else "") + "arguments: " + ", ".join(positional)
        fail(message, hint=hint, code=2)


def bin_path(argv0: str | None = None) -> str:
    """Returns absolute path of script with home directory collapsed to ~."""
    path = os.path.abspath(argv0 or sys.argv[0])
    home = os.path.expanduser("~")
    if path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path
