"""Instrumented SWE-agent entrypoint preserving native LLM responses.

This wrapper does not modify SWE-agent. It registers TraceVerdict's frozen LiteLLM
price table, records each exact request/response at the harness boundary, then
delegates to the official ``sweagent run`` implementation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import litellm


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


def _append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, default=str) + "\n")


def install_capture(
    *, registry_path: Path, capture_path: Path, request_path: Path | None = None
) -> None:
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    litellm.utils.register_model(registry)
    original = litellm.completion

    def traced_completion(*args: Any, **kwargs: Any):
        started = time.time()
        transport_kwargs = dict(kwargs)
        model_name = transport_kwargs.get("model") or (args[0] if args else None)
        thinking = ((transport_kwargs.get("extra_body") or {}).get("thinking") or {}).get("type")
        if model_name == "openai/deepseek-v4-flash" and thinking == "enabled":
            # Same D4-a transport normalization as the mini adapter: DeepSeek
            # thinking mode does not consume sampling controls. They are absent
            # from immutable config identity and must also be absent on the wire.
            transport_kwargs.pop("temperature", None)
            transport_kwargs.pop("top_p", None)
        messages = kwargs.get("messages") or (args[1] if len(args) > 1 else [])
        prompt_bytes = json.dumps(
            messages,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        prompt_sha256 = hashlib.sha256(prompt_bytes).hexdigest()
        if request_path is not None:
            # Write before entering the provider transport.  A response-only
            # journal cannot distinguish "no request" from a request that
            # hung until the adapter timeout.  Deliberately omit messages,
            # credentials, headers, arbitrary kwargs, and environment values.
            _append_jsonl(
                request_path,
                {
                    "timestamp": started,
                    "model": model_name,
                    "prompt_sha256": prompt_sha256,
                    "temperature": transport_kwargs.get("temperature"),
                    "top_p": transport_kwargs.get("top_p"),
                    "timeout": transport_kwargs.get("timeout"),
                    "max_tokens": transport_kwargs.get("max_tokens"),
                },
            )
        response = original(*args, **transport_kwargs)
        response_dict = _jsonable(response)
        cost = litellm.cost_calculator.completion_cost(response)
        # Never serialize api_key, environment values, or arbitrary kwargs.
        _append_jsonl(
            capture_path,
            {
                "timestamp": started,
                "model": model_name,
                "messages": messages,
                "prompt_sha256": prompt_sha256,
                "temperature": transport_kwargs.get("temperature"),
                "top_p": transport_kwargs.get("top_p"),
                "response": response_dict,
                "cost": cost,
            },
        )
        return response

    litellm.completion = traced_completion


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--request-log", type=Path)
    parser.add_argument("swe_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    swe_args = args.swe_args[1:] if args.swe_args[:1] == ["--"] else args.swe_args
    install_capture(
        registry_path=args.registry,
        capture_path=args.capture,
        request_path=args.request_log,
    )
    from sweagent.run.run_single import run_from_cli

    run_from_cli(swe_args)


if __name__ == "__main__":
    main()
