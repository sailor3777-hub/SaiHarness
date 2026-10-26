"""The tools a connected host exposes, as the catalog sees them (no Qt).

A connected agent announces only tool *names* over the channel; their schemas,
descriptions and read-only flags are fixed and known to the main harness, so they
live here, in one place shared by both sides' expectations. The main harness
turns the announced names into ``ToolInfo`` entries named ``host_<name>.<tool>``.

Because a host tool is an ordinary ``ToolInfo`` with ``read_only`` set, it flows
through the existing catalog, ``ToolRouter`` and permission machinery unchanged:
``core.tools.default_policy`` runs read-only tools automatically and makes the
write/shell ones ask via the normal confirmation dialog. The descriptions say the
tool runs on a remote machine so the model does not mistake it for a local one.
"""
from __future__ import annotations

from core.mcp_manager import ToolInfo

SERVER_PREFIX = "host_"

_STR = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


# name -> (read_only, description template, schema). {host} and {os} are filled per agent.
TOOL_SPECS: dict[str, dict] = {
    "shell_run": {
        "read_only": False,
        "description": "Runs a shell (PowerShell) command on the remote host {host} ({os}). "
                       "This executes on another machine; its effects are real and irreversible. Waits up to "
                       "45 seconds; a command that is not done by then keeps running in the background (up to "
                       "1 hour): the reply gives a run_id, follow it with shell_output, stop it with shell_stop.",
        "schema": _schema({"command": _STR, "cwd": _STR}, ["command"]),
    },
    "shell_output": {
        "read_only": True,
        "description": "Reads the output of a command started by shell_run on the remote host {host} ({os}): "
                       "run_id from its reply, offset from the hint at the end. Also shows whether the command "
                       "is still running or its exit code. The command is not run again.",
        "schema": _schema({"run_id": _INT, "offset": _INT}, ["run_id"]),
    },
    "shell_stop": {
        "read_only": False,
        "description": "Stops a command started by shell_run on the remote host {host} ({os}) that is still "
                       "running in the background (kills it and what it started).",
        "schema": _schema({"run_id": _INT}, ["run_id"]),
    },
    "fs_read": {
        "read_only": True,
        "description": "Reads a text file on the remote host {host} ({os}): up to max_chars characters "
                       "from offset.",
        "schema": _schema({"path": _STR, "max_chars": _INT, "offset": _INT}, ["path"]),
    },
    "fs_list": {
        "read_only": True,
        "description": "Lists a folder on the remote host {host} ({os}). Any folder on the host; without path: its start folders (or the drives).",
        "schema": _schema({"path": _STR}, []),
    },
    "fs_search": {
        "read_only": True,
        "description": "Searches files for a regular expression on the remote host {host} ({os}).",
        "schema": _schema({"pattern": _STR, "path": _STR, "ignore_case": _BOOL, "max_results": _INT}, ["pattern"]),
    },
}


def server_name(host_name: str) -> str:
    return f"{SERVER_PREFIX}{host_name}"


def is_host_server(server: str) -> bool:
    return server.startswith(SERVER_PREFIX)


def host_name_from_server(server: str) -> str | None:
    return server[len(SERVER_PREFIX):] if is_host_server(server) else None


def prompt_block(hosts) -> str:
    """A short context block about the connected hosts, or "" when none are connected.

    Mirrors core.memory.prompt_block: added to the system message only while it has
    content, so the prompt does not grow when there are no agents. ``hosts`` is an
    iterable of (name, os) pairs.
    """
    listed = ", ".join(f"{name} ({os or 'unknown OS'})" for name, os in hosts)
    if not listed:
        return ""
    return ("Remote hosts: you have agents on other machines. Calling a host_<name>.<tool> tool "
            "runs the command on that machine, where its effects are real and irreversible, so target "
            f"the right host and confirm before anything destructive. Connected hosts: {listed}.")


def tool_infos(host_name: str, os_name: str, tool_names) -> list[ToolInfo]:
    """ToolInfo entries for the tools a host announced. Unknown names are ignored."""
    server = server_name(host_name)
    infos: list[ToolInfo] = []
    for name in tool_names or ():
        spec = TOOL_SPECS.get(name)
        if spec is None:
            continue
        infos.append(ToolInfo(
            server=server, name=name,
            description=spec["description"].format(host=host_name, os=os_name or "unknown OS"),
            input_schema=spec["schema"],
            read_only=spec["read_only"],
            destructive=not spec["read_only"],
        ))
    return infos
