"""The agent loop, independent of the LLM provider and of MCP (both are injected), so it is unit-testable.

llm(messages, tools) -> {"content": str|None, "tool_calls": [{"id", "name", "arguments", "extra"?}], "usage": {...}}
  "extra": provider-specific fields of the call (e.g. Gemini thought signatures), echoed back verbatim.
call_tool(name, args) -> str
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from prompts import ACTIONS, COMPONENTS, FAULTS, SUBMIT_TOOL, SYSTEM

LLM = Callable[[list[dict], list[dict]], Awaitable[dict]]
ToolCaller = Callable[[str, dict], Awaitable[str]]

MAX_STEPS = 12
MAX_TOOL_CHARS = 2500  # keeps every request well under small context / per-request limits


@dataclass
class Result:
    diagnosis: dict | None
    steps: int = 0
    tool_calls: list[dict] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    waited_s: float = 0.0          # quota pacing / backoff inside seconds
    served_models: list[str] = field(default_factory=list)
    stop_reason: str = ""


def validate(args: dict) -> str | None:
    """Return an error message for the model, or None if the diagnosis is well formed."""
    if args.get("component") not in COMPONENTS:
        return f"component must be one of {COMPONENTS}"
    if args.get("fault") not in FAULTS:
        return f"fault must be one of {FAULTS}"
    if args.get("proposed_action") not in ACTIONS:
        return f"proposed_action must be one of {ACTIONS}"
    if not isinstance(args.get("evidence"), list) or not args["evidence"]:
        return "evidence must be a non-empty list of short facts"
    return None


async def investigate(alert_text: str, tools: list[dict], llm: LLM, call_tool: ToolCaller,
                      max_steps: int = MAX_STEPS) -> Result:
    started = time.monotonic()
    messages: list[dict] = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": alert_text}]
    all_tools = tools + [SUBMIT_TOOL]
    result = Result(diagnosis=None)

    for step in range(1, max_steps + 1):
        result.steps = step
        try:
            reply = await llm(messages, all_tools)
        except Exception as e:  # provider down, quota exhausted, bad model id: record it, don't crash an eval
            result.stop_reason = f"llm_error: {type(e).__name__}: {str(e)[:300]}"
            result.seconds = time.monotonic() - started
            return result
        result.waited_s += reply.get("waited_s", 0.0)
        if reply.get("model") and reply["model"] not in result.served_models:
            result.served_models.append(reply["model"])
        usage = reply.get("usage") or {}
        result.prompt_tokens += usage.get("prompt_tokens", 0)
        result.completion_tokens += usage.get("completion_tokens", 0)
        calls = reply.get("tool_calls") or []
        messages.append({
            **(reply.get("extra") or {}),
            "role": "assistant", "content": reply.get("content") or "",
            **({"tool_calls": [{**(c.get("extra") or {}), "id": c["id"], "type": "function",
                                "function": {"name": c["name"], "arguments": c["arguments"]}} for c in calls]}
               if calls else {}),
        })
        if not calls:
            messages.append({"role": "user", "content": "Use a tool, or call submit_diagnosis if the evidence is clear."})
            continue
        for c in calls:
            try:
                args = json.loads(c["arguments"] or "{}")
            except json.JSONDecodeError:
                args, output = {}, "ERROR: arguments were not valid JSON."
            else:
                if c["name"] == "submit_diagnosis":
                    problem = validate(args)
                    if problem is None:
                        result.diagnosis = args
                        result.stop_reason = "submitted"
                        result.seconds = time.monotonic() - started
                        return result
                    output = f"ERROR: {problem}. Call submit_diagnosis again."
                else:
                    t0 = time.monotonic()
                    output = await call_tool(c["name"], args)
                    result.tool_calls.append({"name": c["name"], "args": args,
                                              "seconds": round(time.monotonic() - t0, 2),
                                              "chars": len(output), "error": output.startswith("ERROR")})
            if len(output) > MAX_TOOL_CHARS:
                output = output[:MAX_TOOL_CHARS] + "\n[truncated: narrow the query]"
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": output})

    result.stop_reason = "max_steps"
    result.seconds = time.monotonic() - started
    return result


def score(diagnosis: dict | None, truth: dict) -> dict:
    """Root cause = exact component AND fault (also_accept / also_accept_component list other labels
    a careful engineer could defend). The proposed remediation is scored separately."""
    if not diagnosis:
        return {"component_ok": False, "fault_ok": False, "correct": False, "action_ok": False}
    component_ok = diagnosis.get("component") in [truth["component"], *truth.get("also_accept_component", [])]
    fault_ok = diagnosis.get("fault") in [truth["fault"], *truth.get("also_accept", [])]
    expected = truth.get("expected_remediation")
    expected = [expected] if isinstance(expected, str) else (expected or [])
    action_ok = diagnosis.get("proposed_action") in expected if expected else None
    return {"component_ok": component_ok, "fault_ok": fault_ok, "correct": component_ok and fault_ok,
            "action_ok": action_ok}
