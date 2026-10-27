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

AGENT_VERSION = "2"  # 1: free loop. 2: enforced step budget (forced answer on the last step) + duplicate-call guard
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
    forced_submit: bool = False    # the budget ran out and only submit_diagnosis was offered
    duplicate_calls: int = 0       # identical tool calls answered from the guard, not re-run
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
    seen: dict[str, int] = {}  # tool call (name + args) -> step it was first made

    for step in range(1, max_steps + 1):
        result.steps = step
        remaining = max_steps - step
        offered = all_tools
        if remaining == 0:
            # The budget is enforced in code, not only stated in the prompt: on the last step the only
            # possible move is an answer, with whatever confidence the evidence supports.
            offered = [SUBMIT_TOOL]
            result.forced_submit = True
            messages.append({"role": "user", "content": "Investigation budget exhausted: no more tools. Call "
                             "submit_diagnosis now with the most likely root cause and an honest confidence."})
        elif remaining == 2:
            messages.append({"role": "user", "content": "2 steps left. Run only the check that would change "
                             "your conclusion, then call submit_diagnosis."})
        try:
            reply = await llm(messages, offered)
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
                elif c["name"] not in {t["function"]["name"] for t in offered}:
                    # never run a tool the model was not offered at this step (e.g. after the budget ran out)
                    output = f"ERROR: {c['name']} is not available now. Call submit_diagnosis."
                else:
                    key = c["name"] + json.dumps(args, sort_keys=True)
                    if key in seen:  # same call, same data: answering again only burns a round trip
                        result.duplicate_calls += 1
                        output = (f"DUPLICATE: you already called {c['name']} with these arguments at step "
                                  f"{seen[key]}; the result has not changed. Use a different check, or call "
                                  "submit_diagnosis.")
                    else:
                        seen[key] = step
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
