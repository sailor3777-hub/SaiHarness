"""Subagents: the chat's model hands a task to another model profile with delegate(agent, task).

A subagent is an ordinary model profile (system prompt, connection, model, temperature, limits). It runs the same
agent loop (core.agent_loop) inside the delegate call, with the chat's tools and permissions, but without the chat's
history and files, and without delegate itself (no recursion). Only its final answer goes back to the chat's model;
the whole course of its work is kept as a log for the user (messages.agent_log), the model does not see it.
No Qt here: the window passes the callbacks.
"""
import json
import threading
from dataclasses import dataclass, field
from typing import Callable

from core import llm
from core.agent_loop import AgentLoop
from core.context_builder import build_messages
from core.tools import META_TOOLS, ToolRouter, WithLog, describe_call, result_label

DESCRIPTION_MAX = 160   # an agent's line in the catalog
LOG_RESULT_MAX = 2000   # how much of one tool result the log keeps

CATALOG_HEAD = (
    "Agents. delegate(agent, task) hands a task to an agent: another model with its own settings that works on it "
    "with the same tools as you and replies with the result. The agent does not see this conversation, so write the "
    "task in full: the goal, every detail it needs (paths, names, data, what the user said), the constraints and what "
    "the result should look like. Delegate when a task matches an agent's description; answer simple things yourself. "
    "Check the agent's result before you pass it on.")

TASK_NOTE = (
    "You are working as an agent for another model, which gave you this task. Nobody can answer your questions: "
    "if something is unclear, choose a sensible option and say which one you chose. Do the task yourself with the "
    "tools. Your final reply goes to that model: give the result, short and complete. Save a large result (code, "
    "a document) to a file and give its path instead of the whole text.")


def description(profile) -> str:
    """The agent's line for the catalog: its own description, or the start of the system prompt."""
    text = (profile["agent_description"] or "").strip() or (profile["system_prompt"] or "").strip()
    text = " ".join(text.split())
    if not text:
        return "no description"
    return text if len(text) <= DESCRIPTION_MAX else text[:DESCRIPTION_MAX - 1].rstrip() + "…"


def catalog_block(profiles) -> str:
    """The agents block of the chat's system message (empty if no agents are enabled)."""
    if not profiles:
        return ""
    lines = "\n".join(f"- {p['name']} — {description(p)}" for p in profiles)
    return f"{CATALOG_HEAD}\nAgents:\n{lines}"


def find_agent(profiles, name: str):
    """A profile by name: exact, then ignoring case."""
    for p in profiles:
        if p["name"] == name:
            return p
    lowered = [p for p in profiles if p["name"].lower() == name.strip().lower()]
    return lowered[0] if len(lowered) == 1 else None


@dataclass
class Setup:
    """What a subagent shares with the chat: tools, permissions, the cancel button and the window's callbacks."""
    manager: object = None                        # the chat's McpManager (or the composite one with remote hosts)
    catalog: object = None                        # core.tools.Catalog of the chat's tools; None: no tools
    policy_for: Callable | None = None            # the chat's effective policy (auto-approve applied)
    confirm: Callable | None = None               # confirm(agent, tool_id, arguments) -> bool
    tools_text: Callable | None = None            # tools_text(profile) -> the catalog block for that profile
    cancel: threading.Event = field(default_factory=threading.Event)
    progress: Callable | None = None              # progress(text): a status line for the window
    context_info: Callable = llm.get_context_info  # (url, key, model) -> (limit, loaded, maximum)


def _log_json(agent: str, model: str, task: str, steps: list, final: str, reasoning: str, notices: list, error: str) -> str:
    return json.dumps({"agent": agent, "model": model, "task": task, "steps": steps, "final": final,
                       "reasoning": reasoning, "notices": notices, "error": error}, ensure_ascii=False)


def run(profile, connection, task: str, setup: Setup) -> tuple[str, bool]:
    """Runs the agent on the task; returns (text for the chat's model, is_error). The text is a WithLog with the log;
    an error text has no ERROR prefix: the router adds it and counts repeated errors."""
    name = profile["name"]
    if connection is None:
        return f"agent {name} has no connection: choose one for its profile in Model Builder.", True
    catalog = setup.catalog if setup.catalog is not None and setup.catalog.tools else None
    router = None
    if catalog is not None:
        router = ToolRouter(setup.manager, catalog, policy_for=setup.policy_for)
        if setup.confirm is not None:
            router.confirm = lambda tool_id, args: setup.confirm(name, tool_id, args)
    catalog_text = setup.tools_text(profile) if (setup.tools_text and catalog is not None) else ""
    messages = build_messages(profile, [], None, catalog_text)
    messages.append({"role": "user", "content": f"{task}\n\n{TASK_NOTE}"})

    steps: list[dict] = []
    notices: list[str] = []
    progress = setup.progress or (lambda _text: None)
    max_steps = profile["max_steps"]

    def on_running(fn: str, arguments: str):
        progress(f"{name}: step {len(steps) + 1}/{max_steps} · {describe_call(fn, arguments)}")

    def on_round(assistant: dict, results: list):
        labels = {c["id"]: (c["function"]["name"], c["function"]["arguments"]) for c in assistant["tool_calls"]}
        calls = []
        for r in results:
            fn, arguments = labels.get(r["tool_call_id"], (r["name"], ""))
            text = r["content"]
            calls.append({"call": describe_call(fn, arguments), "label": result_label(fn, arguments),
                          "result": text if len(text) <= LOG_RESULT_MAX else text[:LOG_RESULT_MAX] + "…"})
        steps.append({"reasoning": assistant["reasoning"], "content": assistant["content"], "calls": calls})

    def on_delta(kind: str, text: str):
        if kind == "notice":
            notices.append(text)
            progress(f"{name}: {text}")

    limit = None
    try:
        limit = setup.context_info(connection["url"], connection["api_key"], profile["base_model"])[0]
    except Exception:  # an unknown limit only turns the context check off, as for the chat
        limit = None
    loop = AgentLoop(
        connection["url"], connection["api_key"], profile["base_model"], profile["temperature"], profile["max_tokens"],
        tools=META_TOOLS if router else None, router=router, max_steps=max_steps, context_limit=limit,
        cancel=setup.cancel, on_delta=on_delta, on_tool_running=on_running, on_round=on_round,
        log_tag=f"agent {name}",
    )
    progress(f"{name}: working on the task…")
    error = ""
    try:
        finished = loop.run(messages)
    except llm.LLMError as e:
        finished, error = False, str(e)
    content, reasoning = loop.content.strip(), loop.reasoning.strip()
    if not error and not content and llm.answer_in_reasoning(content, reasoning, " ".join(notices)):
        content, reasoning = reasoning, ""   # the answer arrived as reasoning (Qwen 3.5 in LM Studio)
    log = _log_json(name, profile["base_model"], task, steps, content, reasoning, notices, error)
    if error:
        return WithLog(f"agent {name} failed: {error}", log), True
    if not finished:
        return WithLog(f"agent {name} was stopped before it finished.", log), True
    if not content:
        return WithLog(f"agent {name} returned an empty reply.", log), True
    return WithLog(f"Result from agent {name}:\n{content}", log), False


def render_log(raw: str) -> str:
    """The log as Markdown for the window (an empty string if there is none or it is damaged)."""
    try:
        log = json.loads(raw) if raw else None
    except ValueError:
        return ""
    if not isinstance(log, dict):
        return ""
    out = [f"**Agent:** {log.get('agent', '?')} · {log.get('model', '')}", "", "**Task:**", "", log.get("task", ""), ""]
    for i, step in enumerate(log.get("steps") or [], 1):
        out += [f"### Step {i}", ""]
        if step.get("reasoning"):
            out += [f"*Reasoning:* {step['reasoning']}", ""]
        if step.get("content"):
            out += [step["content"], ""]
        for c in step.get("calls") or []:
            out += [f"▸ `{c.get('call', '')}`", "", "```", c.get("result", ""), "```", ""]
    for notice in log.get("notices") or []:
        out += [f"*{notice}*", ""]
    if log.get("error"):
        out += [f"**Error:** {log['error']}", ""]
    if log.get("final"):
        out += ["### Answer", "", log["final"]]
    return "\n".join(out)
