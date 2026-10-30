"""Tests for osu_tablet_assistant.otd.livetune. Nothing here talks to a real OTD or osu!."""
from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

from osu_tablet_assistant.engine import adapt
from osu_tablet_assistant.engine.recommend import RADIAL_TABLET
from osu_tablet_assistant.models import ApplyResult, FilterConfig, ProfileChanges, Rect, TabletSpec
from osu_tablet_assistant.otd import controller as ctl
from osu_tablet_assistant.otd import livetune as lt
from osu_tablet_assistant.otd import settings_io

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "settings_sample.json"
CTL472 = TabletSpec("Wacom CTL-472", 152.0, 95.0, 133.0)
RADIAL_PROPS = {"OuterRadius": 0.6, "InnerRadius": 0.04, "SmoothingCoefficient": 0.88,
                "SoftKneeScale": 1.0, "SmoothingLeakCoefficient": 0.0}


def user_settings() -> dict:
    """Synthetic copy of the user's real setup: OTD 0.6.7, CTL-472, 71.66x41.44 mm rotated 180."""
    base = json.loads(FIXTURE.read_text(encoding="utf-8"))
    prof = copy.deepcopy(base["Profiles"][0])
    prof["Tablet"] = "Wacom CTL-472"
    prof["AbsoluteModeSettings"]["Tablet"] = {"Width": 71.66, "Height": 41.44, "X": 80.17, "Y": 26.72,
                                              "Rotation": 180.0}
    prof["Filters"] = [
        {"Path": RADIAL_TABLET, "Settings": [{"Property": k, "Value": v} for k, v in RADIAL_PROPS.items()],
         "Enable": True},
        {"Path": "TabletDriverFilters.Hawku.Smoothing",
         "Settings": [{"Property": "Latency", "Value": 2.0}, {"Property": "Frequency", "Value": 1000.0}],
         "Enable": False},
        {"Path": "OpenTabletDriver.Plugin.NoiseReduction",
         "Settings": [{"Property": "Samples", "Value": 10}, {"Property": "DistanceThreshold", "Value": 0.5}],
         "Enable": False},
        {"Path": "TemporalResampler", "Settings": [{"Property": "frameShift", "Value": 0.5}], "Enable": False},
    ]
    base["Profiles"] = [prof] + base["Profiles"]
    return base


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class FakeSender:
    def __init__(self, fail_on: set[int] | None = None, raise_on: set[int] | None = None) -> None:
        self.calls: list[dict] = []
        self.times: list[float] = []
        self.fail_on, self.raise_on = fail_on or set(), raise_on or set()
        self.clock: FakeClock | None = None

    def __call__(self, settings: dict) -> ApplyResult:
        n = len(self.calls) + 1
        self.calls.append(copy.deepcopy(settings))
        self.times.append(self.clock() if self.clock else 0.0)
        if n in self.raise_on:
            raise OSError("pipe broke")
        if n in self.fail_on:
            return ApplyResult(False, "none", "livetune.error.rpc", "rpc: daemon error")
        return ApplyResult(True, "rpc", "otd.apply.ok_rpc")


def radial(settings: dict, name: str = "Wacom CTL-472") -> dict:
    prof = next(p for p in settings["Profiles"] if p["Tablet"] == name)
    return lt._values(next(f for f in prof["Filters"] if f["Path"] == RADIAL_TABLET))


def radial_change(**values) -> ProfileChanges:
    return ProfileChanges(filters=None) if not values else _radial_changes(values)


def _radial_changes(values: dict) -> ProfileChanges:
    prof = settings_io.list_profiles(user_settings())[0]
    filters = tuple(FilterConfig(f.path, f.enabled, {**f.settings, **values}) if f.path == RADIAL_TABLET else f
                    for f in prof.filters)
    return ProfileChanges(filters=filters)


def make_tuner(sender=None, clock=None, **kw) -> tuple[lt.LiveTuner, FakeSender, FakeClock]:
    clock = clock or FakeClock()
    sender = sender or FakeSender()
    sender.clock = clock
    kw.setdefault("backup", lambda d: Path(d) / "Backup" / "b.json")
    kw.setdefault("writer", lambda d, s: None)
    tuner = lt.LiveTuner(user_settings(), "Wacom CTL-472", sender, tablet=CTL472, clock=clock,
                         meta=adapt.AdaptState(), **kw)
    return tuner, sender, clock


class TunerSendTest(unittest.TestCase):
    def test_first_change_is_sent_immediately_and_not_persisted(self) -> None:
        writes = []
        tuner, sender, _ = make_tuner(config_dir=Path("C:/fake"), writer=lambda d, s: writes.append(s))
        res = tuner.propose(radial_change(OuterRadius=0.8))
        self.assertTrue(res.accepted)
        self.assertTrue(res.sent.ok)
        self.assertEqual(radial(sender.calls[0])["OuterRadius"], 0.8)
        self.assertEqual(writes, [])
        self.assertTrue(tuner.dirty)

    def test_rate_limit_coalesces_bursts(self) -> None:
        tuner, sender, clock = make_tuner()
        for i in range(5):
            tuner.propose(radial_change(OuterRadius=0.7 + 0.1 * i))
            clock.now += 0.02
        self.assertEqual(len(sender.calls), 1)
        self.assertTrue(tuner.pending)
        self.assertIsNone(tuner.pump())          # 0.10 s after the first send
        clock.now += 0.06
        out = tuner.pump()
        self.assertTrue(out.ok)
        self.assertEqual(len(sender.calls), 2)
        self.assertAlmostEqual(radial(sender.calls[-1])["OuterRadius"], 1.1)
        self.assertFalse(tuner.pending)
        self.assertIsNone(tuner.pump())
        gaps = [b - a for a, b in zip(sender.times, sender.times[1:])]
        self.assertTrue(all(g >= tuner.min_interval_s - 1e-9 for g in gaps))

    def test_flush_ignores_the_rate_limit(self) -> None:
        tuner, sender, _ = make_tuner()
        tuner.propose(radial_change(OuterRadius=0.8))
        tuner.propose(radial_change(OuterRadius=0.9))
        tuner.flush()
        self.assertEqual(len(sender.calls), 2)

    def test_no_change_is_not_sent(self) -> None:
        tuner, sender, _ = make_tuner()
        res = tuner.propose(radial_change(OuterRadius=0.6))
        self.assertFalse(res.accepted)
        self.assertEqual(sender.calls, [])

    def test_error_auto_reverts_to_last_acknowledged_state(self) -> None:
        tuner, sender, clock = make_tuner(sender=FakeSender(fail_on={2}))
        tuner.propose(radial_change(OuterRadius=0.8), meta="a")
        clock.now += 1
        res = tuner.propose(radial_change(OuterRadius=0.9), meta="b")
        self.assertFalse(res.sent.ok)
        self.assertTrue(res.sent.reverted)
        self.assertEqual(res.sent.message_key, "livetune.error.rpc")
        self.assertEqual(radial(tuner.settings)["OuterRadius"], 0.8)
        self.assertEqual(tuner.meta, "a")
        self.assertEqual(tuner.change_count, 1)
        self.assertEqual(radial(sender.calls[-1])["OuterRadius"], 0.8)   # re-sent the known state

    def test_sender_exception_is_contained(self) -> None:
        tuner, sender, _ = make_tuner(sender=FakeSender(raise_on={1}))
        res = tuner.propose(radial_change(OuterRadius=0.8))
        self.assertFalse(res.sent.ok)
        self.assertEqual(tuner.settings, user_settings())
        self.assertIn("pipe broke", res.sent.detail)


class TunerUndoBoundsTest(unittest.TestCase):
    def test_undo_restores_exact_settings_and_meta(self) -> None:
        tuner, sender, clock = make_tuner()
        original = user_settings()
        for i in range(6):
            tuner.propose(radial_change(OuterRadius=0.6 + 0.05 * (i + 1)), meta=i)
            clock.now += 1
        self.assertEqual(tuner.change_count, 6)
        for _ in range(6):
            self.assertTrue(tuner.undo().accepted)
            clock.now += 1
        self.assertEqual(tuner.settings, original)
        self.assertIsInstance(tuner.meta, adapt.AdaptState)
        self.assertEqual(sender.calls[-1], original)
        self.assertFalse(tuner.undo().accepted)
        self.assertFalse(tuner.dirty)

    def test_revert_all(self) -> None:
        tuner, sender, clock = make_tuner()
        tuner.propose(radial_change(OuterRadius=0.9), meta="x")
        clock.now += 1
        self.assertTrue(tuner.revert_all().accepted)
        self.assertEqual(tuner.settings, user_settings())
        self.assertFalse(tuner.can_undo)
        self.assertIsInstance(tuner.meta, adapt.AdaptState)
        self.assertFalse(tuner.revert_all().accepted)

    def test_changed_values_are_clamped_to_catalog_bounds(self) -> None:
        tuner, sender, _ = make_tuner()
        res = tuner.propose(radial_change(OuterRadius=99.0, SmoothingCoefficient=-3))
        self.assertTrue(res.accepted)
        self.assertEqual(radial(sender.calls[0])["OuterRadius"], 5.0)
        self.assertEqual(radial(sender.calls[0])["SmoothingCoefficient"], 0.0001)
        self.assertEqual({r.key for r in res.reasons}, {"livetune.clamped"})

    def test_untouched_user_values_outside_bounds_are_kept(self) -> None:
        settings = user_settings()
        prof = settings["Profiles"][0]
        prof["Filters"][0]["Settings"][3]["Value"] = 150.0          # SoftKneeScale, catalog max 100
        tuner = lt.LiveTuner(settings, "Wacom CTL-472", FakeSender(), tablet=CTL472, clock=FakeClock())
        prof_info = tuner.profile()
        filters = tuple(FilterConfig(f.path, f.enabled, {**f.settings, "OuterRadius": 0.7})
                        if f.path == RADIAL_TABLET else f for f in prof_info.filters)
        tuner.propose(ProfileChanges(filters=filters))
        self.assertEqual(radial(tuner.settings)["SoftKneeScale"], 150.0)
        self.assertEqual(radial(tuner.settings)["OuterRadius"], 0.7)

    def test_area_outside_tablet_or_empty_is_rejected(self) -> None:
        tuner, sender, _ = make_tuner()
        res = tuner.propose(ProfileChanges(tablet_area=Rect(160, 90, 76, 47.5, 180)))
        self.assertFalse(res.accepted)
        self.assertEqual(res.reasons[0].key, "livetune.error.area_outside")
        res = tuner.propose(ProfileChanges(tablet_area=Rect(0, 0, 76, 47.5)))
        self.assertEqual(res.reasons[0].key, "livetune.error.area_empty")
        self.assertEqual(sender.calls, [])
        ok = tuner.propose(ProfileChanges(tablet_area=Rect(73.09, 42.27, 80.17, 26.72, 180)))
        self.assertTrue(ok.accepted)

    def test_area_fits(self) -> None:
        self.assertTrue(lt.area_fits(Rect(152, 95, 76, 47.5), CTL472))
        self.assertFalse(lt.area_fits(Rect(100, 60, 76, 47.5, 90), CTL472))

    def test_missing_profile(self) -> None:
        with self.assertRaises(KeyError):
            lt.LiveTuner(user_settings(), "Nope", FakeSender())
        with self.assertRaises(TypeError):
            lt.LiveTuner([], "Nope", FakeSender())   # type: ignore[arg-type]

    def test_user_profile_round_trips_through_a_session(self) -> None:
        tuner, sender, clock = make_tuner()
        catalog = tuner.catalog
        intents = [adapt.Intent.SMOOTHER, adapt.Intent.SMOOTHER, adapt.Intent.RESPONSIVE, adapt.Intent.PRECISE,
                   adapt.Intent.AREA_BIGGER, adapt.Intent.LOOSER, adapt.Intent.AREA_SMALLER]
        for intent in intents:
            res = adapt.step(tuner.meta, tuner.profile(), intent, None, catalog, CTL472)
            self.assertTrue(tuner.propose(res.changes, res.state).accepted, intent)
            clock.now += 0.2
            tuner.pump()
        for sent in sender.calls:
            vals = radial(sent)
            for name, v in vals.items():
                p = catalog[RADIAL_TABLET].param(name)
                self.assertTrue(p.min <= v <= p.max, (name, v))
        area = tuner.profile().tablet_area
        self.assertEqual(area.rotation, 180.0)
        self.assertAlmostEqual(area.width / area.height, 71.66 / 41.44, places=2)
        self.assertEqual(tuner.settings["Profiles"][0]["FutureProfileKey"], {"Nested": [1, 2, 3]})
        while tuner.can_undo:
            tuner.undo()
        self.assertEqual(tuner.settings, user_settings())


class TunerKeepTest(unittest.TestCase):
    def test_keep_backs_up_once_then_writes_live_settings(self) -> None:
        events = []
        tuner, _, _ = make_tuner(config_dir=Path("C:/fake"),
                                 backup=lambda d: events.append("backup") or Path("C:/fake/Backup/x.json"),
                                 writer=lambda d, s: events.append(("write", radial(s)["OuterRadius"])))
        tuner.propose(radial_change(OuterRadius=0.8))
        self.assertEqual(events, ["backup"])          # first-use backup before the first send
        res = tuner.keep()
        self.assertTrue(res.ok)
        self.assertEqual(events, ["backup", ("write", 0.8)])
        self.assertFalse(tuner.dirty)
        self.assertEqual(res.backup_path, Path("C:/fake/Backup/x.json"))

    def test_keep_refused_without_backup_or_config(self) -> None:
        def broken(d):
            raise OSError("disk full")
        tuner, _, _ = make_tuner(config_dir=Path("C:/fake"), backup=broken)
        self.assertEqual(tuner.keep().message_key, "livetune.keep.no_backup")
        tuner, _, _ = make_tuner()
        self.assertEqual(tuner.keep().message_key, "livetune.keep.no_config")
        tuner, _, _ = make_tuner(config_dir=Path("C:/fake"), can_keep=False)
        self.assertEqual(tuner.keep().message_key, "livetune.keep.disabled")

    def test_keep_allowed_when_there_is_no_settings_file_yet(self) -> None:
        def missing(d):
            raise settings_io.OtdSettingsError("otd.error.settings_missing", "none")
        tuner, _, _ = make_tuner(config_dir=Path("C:/fake"), backup=missing)
        self.assertTrue(tuner.keep().ok)

    def test_keep_refused_when_last_send_fails(self) -> None:
        tuner, _, clock = make_tuner(sender=FakeSender(fail_on={2, 3}), config_dir=Path("C:/fake"))
        tuner.propose(radial_change(OuterRadius=0.8))
        tuner.propose(radial_change(OuterRadius=0.9))     # queued by the rate limit
        res = tuner.keep()
        self.assertFalse(res.ok)
        self.assertEqual(res.message_key, "livetune.keep.send_failed")

    def test_keep_on_a_real_temporary_config_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            settings_io.write_settings(folder, user_settings())
            tuner = lt.LiveTuner(settings_io.load_settings(folder), "Wacom CTL-472", FakeSender(),
                                 config_dir=folder, tablet=CTL472, clock=FakeClock())
            tuner.propose(radial_change(OuterRadius=0.75))
            self.assertTrue(tuner.keep().ok)
            self.assertEqual(radial(settings_io.load_settings(folder))["OuterRadius"], 0.75)
            backups = list((folder / settings_io.BACKUP_DIR).glob("*.json"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(radial(json.loads(backups[0].read_text(encoding="utf-8")))["OuterRadius"], 0.6)


class ControllerApplyRpcTest(unittest.TestCase):
    class Pipe(io.RawIOBase):
        def __init__(self, incoming: bytes) -> None:
            self.incoming, self.sent = io.BytesIO(incoming), bytearray()

        def readable(self) -> bool:
            return True

        def writable(self) -> bool:
            return True

        def read(self, size: int = -1) -> bytes:
            return self.incoming.read(size)

        def write(self, data) -> int:  # type: ignore[override]
            self.sent += bytes(data)
            return len(data)

    def test_apply_rpc_sends_set_settings_only(self) -> None:
        pipes = []

        def opener():
            pipes.append(self.Pipe(ctl.encode_message({"jsonrpc": "2.0", "id": 1, "result": None})))
            return pipes[-1]

        runner_calls = []
        c = ctl.OtdController(None, None, pipe_opener=opener, daemon_probe=lambda: True,
                              runner=lambda a, t: runner_calls.append(a))
        res = c.apply_rpc({"Profiles": []}, timeout_s=1.0)
        self.assertTrue(res.ok)
        self.assertEqual(res.method, "rpc")
        self.assertEqual(runner_calls, [])
        body = bytes(pipes[0].sent).split(b"\r\n\r\n", 1)[1]
        self.assertEqual(json.loads(body)["method"], "SetSettings")

    def test_apply_rpc_without_daemon_or_with_error(self) -> None:
        c = ctl.OtdController(None, None, daemon_probe=lambda: False)
        self.assertEqual(c.apply_rpc({}).message_key, "livetune.error.daemon")

        def broken():
            raise OSError("no pipe")
        c = ctl.OtdController(None, None, pipe_opener=broken, daemon_probe=lambda: True)
        res = c.apply_rpc({})
        self.assertFalse(res.ok)
        self.assertEqual(res.message_key, "livetune.error.rpc")
        with self.assertRaises(TypeError):
            c.apply_rpc([])   # type: ignore[arg-type]

    def test_controller_sender_prefers_fast_path(self) -> None:
        class Ctl:
            def __init__(self):
                self.used = []

            def apply_rpc(self, s, timeout):
                self.used.append(("rpc", timeout))
                return ApplyResult(True, "rpc", "k")

            def apply(self, s):
                self.used.append("apply")
                return ApplyResult(True, "console", "k")
        c = Ctl()
        lt.controller_sender(c, 0.5)({})
        self.assertEqual(c.used, [("rpc", 0.5)])
        self.assertTrue(lt.dry_run_sender({}).ok)


class OsuDetectionTest(unittest.TestCase):
    def test_classify(self) -> None:
        stable = lt.ProcessEntry(10, "osu!.exe", r"C:\Users\x\AppData\Local\osu!\osu!.exe")
        lazer = lt.ProcessEntry(11, "osu!.exe", r"C:\Users\x\AppData\Local\osulazer\current\osu!.exe")
        self.assertEqual(lt.classify_osu(stable).flavor, "stable")
        self.assertEqual(lt.classify_osu(lazer).flavor, "lazer")
        self.assertEqual(lt.classify_osu(lt.ProcessEntry(12, "OSU!.EXE")).flavor, "unknown")
        self.assertIsNone(lt.classify_osu(lt.ProcessEntry(13, "osu-helper.exe")))
        self.assertEqual(lt.find_osu([stable, lazer]).pid, 11)
        self.assertIsNone(lt.find_osu([lt.ProcessEntry(1, "explorer.exe")]))

    def test_is_osu_running_with_fake_lister(self) -> None:
        self.assertTrue(lt.is_osu_running(lambda: [lt.ProcessEntry(5, "osu!.exe")]))
        self.assertFalse(lt.is_osu_running(lambda: []))

    def test_parse_tasklist(self) -> None:
        text = '"System Idle Process","0","Services","0","8 K"\n"osu!.exe","4242","Console","1","300,000 K"\n'
        procs = lt.parse_tasklist_csv(text)
        self.assertEqual(procs[1], lt.ProcessEntry(4242, "osu!.exe"))
        self.assertEqual(lt.parse_tasklist_csv("garbage\n"), [])

    def test_watcher_reports_changes_and_rate_limits(self) -> None:
        clock = FakeClock()
        procs: list = []
        calls = []

        def lister():
            calls.append(clock.now)
            return list(procs)
        w = lt.OsuWatcher(lister, clock, interval_s=3.0)
        self.assertFalse(w.poll())
        procs.append(lt.ProcessEntry(7, "osu!.exe"))
        self.assertFalse(w.poll())              # not due yet
        clock.now += 3
        self.assertTrue(w.poll())
        self.assertTrue(w.running)
        clock.now += 3
        self.assertFalse(w.poll())
        procs.clear()
        self.assertTrue(w.poll(force=True))
        self.assertFalse(w.running)
        self.assertEqual(len(calls), 4)

    def test_watcher_survives_lister_errors(self) -> None:
        def broken():
            raise OSError("denied")
        w = lt.OsuWatcher(broken, FakeClock())
        self.assertFalse(w.poll())
        self.assertFalse(w.running)

    def test_real_process_listing_does_not_crash(self) -> None:
        procs = lt.list_processes()   # read-only
        self.assertIsInstance(procs, list)


if __name__ == "__main__":
    unittest.main()
