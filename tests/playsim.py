"""A simulated player for the Play Coach tests (synthetic: nothing touches osu! or the real driver).

``simulate`` builds a random map and the cursor/click stream of a player with a known gain, bias,
lag, jitter and timing noise, so tests can check what the analysis recovers.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from osu_tablet_assistant.engine.beatmap import playfield_to_desktop
from osu_tablet_assistant.engine.playtypes import Beatmap, ClickEvent, HitObject, PlayCapture
from osu_tablet_assistant.models import DisplaySpec, PenSample, Rect, TabletSpec

TABLET = TabletSpec("CTL-472", 152.0, 95.0)
AREA = Rect(71.66, 41.44, 76.0, 47.5, 180.0)
DISPLAY = DisplaySpec(1920, 1080, 960, 540)


@dataclass
class Player:
    gain_x: float = 1.0
    gain_y: float = 1.0
    bias_x_mm: float = 0.0          # view axes, mm
    bias_y_mm: float = 0.0
    lag_ms: float = 0.0             # the cursor trails the intended path by this much
    jitter_px: float = 0.0          # per-sample wobble
    aim_noise_mm: float = 0.3       # scatter of where each click lands
    time_sd_ms: float = 15.0
    time_bias_ms: float = 10.0
    miss_rate: float = 0.04
    scale: float = 1.0              # 1.5 = DT, 0.75 = HT
    hr: bool = False                # the player sees the map flipped vertically
    hz: float = 1000.0
    patch: tuple[float, float, float, float] | None = None   # (fx0, fy0, fx1, fy1) playfield region ...
    patch_short_mm: float = 0.0     # ... where the cursor stops this many mm short (towards the centre)


def player_for(area: Rect, ideal: Rect, **kw) -> Player:
    """The pen travel is fixed by ``ideal``; what the cursor does depends on the active ``area``."""
    from osu_tablet_assistant.engine import areafit

    vw, vh = areafit.view_size(area)
    iw, ih = areafit.view_size(ideal)
    cu, cv = areafit.tablet_mm_to_area(area, ideal.x, ideal.y)
    return Player(gain_x=iw / vw, gain_y=ih / vh, bias_x_mm=(cu - 0.5) * vw, bias_y_mm=(cv - 0.5) * vh,
                  miss_rate=0.02, aim_noise_mm=0.25, **kw)


def make_map(seed: int = 1, n: int = 120, gap_ms: float = 280.0, od: float = 8.0, cs: float = 4.0,
             fast_every: int = 0) -> Beatmap:
    rng = random.Random(seed)
    objs, t = [], 1500.0
    for i in range(n):
        gap = gap_ms * rng.uniform(0.8, 1.3)
        if fast_every and i % fast_every == 0:
            gap = 120.0
        t += gap
        objs.append(HitObject(t, rng.uniform(10, 502), rng.uniform(10, 374), "slider" if i % 7 == 3 else "circle"))
    return Beatmap("Song", "Artist", "Hard", "me", "Song", "Artist", cs, od, 8.0, 5.0, tuple(objs), "sim.osu")


def simulate(beatmap: Beatmap, player: Player, seed: int = 0, t0: float | None = None, area: Rect = AREA,
             display: DisplaySpec = DISPLAY, stop_after_s: float | None = None) -> PlayCapture:
    """The capture a player with these traits would produce on ``beatmap`` (clock starts at ``t0``)."""
    rng = random.Random(seed)
    from osu_tablet_assistant.engine import areafit

    vw, vh = areafit.view_size(area)
    mm_x, mm_y = vw / display.width_px, vh / display.height_px
    cx, cy = display.offset_x, display.offset_y
    t0 = rng.uniform(5.0, 400.0) if t0 is None else t0
    objs = beatmap.judged_objects
    when, aims, clicks = [], [], []
    for o in objs:
        ox, oy = playfield_to_desktop(o.x, 384.0 - o.y if player.hr else o.y, display)
        ax = cx + player.gain_x * (ox - cx) + player.bias_x_mm / mm_x
        ay = cy + player.gain_y * (oy - cy) + player.bias_y_mm / mm_y
        ax += rng.gauss(0, player.aim_noise_mm / mm_x)
        ay += rng.gauss(0, player.aim_noise_mm / mm_y)
        if player.patch is not None:
            fx, fy = o.x / 512.0, (384.0 - o.y if player.hr else o.y) / 384.0
            x0, y0, x1, y1 = player.patch
            if x0 <= fx <= x1 and y0 <= fy <= y1:
                pcx, pcy = playfield_to_desktop(256.0, 192.0, display)
                dx_mm, dy_mm = (pcx - ox) * mm_x, (pcy - oy) * mm_y
                length = max((dx_mm ** 2 + dy_mm ** 2) ** 0.5, 1e-6)
                ax += dx_mm / length * player.patch_short_mm / mm_x
                ay += dy_mm / length * player.patch_short_mm / mm_y
        when.append(t0 + o.time_ms / 1000.0 / player.scale)
        aims.append((ax, ay))
        if rng.random() >= player.miss_rate:
            clicks.append(ClickEvent(when[-1] + player.time_bias_ms / 1000 + rng.gauss(0, player.time_sd_ms / 1000),
                                     "z"))
    end = when[-1] + 0.5
    if stop_after_s is not None:
        end = min(end, t0 + stop_after_s)
        clicks = [c for c in clicks if c.t <= end]
    lag = player.lag_ms / 1000.0
    samples, step, k = [], 1.0 / player.hz, 0
    t = when[0] - 1.0
    while t < end:
        te = t - lag
        while k + 1 < len(when) and when[k + 1] <= te:
            k += 1
        if te <= when[0]:
            x, y = aims[0]
        else:
            t_a = when[k]
            nxt = k + 1 if k + 1 < len(when) else k
            # move towards the next aim, arriving exactly at its time (smoothstep); rest on the aim before
            span = max(when[nxt] - t_a, 1e-6)
            f = min(max((te - t_a) / span, 0.0), 1.0)
            f = f * f * (3 - 2 * f)
            x = aims[k][0] + f * (aims[nxt][0] - aims[k][0])
            y = aims[k][1] + f * (aims[nxt][1] - aims[k][1])
        samples.append(PenSample(t, x + rng.gauss(0, player.jitter_px), y + rng.gauss(0, player.jitter_px)))
        t += step
    return PlayCapture(tuple(samples), tuple(clicks), area, display, TABLET, samples[0].t, None if stop_after_s else end)
