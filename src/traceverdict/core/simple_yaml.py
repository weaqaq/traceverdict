"""Minimal YAML subset load/dump (stdlib only; not a full YAML implementation).

Supports the shapes used by TraceVerdict task/dev configs and generated mini-swe-agent configs:
mappings, lists, scalars (str/int/float/bool/null), and block scalars via '|'.
"""

from __future__ import annotations

import json
import re
from typing import Any


def dumps(data: Any) -> str:
    """Dump a JSON-compatible structure to a readable YAML-like subset."""
    return _dump(data, 0) + "\n"


def dump_to_path(path: Any, data: Any) -> None:
    from pathlib import Path

    Path(path).write_text(dumps(data), encoding="utf-8")


def loads(text: str) -> Any:
    """Load our YAML subset. Falls back to JSON if the whole document is JSON."""
    stripped = text.strip()
    if not stripped:
        return None
    if stripped[0] in "{[":
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    lines = text.replace("\r\n", "\n").split("\n")
    value, _ = _parse_block(lines, 0, 0)
    return value


def load_path(path: Any) -> Any:
    from pathlib import Path

    return loads(Path(path).read_text(encoding="utf-8"))


def _dump(data: Any, indent: int) -> str:
    sp = "  " * indent
    if isinstance(data, dict):
        if not data:
            return "{}"
        parts = []
        for k, v in data.items():
            key = str(k)
            if isinstance(v, (dict, list)):
                if not v:
                    parts.append(f"{sp}{key}: " + ("[]" if isinstance(v, list) else "{}"))
                else:
                    parts.append(f"{sp}{key}:\n{_dump(v, indent + 1)}")
            else:
                parts.append(f"{sp}{key}: {_dump_scalar(v)}")
        return "\n".join(parts)
    if isinstance(data, list):
        if not data:
            return "[]"
        parts = []
        for item in data:
            if isinstance(item, (dict, list)):
                nested = _dump(item, indent + 1)
                parts.append(f"{sp}-")
                parts.append(nested)
            else:
                parts.append(f"{sp}- {_dump_scalar(item)}")
        return "\n".join(parts)
    return f"{sp}{_dump_scalar(data)}"


def _dump_scalar(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    # Always JSON-quote non-plain strings so PyYAML (mini-swe-agent) can load reliably.
    # Includes newlines and Jinja braces {{ }} used in agent templates.
    if (
        s == ""
        or "\n" in s
        or s.strip() != s
        or any(c in s for c in ":#{}[]&*!|>%@`" + "'\"\\")
        # YAML 1.1 readers (including PyYAML) coerce on/off and yes/no to
        # booleans even though they are strings in our Python structure.
        or s.casefold() in {"true", "false", "null", "yes", "no", "on", "off"}
        or s == "~"
    ):
        return json.dumps(s, ensure_ascii=False)
    return s


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _parse_block(lines: list[str], i: int, min_indent: int) -> tuple[Any, int]:
    # Skip blanks/comments
    while i < len(lines) and (not lines[i].strip() or lines[i].lstrip().startswith("#")):
        i += 1
    if i >= len(lines):
        return None, i

    line = lines[i]
    if _indent_of(line) < min_indent:
        return None, i

    stripped = line.strip()
    # List at this level
    if stripped.startswith("- ") or stripped == "-":
        return _parse_list(lines, i, _indent_of(line))

    # Mapping
    if ":" in stripped:
        return _parse_map(lines, i, _indent_of(line))

    return _parse_scalar(stripped), i + 1


def _parse_map(lines: list[str], i: int, indent: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        cur = _indent_of(line)
        if cur < indent:
            break
        if cur > indent:
            break
        stripped = line.strip()
        if stripped.startswith("-"):
            break
        if ":" not in stripped:
            break
        key, _, rest = stripped.partition(":")
        key = key.strip()
        rest = rest.strip()
        if rest == "|" or rest == ">":
            # block scalar
            i += 1
            block_lines = []
            while i < len(lines):
                if not lines[i].strip():
                    block_lines.append("")
                    i += 1
                    continue
                if _indent_of(lines[i]) <= indent:
                    break
                block_lines.append(lines[i][indent + 2 :])
                i += 1
            result[key] = "\n".join(block_lines).rstrip("\n")
            continue
        if rest == "":
            # nested value
            i += 1
            # peek
            while i < len(lines) and not lines[i].strip():
                i += 1
            if i >= len(lines) or _indent_of(lines[i]) <= indent:
                result[key] = None
            else:
                val, i = _parse_block(lines, i, indent + 1)
                result[key] = val
        else:
            result[key] = _parse_scalar(rest)
            i += 1
    return result, i


def _parse_list(lines: list[str], i: int, indent: int) -> tuple[list[Any], int]:
    result: list[Any] = []
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        cur = _indent_of(line)
        if cur < indent:
            break
        if cur > indent:
            break
        stripped = line.strip()
        if not stripped.startswith("-"):
            break
        rest = stripped[1:].strip()
        if rest == "":
            i += 1
            val, i = _parse_block(lines, i, indent + 2)
            result.append(val)
        elif rest.startswith("{") or rest.startswith("["):
            result.append(json.loads(rest))
            i += 1
        elif (
            re.match(r"^[^:]+:(?:\s|$)", rest)
            and not rest.startswith('"')
            and not rest.startswith("'")
        ):
            # inline map start as nested map under list item — treat rest as single-line key: val
            # push synthetic mapping lines: re-parse "key: val" at indent+2 by constructing
            key, _, r2 = rest.partition(":")
            m = {key.strip(): _parse_scalar(r2.strip())}
            i += 1
            # absorb following nested keys at greater indent
            while i < len(lines):
                if not lines[i].strip():
                    i += 1
                    continue
                if _indent_of(lines[i]) <= indent:
                    break
                if lines[i].strip().startswith("-"):
                    break
                nested, i = _parse_map(lines, i, _indent_of(lines[i]))
                m.update(nested)
                break
            result.append(m)
        else:
            result.append(_parse_scalar(rest))
            i += 1
    return result, i


_FLOAT = re.compile(r"^-?(?:(?:\d+\.\d*|\d*\.\d+)|\d+[eE][+-]?\d+|(?:\d+\.\d*|\d*\.\d+)[eE][+-]?\d+)$")
_INT = re.compile(r"^-?\d+$")


def _parse_scalar(s: str) -> Any:
    if s in ("null", "~", ""):
        return None
    if s in ("true", "True", "yes"):
        return True
    if s in ("false", "False", "no"):
        return False
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        try:
            return json.loads(s if s.startswith('"') else json.dumps(s[1:-1]))
        except json.JSONDecodeError:
            return s[1:-1]
    if _INT.match(s):
        return int(s)
    if _FLOAT.match(s):
        return float(s)
    return s
