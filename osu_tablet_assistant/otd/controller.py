"""Push settings into a running OpenTabletDriver daemon without its GUI.

Strategy (``OtdController.apply``), verified against OTD 0.6.7 sources:

1. **Console**: ``OpenTabletDriver.Console loadsettings <file>`` deserializes the
   file and calls the daemon's ``SetSettings`` over RPC. The daemon applies the
   settings *in memory only* (it never writes ``settings.json``), so callers
   persist with ``settings_io.backup_settings`` + ``write_settings``. The
   console always exits 0; failures are only visible in its output.
2. **RPC**: the same ``SetSettings`` call made directly over the daemon's named
   pipe ``OpenTabletDriver.Daemon`` (StreamJsonRpc: ``Content-Length`` framed
   UTF-8 JSON-RPC 2.0, positional params). On Linux/macOS .NET maps the pipe to
   the Unix socket ``$TMPDIR/CoreFxPipe_OpenTabletDriver.Daemon``.
3. **Preset file**: write ``<config>/Presets/<preset_name>.json`` and report
   ``ok=False, method="preset_file"`` so the GUI asks the user to load it.

Runner, pipe opener and daemon probe are injectable for tests.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, BinaryIO

from ..models import ApplyResult
from .settings_io import OtdSettingsError, console_exe_name, dumps_settings, save_preset

PIPE_NAME = "OpenTabletDriver.Daemon"
DEFAULT_PRESET_NAME = "osu-tablet-assistant"
# Strings OTD 0.6.7 Console prints on failure (it still exits with code 0).
CONSOLE_FAILURE_MARKERS = (
    "OpenTabletDriver Daemon not running",
    "Invalid settings file",
    "Unhandled exception",
    "Exception:",
)

Runner = Callable[[Sequence[str], float], "subprocess.CompletedProcess[str]"]
PipeOpener = Callable[[], BinaryIO]


class RpcError(Exception):
    """The daemon pipe failed or answered with a JSON-RPC error."""


# --------------------------------------------------------------------------
# Defaults (real system access)
# --------------------------------------------------------------------------
def run_subprocess(args: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """Run ``args`` without a console window, capturing text output."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    return subprocess.run(list(args), capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, stdin=subprocess.DEVNULL, creationflags=flags)


def unix_pipe_path(name: str = PIPE_NAME) -> Path:
    """Socket path .NET uses for a named pipe on Linux/macOS."""
    return Path(os.environ.get("TMPDIR") or "/tmp") / f"CoreFxPipe_{name}"


def daemon_pipe_exists() -> bool:
    """True when the daemon's pipe is listed (Windows: no connection is made)."""
    if sys.platform == "win32":
        try:
            return PIPE_NAME in os.listdir("\\\\.\\pipe\\")
        except OSError:
            return False
    return unix_pipe_path().exists()


def open_daemon_pipe() -> BinaryIO:
    """Open a read/write byte stream to the daemon pipe (raises ``OSError``)."""
    if sys.platform == "win32":
        return open(f"\\\\.\\pipe\\{PIPE_NAME}", "r+b", buffering=0)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(unix_pipe_path()))
        stream = sock.makefile("rwb", buffering=0)
    finally:
        sock.close()   # the file object keeps the connection open until closed
    return stream


# --------------------------------------------------------------------------
# JSON-RPC framing (StreamJsonRpc HeaderDelimitedMessageHandler)
# --------------------------------------------------------------------------
def encode_message(message: dict) -> bytes:
    """``Content-Length: N\\r\\n\\r\\n`` + UTF-8 JSON body."""
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def read_message(stream: BinaryIO) -> dict:
    """Read one framed message; raises ``RpcError`` on EOF or bad framing."""
    header = b""
    while not header.endswith(b"\r\n\r\n"):
        header += _read_exact(stream, 1)
        if len(header) > 4096:
            raise RpcError("header too long")
    length = None
    for line in header.decode("ascii", "replace").split("\r\n"):
        name, _, value = line.partition(":")
        if name == "Content-Length" and value.strip().isdigit():
            length = int(value.strip())
    if length is None:
        raise RpcError(f"no Content-Length header in {header!r}")
    try:
        message = json.loads(_read_exact(stream, length).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RpcError(f"bad message body: {exc}") from exc
    if not isinstance(message, dict):
        raise RpcError("message is not a JSON object")
    return message


def rpc_call(stream: BinaryIO, method: str, params: list[Any], request_id: int = 1) -> Any:
    """Send one request and return its ``result``, skipping event notifications."""
    data = encode_message({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
    view = memoryview(data)
    while view:
        written = stream.write(view)
        if not written:
            raise RpcError("pipe closed while writing")
        view = view[written:]
    while True:
        message = read_message(stream)
        if message.get("id") != request_id or "method" in message:
            continue
        if "error" in message:
            raise RpcError(f"daemon error: {json.dumps(message['error'], ensure_ascii=False)[:2000]}")
        return message.get("result")


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    buf = b""
    while len(buf) < size:
        chunk = stream.read(size - len(buf))
        if not chunk:
            raise RpcError("pipe closed by the daemon")
        buf += chunk
    return buf


# --------------------------------------------------------------------------
# Controller
# --------------------------------------------------------------------------
class OtdController:
    """Applies settings to a running OTD daemon: Console, then RPC, then preset file."""

    def __init__(self, install_dir: Path | None, config_dir: Path | None, *,
                 runner: Runner | None = None, pipe_opener: PipeOpener | None = None,
                 daemon_probe: Callable[[], bool] | None = None,
                 preset_name: str = DEFAULT_PRESET_NAME,
                 console_timeout_s: float = 30.0, rpc_timeout_s: float = 10.0) -> None:
        self.install_dir = Path(install_dir) if install_dir else None
        self.config_dir = Path(config_dir) if config_dir else None
        self.preset_name = preset_name
        self.console_timeout_s = console_timeout_s
        self.rpc_timeout_s = rpc_timeout_s
        self._runner = runner or run_subprocess
        self._pipe_opener = pipe_opener or open_daemon_pipe
        self._daemon_probe = daemon_probe or daemon_pipe_exists

    @property
    def console_path(self) -> Path | None:
        """The OTD console executable, if present in ``install_dir``."""
        if self.install_dir is None:
            return None
        exe = self.install_dir / console_exe_name()
        return exe if exe.is_file() else None

    def is_daemon_running(self) -> bool:
        """True when the daemon's RPC pipe exists (no connection is opened)."""
        try:
            return bool(self._daemon_probe())
        except OSError:
            return False

    def apply(self, settings: dict) -> ApplyResult:
        """Make the running daemon use ``settings`` (live only, not persisted)."""
        if not isinstance(settings, dict):
            raise TypeError("settings must be a dict")
        if not self.is_daemon_running():
            return self._fallback(settings, ["daemon: pipe not found"], "otd.apply.daemon_not_running")
        details: list[str] = []
        ok, detail = self._apply_console(settings)
        details.append(detail)
        if ok:
            return ApplyResult(True, "console", "otd.apply.ok_console", "\n".join(details))
        ok, detail = self._apply_rpc(settings)
        details.append(detail)
        if ok:
            return ApplyResult(True, "rpc", "otd.apply.ok_rpc", "\n".join(details))
        return self._fallback(settings, details, "otd.apply.manual_preset")

    def apply_rpc(self, settings: dict, timeout_s: float | None = None) -> ApplyResult:
        """Fast live apply: one ``SetSettings`` over the pipe, no Console, no preset fallback.

        Meant for repeated small changes (companion mode). Like :meth:`apply`
        it is in-memory only: the daemon never writes ``settings.json``.
        ``timeout_s`` overrides ``rpc_timeout_s`` for this call.
        """
        if not isinstance(settings, dict):
            raise TypeError("settings must be a dict")
        if not self.is_daemon_running():
            return ApplyResult(False, "none", "livetune.error.daemon", "daemon: pipe not found")
        ok, detail = self._apply_rpc(settings, timeout_s)
        return ApplyResult(ok, "rpc" if ok else "none", "otd.apply.ok_rpc" if ok else "livetune.error.rpc",
                           detail)

    def _apply_console(self, settings: dict) -> tuple[bool, str]:
        exe = self.console_path
        if exe is None:
            return False, f"console: not found in {self.install_dir}"
        fd, tmp = tempfile.mkstemp(prefix="osu-assistant-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(dumps_settings(settings))
            proc = self._runner([str(exe), "loadsettings", tmp], self.console_timeout_s)
        except subprocess.TimeoutExpired:
            return False, f"console: timed out after {self.console_timeout_s}s"
        except OSError as exc:
            return False, f"console: {exc}"
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        output = f"{proc.stdout or ''}{proc.stderr or ''}".strip()
        failed = proc.returncode != 0 or any(m in output for m in CONSOLE_FAILURE_MARKERS)
        return not failed, f"console: exit {proc.returncode}; output: {output or '(none)'}"

    def _apply_rpc(self, settings: dict, timeout_s: float | None = None) -> tuple[bool, str]:
        timeout = self.rpc_timeout_s if timeout_s is None else timeout_s
        outcome: dict[str, Any] = {}

        def worker() -> None:
            try:
                stream = self._pipe_opener()
                try:
                    rpc_call(stream, "SetSettings", [settings])
                finally:
                    stream.close()
                outcome["ok"] = True
            except (OSError, RpcError) as exc:
                outcome["error"] = str(exc)

        # A blocked synchronous pipe read cannot be interrupted, so the worker is
        # abandoned (daemon thread) when the deadline passes.
        thread = threading.Thread(target=worker, name="otd-rpc", daemon=True)
        thread.start()
        thread.join(timeout)
        if outcome.get("ok"):
            return True, "rpc: SetSettings ok"
        return False, f"rpc: {outcome.get('error', f'timed out after {timeout}s')}"

    def _fallback(self, settings: dict, details: list[str], key: str) -> ApplyResult:
        """Save the preset for manual loading; ``method="none"`` if even that fails."""
        if self.config_dir is None:
            details.append("preset: no OTD config dir")
            return ApplyResult(False, "none", "otd.apply.failed", "\n".join(details))
        try:
            path = save_preset(self.config_dir, self.preset_name, settings, overwrite=True)
        except (OSError, OtdSettingsError) as exc:
            details.append(f"preset: {exc}")
            return ApplyResult(False, "none", "otd.apply.failed", "\n".join(details))
        details.append(f"preset: {path}")
        return ApplyResult(False, "preset_file", key, "\n".join(details))
