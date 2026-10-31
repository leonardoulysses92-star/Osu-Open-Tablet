"""Capture PNG screenshots of every screen (Windows only; used to review the design).

Run from the repository root::

    py -3 tools/screenshots.py [es|en] [scale, e.g. 1.25]

It drives the real window with the test harness (temp OTD folder, fake OTD and pen, nothing
outside the temp folder is touched) and saves the window images into ``docs/screenshots``.
The capture uses ``PrintWindow`` and a tiny stdlib PNG writer, so it needs no extra packages.
"""
from __future__ import annotations

import ctypes
import struct
import sys
import time
import unittest
import zlib
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
OUT = ROOT / "docs" / "screenshots"


def write_png(path: Path, width: int, height: int, bgra: bytes) -> None:
    """Write top-down BGRA pixels as an RGB PNG."""
    rows = []
    stride = width * 4
    for y in range(height):
        src = bgra[y * stride:(y + 1) * stride]
        rgb = bytearray(width * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = src[2::4], src[1::4], src[0::4]
        rows.append(b"\x00" + bytes(rgb))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b"")
    path.write_bytes(png)


def capture(window, name: str) -> Path:
    """Save the whole top-level window (title bar included) as ``OUT/name.png``."""
    for _ in range(4):
        window.update()
        time.sleep(0.12)
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    hwnd = int(window.wm_frame(), 16)
    rect = wintypes.RECT()
    ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect))  # frame bounds
    full = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(full))
    width, height = full.right - full.left, full.bottom - full.top
    screen = user32.GetDC(0)
    dc = gdi32.CreateCompatibleDC(screen)
    bitmap = gdi32.CreateCompatibleBitmap(screen, width, height)
    gdi32.SelectObject(dc, bitmap)
    user32.PrintWindow(hwnd, dc, 2)                          # PW_RENDERFULLCONTENT

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]
    header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
    buffer = ctypes.create_string_buffer(width * height * 4)
    gdi32.GetDIBits(dc, bitmap, 0, height, buffer, ctypes.byref(header), 0)
    gdi32.DeleteObject(bitmap)
    gdi32.DeleteDC(dc)
    user32.ReleaseDC(0, screen)
    # crop the invisible resize borders: keep the DWM frame bounds
    left, top = rect.left - full.left, rect.top - full.top
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        left, top, w, h = 0, 0, width, height
    raw = buffer.raw
    cropped = b"".join(raw[((top + y) * width + left) * 4:((top + y) * width + left + w) * 4] for y in range(h))
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.png"
    write_png(path, w, h, cropped)
    return path


class _Case(unittest.TestCase):
    def runTest(self) -> None:   # pragma: no cover - only a holder for addCleanup / asserts
        pass


def main() -> None:
    lang = sys.argv[1] if len(sys.argv) > 1 else "es"
    scaling = float(sys.argv[2]) if len(sys.argv) > 2 else None
    from osu_tablet_assistant.gui import i18n, wizard
    from osu_tablet_assistant.gui.app import App
    from osu_tablet_assistant.gui.session import CANDIDATE_KINDS, Step
    from test_integration import Harness
    from osu_tablet_assistant.gui import app as app_module
    i18n.set_language(lang)
    case = _Case()
    harness = Harness(case, dry_run=False)
    session = harness.session
    fake_display = session.services.detect_display
    if scaling:                    # simulate another Windows display scale: set it before the theme is built
        themed = app_module.apply_theme

        def scaled_theme(root):
            root.tk.call("tk", "scaling", scaling * 96 / 72)
            return themed(root)
        app_module.apply_theme = scaled_theme
    app = App(session)
    session.services.detect_display = fake_display      # App points it at the real screen
    root = app.root
    suffix = f"-{lang}" + (f"-{round(scaling * 100)}" if scaling else "")
    root.deiconify()
    root.lift()
    capture(root, f"01-welcome{suffix}")
    app.wizard.page._pick_goal("guided")
    capture(root, f"02-welcome-chosen{suffix}")
    s = harness.drive_to_testing(case)
    for cid in ("balanced", "keep-current"):
        cand = s.candidate(cid)
        s.apply_candidate(cand, confirmed=True)
        for kind in CANDIDATE_KINDS:
            s.run_measurement(None, kind, cand)
    s.set_rating("balanced", 4)
    for index, step in enumerate((Step.DETECT, Step.TABLET, Step.DISPLAY, Step.QUESTIONS, Step.BASELINE,
                                  Step.TESTING), start=3):
        s.step = step
        app.wizard.show_current()
        capture(root, f"{index:02d}-{step.value}{suffix}")
    s.advance()
    app.wizard.show_current()
    capture(root, f"09-result{suffix}")
    app.wizard._scroll._canvas.yview_moveto(0.55)
    capture(root, f"10-result-lower{suffix}")
    app.wizard.show_error(RuntimeError("boom"))
    capture(root, f"11-error-banner{suffix}")
    settings = app.wizard.open_settings()
    settings.geometry("+40+40")
    capture(settings, f"12-settings{suffix}")
    settings.destroy()
    dialog = wizard.SaveDialog(app.wizard.page)
    capture(dialog, f"13-save{suffix}")
    dialog.destroy()
    capture_tools(root, case, suffix)
    min_w, min_h = root.minsize()
    root.geometry(f"{min_w}x{min_h}")
    s.step = Step.TESTING
    app.wizard.show_current()
    capture(root, f"14-testing-min-size{suffix}")
    root.destroy()
    case.doCleanups()


def capture_tools(root, case: unittest.TestCase, suffix: str) -> None:
    """Companion window, Play Coach (prepare / report) and a tooltip, with the test fakes."""
    from osu_tablet_assistant.gui import coach, companion
    from osu_tablet_assistant.gui import hotkeys as hk
    from osu_tablet_assistant.gui.widgets import Tooltip
    from osu_tablet_assistant.otd import livetune as lt
    import test_companion as tc
    import test_coach
    win = companion.CompanionWindow(
        root, settings=tc.user_settings(), tablet_name="Wacom CTL-472", tablet=tc.CTL472, display=tc.DISPLAY,
        sender=tc.Sender(), sampler=tc.FakeSampler(), hotkey_backend=tc.FakeBackend(),
        watcher=lt.OsuWatcher(lambda: [lt.ProcessEntry(9, "osu!.exe", r"C:\osu!\osu!.exe")]),
        beeper=hk.Beeper(enabled=False), overlay=False, available_filters={tc.RADIAL_TABLET})
    win.root.geometry("+30+30")
    win.do("smoother")
    capture(win.root, f"15-companion{suffix}")
    win.destroy()
    env = test_coach.Env(case)
    cw = coach.CoachWindow(root, core=env.core, enable_hotkeys=False, beeper=hk.Beeper(enabled=False))
    cw.root.geometry("+30+30")
    capture(cw.root, f"16-coach-prepare{suffix}")
    cw.start_observing()
    cw._tick_observe()
    capture(cw.root, f"17-coach-watching{suffix}")
    cw.stop_observing()
    capture(cw.root, f"18-coach-report{suffix}")
    cw.destroy()
    probe = Tooltip(root.nametowidget(root.winfo_children()[0]), "Tooltip: rounded glass bubble.")
    probe.show()
    capture(probe._tip, f"19-tooltip{suffix}")
    probe.hide()


if __name__ == "__main__":
    main()
