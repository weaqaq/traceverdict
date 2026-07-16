"""Faithful mini-swe-agent injection mappings (D6-b/D7)."""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

# Config generation is a machine-readable CLI path; suppress mini's import banner.
os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

from minisweagent.models.litellm_model import LitellmModel

from traceverdict.core.simple_yaml import dump_to_path, load_path

INJECTION_DESCRIPTIONS = {
    "I1": "Remove mini's bash/editing instruction from the instance prompt",
    "I1P": "Omit the task body while preserving tool and submission protocols",
    "I2": "Truncate agent-visible tool output to 500 characters",
    "I2P": "Hide observation output while preserving returncode and exception",
    "I3": "Block pytest commands in the agent Docker interpreter only",
    "I4": "Set temperature=1.2 on a dedicated non-thinking baseline",
    "I5": "Keep the initial prompt and only the latest two assistant/tool turns",
    "I1Q": "Corrupt native tool-call names in the parsing copy",
    "I3Q": "Mount only the agent worktree read-only",
    "I4Q": "Disable tool selection on a dedicated non-thinking baseline",
    "I5P": "Reset every API request to the initial system and task messages",
}

M1_INJECTION_IDS = ("I1Q", "I2P", "I3Q", "I4Q", "I5P")
DETERMINISTIC_INJECTION_IDS = ("I1Q", "I3Q", "I4Q")
PROBABILISTIC_INJECTION_IDS = ("I2P", "I5P")
INJECTION_DISPLAY_NAMES = {
    "I1P": "I1′",
    "I2P": "I2′",
    "I1Q": "I1Q",
    "I3Q": "I3Q",
    "I4Q": "I4Q",
    "I5P": "I5P",
}


def injection_patch(injection_id: str) -> dict[str, Any]:
    injection_id = injection_id.upper()
    if injection_id not in INJECTION_DESCRIPTIONS:
        raise ValueError(f"unknown injection_id: {injection_id}")
    patch: dict[str, Any] = {"_traceverdict_injection": {"id": injection_id}}
    if injection_id == "I4":
        patch["temperature"] = 1.2
    elif injection_id == "I4Q":
        patch["tool_choice"] = "none"
    return {"model_params": patch}


def generate_injected_config(
    injection_id: str,
    base_path: str | Path,
    output_path: str | Path,
    *,
    session_id: str = "manual",
    parent_config_id: str | None = None,
) -> dict[str, Any]:
    injection_id = injection_id.upper()
    base = load_path(Path(base_path))
    if not isinstance(base, dict):
        raise ValueError("base config must be a mapping")
    result = copy.deepcopy(base)
    parent = parent_config_id or str(base["config_id"])
    result["config_id"] = f"{parent}-selftest-{session_id}-{injection_id.lower()}"
    params = dict(result.get("model_params") or {})
    if injection_id in {"I4", "I4Q"}:
        thinking = (params.get("thinking") or {}).get("type")
        if thinking != "disabled":
            raise ValueError(
                f"{injection_id} requires a dedicated thinking.type=disabled baseline (D7-a/D12-b)"
            )
        if injection_id == "I4":
            params["temperature"] = 1.2
        else:
            params["tool_choice"] = "none"
    params["_traceverdict_injection"] = {"id": injection_id}
    result["model_params"] = params
    result["notes"] = (
        f"parent_config_id={parent}; selftest_session={session_id}; "
        f"injection_id={injection_id}; {INJECTION_DESCRIPTIONS[injection_id]}"
    )
    dump_to_path(Path(output_path), result)
    return result


def _history_turns(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    turns: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") == "assistant":
            if current:
                turns.append(current)
            current = [message]
        elif current:
            current.append(message)
    if current:
        turns.append(current)
    return turns


def truncate_history(messages: list[dict[str, Any]], keep_turns: int = 2) -> list[dict[str, Any]]:
    prefix = [message for message in messages[:2] if message.get("role") in {"system", "user"}]
    turns = _history_turns(messages[2:])
    return [*prefix, *(message for turn in turns[-keep_turns:] for message in turn)]


class HistoryWindowLitellmModel(LitellmModel):
    """Public model_class wrapper used by I5; mini source remains untouched."""

    def _prepare_messages_for_api(self, messages: list[dict]) -> list[dict]:
        return super()._prepare_messages_for_api(truncate_history(messages, keep_turns=2))


class BrokenToolNameLitellmModel(LitellmModel):
    """D12-b: corrupt only a deep parsing copy; retain the original response for audit."""

    def _parse_actions(self, response) -> list[dict]:
        damaged = response.model_copy(deep=True)
        for tool_call in damaged.choices[0].message.tool_calls or []:
            tool_call.function.name = "traceverdict_broken_bash"
        return super()._parse_actions(damaged)


class HistoryResetLitellmModel(LitellmModel):
    """D12-c: every API request sees only the initial system and task messages."""

    def _prepare_messages_for_api(self, messages: list[dict]) -> list[dict]:
        return super()._prepare_messages_for_api(messages[:2])
