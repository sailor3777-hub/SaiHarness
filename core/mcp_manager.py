"""Клиент MCP: запуск серверов из конфига, список их инструментов и вызовы.

Все серверы живут в одном фоновом потоке с циклом asyncio. Каждый сервер обслуживает отдельная
долгая задача: контексты stdio_client/ClientSession (anyio) нужно входить и выходить в одной задаче.
Публичные методы блокирующие и потокобезопасные: их зовут из рабочих потоков, не из GUI.
"""
import asyncio
import json
import os
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

import mcp.types as mcp_types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONNECT_TIMEOUT = 25.0
DEFAULT_CALL_TIMEOUT = 60.0


@dataclass
class ToolInfo:
    server: str
    name: str
    description: str
    input_schema: dict
    read_only: bool | None = None
    destructive: bool | None = None

    @property
    def id(self) -> str:
        return f"{self.server}.{self.name}"


@dataclass
class ServerState:
    name: str
    status: str = "connecting"  # connecting | ready | error | disabled
    error: str = ""
    tools: list[ToolInfo] = field(default_factory=list)


def default_config(db_path: Path) -> dict:
    """Конфиг по умолчанию: встроенный сервер toolbox (формат как у Claude Desktop)."""
    return {
        "mcpServers": {
            "toolbox": {
                "command": sys.executable,
                "args": [str(PROJECT_ROOT / "tools_server.py")],
                "env": {"SAI_DB": str(db_path), "SAI_NOTES_DIR": str(db_path.parent / "notes"),
                        "SAI_SHELL_DIR": str(db_path.parent / "shell")},
            }
        }
    }


def load_config(path: Path, db_path: Path) -> dict:
    """Читает конфиг; если файла нет, создаёт его с сервером по умолчанию."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(default_config(db_path), ensure_ascii=False, indent=2), encoding="utf-8")
    return json.loads(path.read_text(encoding="utf-8"))


def _server_env(extra: dict | None) -> dict[str, str]:
    """Окружение сервера: всё окружение приложения плюс env из конфига.
    Без этого mcp отдаёт серверу урезанный набор (нет windir, ProgramData, PSModulePath, COMPUTERNAME…),
    и PowerShell из shell_run не видит модуль Hyper-V: Get-VM пуст, Get-VMSwitch «Der Wert darf nicht NULL sein»."""
    env = dict(os.environ)
    env.update({str(k): str(v) for k, v in (extra or {}).items()})
    return env


def _root_cause(exc: BaseException) -> str:
    """Самая глубокая причина из ExceptionGroup (anyio оборачивает ошибки в группы)."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def _result_text(result) -> tuple[str, bool]:
    parts = []
    for block in getattr(result, "content", None) or []:
        if isinstance(block, mcp_types.TextContent):
            parts.append(block.text)
        else:
            parts.append(f"[{getattr(block, 'type', 'данные')}: нетекстовый результат пропущен]")
    text = "\n".join(parts)
    structured = getattr(result, "structured_content", None)
    if not text and structured:
        text = json.dumps(structured, ensure_ascii=False)
    return text, bool(getattr(result, "is_error", False))


class McpManager:
    def __init__(self, log_dir: Path | None = None):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._sessions: dict[str, ClientSession] = {}
        self._stops: dict[str, asyncio.Event] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self.states: dict[str, ServerState] = {}
        self._log_dir = log_dir

    # --- поток с циклом ---

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                ready = threading.Event()

                def run():
                    asyncio.set_event_loop(loop)
                    ready.set()
                    loop.run_forever()

                self._thread = threading.Thread(target=run, name="mcp-loop", daemon=True)
                self._thread.start()
                ready.wait()
                self._loop = loop
            return self._loop

    def _run(self, coro, timeout: float | None):
        return asyncio.run_coroutine_threadsafe(coro, self._ensure_loop()).result(timeout)

    # --- серверы ---

    def connect_all(self, config: dict) -> dict[str, ServerState]:
        """(Пере)подключает все серверы из конфига. Ошибки отдельных серверов не прерывают остальные."""
        self.close_servers()
        servers = (config or {}).get("mcpServers") or {}
        return self._run(self._connect_all(servers), CONNECT_TIMEOUT * max(1, len(servers)) + 5)

    async def _connect_all(self, servers: dict) -> dict[str, ServerState]:
        loop = asyncio.get_running_loop()
        waits = []
        self.states = {}
        for name, spec in servers.items():
            state = ServerState(name)
            self.states[name] = state
            if not isinstance(spec, dict) or not spec.get("command"):
                state.status, state.error = "error", "в конфиге нет поля command"
                continue
            if spec.get("disabled"):
                state.status = "disabled"
                continue
            ready = loop.create_future()
            self._stops[name] = asyncio.Event()
            self._tasks[name] = loop.create_task(self._serve(name, spec, state, ready))
            waits.append(ready)
        if waits:
            await asyncio.gather(*waits)
        return dict(self.states)

    async def _serve(self, name: str, spec: dict, state: ServerState, ready: asyncio.Future):
        errlog = None
        try:
            params = StdioServerParameters(
                command=spec["command"], args=[str(a) for a in spec.get("args", [])],
                env=_server_env(spec.get("env")),
                cwd=spec.get("cwd"),
            )
            if self._log_dir is not None:
                self._log_dir.mkdir(parents=True, exist_ok=True)
                errlog = open(self._log_dir / f"mcp-{name}.log", "a", encoding="utf-8", buffering=1)
            async with stdio_client(params, **({"errlog": errlog} if errlog else {})) as (read, write):
                async with ClientSession(read, write) as session:
                    await asyncio.wait_for(session.initialize(), CONNECT_TIMEOUT)
                    state.tools = await asyncio.wait_for(self._list_tools(name, session), CONNECT_TIMEOUT)
                    self._sessions[name] = session
                    state.status = "ready"
                    if not ready.done():
                        ready.set_result(True)
                    await self._stops[name].wait()
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # noqa: BLE001: ошибка сервера не должна ронять обвязку
            if state.status != "ready" or not ready.done():
                state.status, state.error = "error", _root_cause(e)
            else:
                state.status, state.error = "error", f"сервер завершился: {_root_cause(e)}"
        finally:
            self._sessions.pop(name, None)
            if errlog:
                errlog.close()
            if not ready.done():
                ready.set_result(False)

    @staticmethod
    async def _list_tools(server: str, session: ClientSession) -> list[ToolInfo]:
        tools: list[ToolInfo] = []
        cursor = None
        while True:
            page = await session.list_tools(
                params=mcp_types.PaginatedRequestParams(cursor=cursor) if cursor else None
            )
            for t in page.tools:
                ann = t.annotations
                tools.append(ToolInfo(
                    server=server, name=t.name, description=(t.description or "").strip(),
                    input_schema=t.input_schema or {"type": "object", "properties": {}},
                    read_only=getattr(ann, "read_only_hint", None),
                    destructive=getattr(ann, "destructive_hint", None),
                ))
            cursor = getattr(page, "next_cursor", None)
            if not cursor:
                return tools

    def close_servers(self):
        """Останавливает все серверы (и их процессы)."""
        if self._loop is None or not self._tasks:
            return
        self._run(self._close_servers(), 15)

    async def _close_servers(self):
        for stop in self._stops.values():
            stop.set()
        tasks = list(self._tasks.values())
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=8)
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.wait(pending, timeout=4)
        self._tasks.clear()
        self._stops.clear()
        self._sessions.clear()

    def shutdown(self):
        self.close_servers()
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)

    # --- вызовы ---

    def all_tools(self) -> list[ToolInfo]:
        return [t for s in self.states.values() if s.status == "ready" for t in s.tools]

    def call(self, server: str, tool: str, arguments: dict | None, timeout: float = DEFAULT_CALL_TIMEOUT
             ) -> tuple[str, bool]:
        """Вызывает инструмент. Возвращает (текст, это_ошибка). Блокирующий, вызывать из рабочего потока."""
        try:
            return self._run(self._call(server, tool, arguments or {}, timeout), timeout + 5)
        except (TimeoutError, asyncio.TimeoutError):
            return f"Инструмент не ответил за {timeout:g} с", True
        except Exception as e:  # noqa: BLE001
            return f"Сбой вызова: {_root_cause(e)}", True

    async def _call(self, server: str, tool: str, arguments: dict, timeout: float) -> tuple[str, bool]:
        session = self._sessions.get(server)
        if session is None:
            state = self.states.get(server)
            why = f": {state.error}" if state and state.error else ""
            return f"Сервер «{server}» не подключён{why}", True
        result = await asyncio.wait_for(session.call_tool(tool, arguments), timeout)
        return _result_text(result)
