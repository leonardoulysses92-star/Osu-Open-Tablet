"""Tests for osu_tablet_assistant.otd.controller. Nothing here talks to a real OTD."""
from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import tempfile
import threading
import unittest
from collections.abc import Sequence
from pathlib import Path

from osu_tablet_assistant.models import ApplyResult
from osu_tablet_assistant.otd import controller as ctl
from osu_tablet_assistant.otd.settings_io import console_exe_name

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "settings_sample.json"
SETTINGS = json.loads(FIXTURE.read_text(encoding="utf-8"))


class FakeRunner:
    """Records console invocations and the settings file content it was given."""

    def __init__(self, stdout: str = "", returncode: int = 0, exc: BaseException | None = None) -> None:
        self.stdout, self.returncode, self.exc = stdout, returncode, exc
        self.calls: list[list[str]] = []
        self.loaded: list[dict] = []

    def __call__(self, args: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        self.loaded.append(json.loads(Path(args[2]).read_text(encoding="utf-8")))
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(list(args), self.returncode, self.stdout, "")


class FakePipe(io.RawIOBase):
    """In-memory duplex stream: ``read`` serves canned bytes, ``write`` records."""

    def __init__(self, incoming: bytes) -> None:
        self.incoming = io.BytesIO(incoming)
        self.sent = bytearray()

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        return self.incoming.read(size)

    def write(self, data) -> int:  # type: ignore[override]
        self.sent += bytes(data)
        return len(data)


def response(result: object = None, request_id: int = 1) -> bytes:
    return ctl.encode_message({"jsonrpc": "2.0", "id": request_id, "result": result})


def notification(method: str) -> bytes:
    return ctl.encode_message({"jsonrpc": "2.0", "method": method, "params": []})


class FramingTest(unittest.TestCase):
    def test_encode_and_read_roundtrip(self) -> None:
        msg = {"jsonrpc": "2.0", "id": 7, "result": {"ñ": "ü"}}
        data = ctl.encode_message(msg)
        head, _, body = data.partition(b"\r\n\r\n")
        self.assertEqual(head, f"Content-Length: {len(body)}".encode())
        self.assertEqual(ctl.read_message(io.BytesIO(data)), msg)

    def test_extra_headers_are_ignored(self) -> None:
        body = b'{"id":1,"result":null}'
        data = b"Content-Type: application/jsonrpc; charset=utf-8\r\nContent-Length: %d\r\n\r\n" % len(body) + body
        self.assertEqual(ctl.read_message(io.BytesIO(data)), {"id": 1, "result": None})

    def test_bad_frames(self) -> None:
        for data in (b"", b"Content-Length: 50\r\n\r\n{}", b"X: 1\r\n\r\n{}", b"Content-Length: 2\r\n\r\n[]",
                     b"Content-Length: 3\r\n\r\n{x}", b"A" * 5000):
            with self.subTest(data=data[:30]), self.assertRaises(ctl.RpcError):
                ctl.read_message(io.BytesIO(data))

    def test_rpc_call_skips_notifications(self) -> None:
        pipe = FakePipe(notification("Message") + response(None, 99) + notification("Resynchronize") + response(5))
        self.assertEqual(ctl.rpc_call(pipe, "SetSettings", [SETTINGS]), 5)
        request = ctl.read_message(io.BytesIO(bytes(pipe.sent)))
        self.assertEqual(request, {"jsonrpc": "2.0", "id": 1, "method": "SetSettings", "params": [SETTINGS]})

    def test_rpc_call_error(self) -> None:
        pipe = FakePipe(ctl.encode_message({"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "boom"}}))
        with self.assertRaisesRegex(ctl.RpcError, "boom"):
            ctl.rpc_call(pipe, "SetSettings", [{}])

    def test_rpc_call_over_real_socket(self) -> None:
        server, client = socket.socketpair()
        received: list[dict] = []

        def serve() -> None:
            with server, server.makefile("rwb", buffering=0) as stream:
                received.append(ctl.read_message(stream))
                stream.write(notification("TabletsChanged") + response(None))

        thread = threading.Thread(target=serve)
        thread.start()
        with client, client.makefile("rwb", buffering=0) as stream:
            self.assertIsNone(ctl.rpc_call(stream, "SetSettings", [SETTINGS]))
        thread.join(5)
        self.assertEqual(received[0]["params"], [SETTINGS])


class ControllerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.install = root / "install"
        self.install.mkdir()
        (self.install / console_exe_name()).write_bytes(b"")
        self.config = root / "config"
        self.config.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def make(self, runner: FakeRunner | None = None, pipe: object = None, running: bool = True,
             install: Path | None | bool = True, config: Path | None | bool = True,
             rpc_timeout_s: float = 5.0) -> ctl.OtdController:
        def opener():
            if isinstance(pipe, BaseException):
                raise pipe
            if callable(pipe):
                return pipe()
            if pipe is None:
                raise FileNotFoundError("no pipe")
            return pipe
        return ctl.OtdController(self.install if install is True else install,
                                 self.config if config is True else config,
                                 runner=runner or FakeRunner(), pipe_opener=opener,
                                 daemon_probe=lambda: running, rpc_timeout_s=rpc_timeout_s)

    def preset(self) -> Path:
        return self.config / "Presets" / f"{ctl.DEFAULT_PRESET_NAME}.json"

    def test_console_success(self) -> None:
        runner = FakeRunner(stdout="")
        result = self.make(runner).apply(SETTINGS)
        self.assertEqual((result.ok, result.method, result.message_key), (True, "console", "otd.apply.ok_console"))
        (args,) = runner.calls
        self.assertEqual(args[:2], [str(self.install / console_exe_name()), "loadsettings"])
        self.assertTrue(Path(args[2]).is_absolute())
        self.assertFalse(os.path.exists(args[2]))       # temp file cleaned up
        self.assertEqual(runner.loaded[0], SETTINGS)
        self.assertFalse(self.preset().exists())

    def test_console_failure_output_falls_back_to_rpc(self) -> None:
        for out, code in (("OpenTabletDriver Daemon not running\n", 0), ("Invalid settings file\n", 0),
                          ("", 1), ("Unhandled exception. System.Exception: x", 0)):
            with self.subTest(out=out, code=code):
                pipe = FakePipe(response(None))
                result = self.make(FakeRunner(out, code), pipe).apply(SETTINGS)
                self.assertEqual((result.ok, result.method, result.message_key), (True, "rpc", "otd.apply.ok_rpc"))
                self.assertIn("console: exit", result.detail)
                self.assertTrue(pipe.closed)

    def test_console_timeout_and_oserror(self) -> None:
        for exc in (subprocess.TimeoutExpired("otd", 1), PermissionError("denied")):
            with self.subTest(exc=type(exc).__name__):
                result = self.make(FakeRunner(exc=exc), FakePipe(response())).apply(SETTINGS)
                self.assertEqual(result.method, "rpc")

    def test_no_console_uses_rpc(self) -> None:
        runner = FakeRunner()
        result = self.make(runner, FakePipe(response()), install=None).apply(SETTINGS)
        self.assertEqual(result.method, "rpc")
        self.assertEqual(runner.calls, [])
        self.assertIsNone(self.make(install=self.config).console_path)

    def test_everything_fails_saves_preset(self) -> None:
        pipe = FakePipe(ctl.encode_message({"jsonrpc": "2.0", "id": 1, "error": {"message": "nope"}}))
        result = self.make(FakeRunner("Invalid settings file"), pipe).apply(SETTINGS)
        self.assertEqual((result.ok, result.method, result.message_key),
                         (False, "preset_file", "otd.apply.manual_preset"))
        self.assertEqual(json.loads(self.preset().read_text(encoding="utf-8")), SETTINGS)
        self.assertIn(str(self.preset()), result.detail)
        self.assertIn("nope", result.detail)
        # a second failing apply overwrites our own preset
        again = self.make(FakeRunner("", 1), OSError("pipe busy")).apply({"Profiles": []})
        self.assertEqual(again.method, "preset_file")
        self.assertEqual(json.loads(self.preset().read_text(encoding="utf-8")), {"Profiles": []})

    def test_rpc_timeout(self) -> None:
        release = threading.Event()

        class Blocking(FakePipe):
            def read(self, size: int = -1) -> bytes:
                release.wait(5)
                return b""

        try:
            result = self.make(FakeRunner("", 1), lambda: Blocking(b""), rpc_timeout_s=0.2).apply(SETTINGS)
        finally:
            release.set()
        self.assertEqual(result.method, "preset_file")
        self.assertIn("timed out", result.detail)

    def test_daemon_not_running(self) -> None:
        runner = FakeRunner()
        result = self.make(runner, running=False).apply(SETTINGS)
        self.assertEqual((result.ok, result.method, result.message_key),
                         (False, "preset_file", "otd.apply.daemon_not_running"))
        self.assertEqual(runner.calls, [])
        self.assertTrue(self.preset().exists())

    def test_no_config_dir(self) -> None:
        result = self.make(FakeRunner("", 1), config=None).apply(SETTINGS)
        self.assertEqual(result, ApplyResult(False, "none", "otd.apply.failed", result.detail))

    def test_preset_write_error(self) -> None:
        (self.config / "Presets").write_text("a file, not a folder", encoding="utf-8")
        result = self.make(running=False).apply(SETTINGS)
        self.assertEqual((result.method, result.message_key), ("none", "otd.apply.failed"))

    def test_rejects_non_dict(self) -> None:
        with self.assertRaises(TypeError):
            self.make().apply([])  # type: ignore[arg-type]

    def test_is_daemon_running(self) -> None:
        self.assertTrue(self.make(running=True).is_daemon_running())
        self.assertFalse(self.make(running=False).is_daemon_running())

        def broken() -> bool:
            raise OSError("no access")
        self.assertFalse(ctl.OtdController(None, None, daemon_probe=broken).is_daemon_running())

    def test_default_probe_is_read_only_and_safe(self) -> None:
        self.assertIsInstance(ctl.daemon_pipe_exists(), bool)
        self.assertEqual(ctl.unix_pipe_path("X").name, "CoreFxPipe_X")


if __name__ == "__main__":
    unittest.main()
