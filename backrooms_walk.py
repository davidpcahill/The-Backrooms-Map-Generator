#!/usr/bin/env python3
"""First-person walkthrough of a generated Backrooms map.

A software renderer in the spirit of Build-engine sector casting: every cell
has its own floor and ceiling height — and now floor *slopes* — so the
levels can do what the canon says they do:

Level 0 (--level 0, default):
- grand halls with 30-foot ceilings where the fluorescent light barely
  reaches the carpet; crawlspaces around four feet tall (auto-crouch)
- the Pitfalls: lattice-pattern fields of carpeted shafts ~8 m deep
- stairs and ramps down into sunken wings; subtly tilted "raked" floors
- textured striped wallpaper with a dark chair-rail trim, drop-ceiling
  fluorescent panels, blackout zones

Level 1 (--level 1):
- an endless concrete parking structure: formwork-lined walls, pillar
  forests, rows of strip lighting, garage ramps, a deeper 60 Hz hum,
  and water dripping somewhere out of sight

Both levels:
- Peripheral Shift: the map re-carves itself where you aren't looking
- synthesized ambience: fluorescent ballast buzz, distant footsteps that
  approach or recede in stereo, banks of lights that strobe and die
  (then slowly hum back to life)
- a route-planning auto-walker with smoothed steering and head bob

Scale: 1 world unit = one normal room height (~2.7 m / 9 ft).

Run it:

    python backrooms_walk.py                 # auto-walk demo (it drives)
    python backrooms_walk.py --manual        # you drive
    python backrooms_walk.py --level 1       # the parking garage
    python backrooms_walk.py --export map.json      # dump the world as JSON
    python backrooms_walk.py --record demo.gif --seconds 10   # headless GIF

Controls:

    TAB        toggle auto-walk
    W/A/S/D    move / strafe        arrows or Q/E   turn
    M          toggle minimap       R               new map (new seed)
    F12        save screenshot      ESC             quit
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
from array import array
from collections import deque

import backrooms_generator as bg

# Rendering
INTERNAL_W, INTERNAL_H = 480, 300
HALF_H = INTERNAL_H // 2
WINDOW_SCALE = 2
FOV = math.radians(72)
PROJ_K = (INTERNAL_W / 2) / math.tan(FOV / 2)   # square-pixel projection
MAX_DEPTH = 24.0


def set_resolution(w: int, h: int) -> None:
    """Change the internal render resolution (--hires)."""
    global INTERNAL_W, INTERNAL_H, HALF_H, PROJ_K
    INTERNAL_W, INTERNAL_H = w, h
    HALF_H = h // 2
    PROJ_K = (w / 2) / math.tan(FOV / 2)

# Wall texture: PX_PER_UNIT rows per world unit, TEX_UNITS units tall.
TEX_W = 128
PX_PER_UNIT = 128
TEX_UNITS = 4
TEX_H = PX_PER_UNIT * TEX_UNITS
SHADE_LEVELS = 24

# Heights (world units; 1.0 = normal ceiling)
EYE_STAND = 0.55
STEP_UP = 0.27          # max auto-step, like a stair riser
WALKER_MAX_DROP = 0.45  # auto-walker won't walk off bigger ledges
PIT_FLOOR = -3.0
FALL_LIMIT = -1.6       # below this you've fallen into a pit: respawn

MOVE_SPEED = 2.2
TURN_SPEED = math.radians(115)
ACCEL = 6.0                     # velocity smoothing (1/s)
BOB_AMPLITUDE = 0.022
BOB_STRIDE_HZ = 1.85
PLAYER_RADIUS = 0.22
GRAVITY = 9.0
SHIFT_PERIOD = 1.6
SHIFT_SAFE_RADIUS = 10.0

TRIM_LO, TRIM_HI = 0.30, 0.345  # chair-rail band, in units above local floor

SAMPLE_RATE = 22050

# ---------------------------------------------------------------------------
# Level styles
# ---------------------------------------------------------------------------

STYLES = {
    0: dict(
        name="Level 0", kind="wallpaper",
        wall_upper=(221, 210, 156), wall_lower=(196, 180, 122),
        wall_trim=(118, 100, 60), ceil_tile=(209, 201, 168),
        light_panel=(255, 252, 224), carpet=(177, 157, 112),
        pit_shaft=(58, 50, 32), pit_bottom=(8, 7, 4), fog=(24, 20, 8),
        hum_freq=120, ceil_norm=1.0,
        tall=(3, 5), tall_h=(1.8, 3.4), crawl=(2, 4), sunken=(2, 3),
        pits=(1, 2), blackouts=(1, 2), raked=(1, 2), ramp_chance=0.5,
        panel=lambda x, y: x % 2 == 1 and y % 3 == 1, panel_prob=0.7,
        gen=dict(rooms=4, pillar_rooms=3, poly_rooms=3),
        drips=False,
    ),
    1: dict(
        name="Level 1", kind="concrete",
        wall_upper=(148, 146, 138), wall_lower=(122, 120, 113),
        wall_trim=(96, 94, 88), ceil_tile=(94, 94, 92),
        light_panel=(232, 238, 232), carpet=(104, 102, 96),
        pit_shaft=(42, 42, 40), pit_bottom=(6, 6, 6), fog=(9, 9, 11),
        hum_freq=60, ceil_norm=1.15,
        tall=(1, 2), tall_h=(1.8, 2.6), crawl=(0, 1), sunken=(3, 4),
        pits=(0, 0), blackouts=(2, 3), raked=(2, 3), ramp_chance=1.0,
        panel=lambda x, y: y % 4 == 2, panel_prob=0.55,
        gen=dict(rooms=3, pillar_rooms=6, poly_rooms=1),
        drips=True,
    ),
}

STYLE = STYLES[0]


def apply_style(level: int) -> None:
    """Set the active level style. Palette lives in module globals because
    shade() and the renderer are hot paths."""
    global STYLE, WALL_UPPER, WALL_LOWER, WALL_TRIM, CEIL_TILE
    global LIGHT_PANEL, CARPET, PIT_SHAFT, PIT_BOTTOM, FOG
    STYLE = STYLES[level]
    WALL_UPPER = STYLE["wall_upper"]
    WALL_LOWER = STYLE["wall_lower"]
    WALL_TRIM = STYLE["wall_trim"]
    CEIL_TILE = STYLE["ceil_tile"]
    LIGHT_PANEL = STYLE["light_panel"]
    CARPET = STYLE["carpet"]
    PIT_SHAFT = STYLE["pit_shaft"]
    PIT_BOTTOM = STYLE["pit_bottom"]
    FOG = STYLE["fog"]


apply_style(0)


def shade(color, t, dim=1.0):
    """Blend toward fog by t (0 near, 1 far), scaled by local light."""
    if t > 1.0:
        t = 1.0
    elif t < 0.0:
        t = 0.0
    r, g, b = color
    fr, fg, fb = FOG
    return (min(255, int((r + (fr - r) * t) * dim)),
            min(255, int((g + (fg - g) * t) * dim)),
            min(255, int((b + (fb - b) * t) * dim)))


# ---------------------------------------------------------------------------
# Wall textures
# ---------------------------------------------------------------------------

def make_wall_textures(pygame_module):
    """A TEX_W x TEX_H strip covering 4 world units of wall above the local
    floor (v=TEX_H is floor level), pre-shaded into SHADE_LEVELS fog blends.

    Level 0: striped cream wallpaper with speckle and a dark chair-rail trim.
    Level 1: bare concrete with formwork lines, joints, and grime."""
    rng = random.Random(7)
    tex = pygame_module.Surface((TEX_W, TEX_H))
    tex.fill(WALL_UPPER)

    if STYLE["kind"] == "wallpaper":
        band_w = 16
        for x0 in range(0, TEX_W, band_w * 2):
            tex.fill(tuple(max(0, c - 10) for c in WALL_UPPER),
                     (x0, 0, band_w, TEX_H))
        for x0 in range(0, TEX_W, band_w):
            tex.fill(tuple(max(0, c - 14) for c in WALL_UPPER),
                     (x0, 0, 1, TEX_H))
        for _ in range(900):
            x = rng.randrange(TEX_W)
            y = rng.randrange(TEX_H)
            delta = rng.choice((-10, -6, 5))
            tex.fill(tuple(min(255, max(0, c + delta)) for c in WALL_UPPER),
                     (x, y, 1, rng.randint(1, 2)))
        lower_top = TEX_H - int(TRIM_LO * PX_PER_UNIT)
        trim_top = TEX_H - int(TRIM_HI * PX_PER_UNIT)
        tex.fill(WALL_LOWER, (0, lower_top, TEX_W, TEX_H - lower_top))
        for _ in range(350):
            x = rng.randrange(TEX_W)
            y = rng.randrange(lower_top, TEX_H)
            tex.fill(tuple(max(0, c - rng.randint(3, 10)) for c in WALL_LOWER),
                     (x, y, 1, 2))
        tex.fill(WALL_TRIM, (0, trim_top, TEX_W, lower_top - trim_top))
        tex.fill(tuple(min(255, c + 26) for c in WALL_TRIM),
                 (0, trim_top, TEX_W, 1))
    else:  # concrete
        for vy in range(0, TEX_H, 64):     # horizontal formwork lines
            tex.fill(tuple(max(0, c - 16) for c in WALL_UPPER),
                     (0, vy, TEX_W, 2))
        for x0 in range(0, TEX_W, 32):     # faint vertical joints
            tex.fill(tuple(max(0, c - 7) for c in WALL_UPPER),
                     (x0, 0, 1, TEX_H))
        for _ in range(1600):              # aggregate speckle
            x = rng.randrange(TEX_W)
            y = rng.randrange(TEX_H)
            delta = rng.choice((-9, -5, 4))
            tex.fill(tuple(min(255, max(0, c + delta)) for c in WALL_UPPER),
                     (x, y, 1, rng.randint(1, 2)))
        for _ in range(40):                # water stain streaks
            x = rng.randrange(TEX_W)
            y0 = rng.randrange(TEX_H - 60)
            tex.fill(tuple(max(0, c - rng.randint(8, 14)) for c in WALL_UPPER),
                     (x, y0, 1, rng.randint(15, 60)))
        for i, drop in enumerate((8, 14, 20)):   # grime skirt at floor level
            tex.fill(tuple(max(0, c - drop) for c in WALL_UPPER),
                     (0, TEX_H - 18 + i * 6, TEX_W, 6))

    variants = []
    for i in range(SHADE_LEVELS):
        t = i / (SHADE_LEVELS - 1)
        v = tex.copy()
        overlay = pygame_module.Surface((TEX_W, TEX_H))
        overlay.fill(FOG)
        overlay.set_alpha(int(t * 255))
        v.blit(overlay, (0, 0))
        variants.append(v.convert())
    return variants


# ---------------------------------------------------------------------------
# Post-processing: bloom, vignette, film grain (numpy; degrades gracefully)
# ---------------------------------------------------------------------------

class PostFX:
    """Found-footage finish: fluorescent panels bloom, the frame carries a
    soft vignette that tightens with fear, and film grain sits over
    everything, heavier when he's scared."""

    def __init__(self, pygame_module, w, h):
        self.pg = pygame_module
        self.ok = False
        try:
            import numpy as np
        except ImportError:
            return
        self.np = np
        self.ok = True
        self.w, self.h = w, h
        self.sw, self.sh = max(2, w // 4), max(2, h // 4)

        # Multiplicative vignette: full brightness center, dimmer corners.
        yy, xx = np.mgrid[0:h, 0:w]
        r = np.hypot((xx - w / 2) / (w / 2), (yy - h / 2) / (h / 2))
        base = np.clip(255 - (r ** 2.2) * 70, 150, 255).astype(np.uint8)
        self.vignette = pygame_module.surfarray.make_surface(
            np.repeat(base.T[:, :, None], 3, axis=2)).convert()

        # Fear vignette: black with radial per-pixel alpha, scaled at blit.
        fear_a = np.clip((r - 0.35) * 260, 0, 255).astype(np.uint8)
        vf = pygame_module.Surface((w, h), pygame_module.SRCALPHA)
        alpha = pygame_module.surfarray.pixels_alpha(vf)
        alpha[:, :] = fear_a.T
        del alpha
        self.vignette_fear = vf

        # Film grain: multiplicative noise sheets, two intensities.
        rng = np.random.default_rng(7)
        self.grain = [self._grain_sheet(rng, 10) for _ in range(8)]
        self.grain_heavy = [self._grain_sheet(rng, 26) for _ in range(8)]

    def _grain_sheet(self, rng, depth):
        noise = 255 - rng.integers(0, depth, (self.w, self.h), dtype=self.np.uint8)
        return self.pg.surfarray.make_surface(
            self.np.repeat(noise[:, :, None], 3, axis=2)).convert()

    def apply(self, frame, fear, tick):
        if not self.ok:
            return
        np = self.np
        pg = self.pg

        # Bloom: downsample, keep only the bright end (light panels),
        # blur by upscaling, add back.
        small = pg.transform.smoothscale(frame, (self.sw, self.sh))
        arr = pg.surfarray.array3d(small).astype(np.int16)
        lum = arr.sum(axis=2)
        mask = (lum > 640)[:, :, None]
        bright = (arr * mask * 0.55).astype(np.uint8)
        glow = pg.transform.smoothscale(
            pg.surfarray.make_surface(bright), (self.w, self.h))
        frame.blit(glow, (0, 0), special_flags=pg.BLEND_RGB_ADD)

        frame.blit(self.vignette, (0, 0), special_flags=pg.BLEND_RGB_MULT)
        if fear > 0.05:
            self.vignette_fear.set_alpha(int(min(1.0, fear) * 150))
            frame.blit(self.vignette_fear, (0, 0))

        sheets = self.grain_heavy if fear > 0.5 else self.grain
        frame.blit(sheets[tick % len(sheets)], (0, 0),
                   special_flags=pg.BLEND_RGB_MULT)


# ---------------------------------------------------------------------------
# World
# ---------------------------------------------------------------------------

class World:
    """floor[y][x] / ceil[y][x] in world units. Solid wall: floor >= ceil.
    gx/gy hold per-cell floor slopes (ramps, raked floors). light scales
    colors (blackout zones, dying lights); panel marks a fluorescent tile.
    Coordinates wrap at the edges."""

    def __init__(self, seed: int, cols: int, rows: int):
        self.seed = seed
        rng = random.Random(seed ^ 0x5EED)
        cfg = bg.Config(width=cols * 8, height=rows * 8, cell_size=8,
                        **STYLE["gen"])
        grid = bg.generate(cfg, seed)

        open_cells = []
        self.rows, self.cols = len(grid) * 2, len(grid[0]) * 2
        R, C = self.rows, self.cols
        ceil_norm = STYLE["ceil_norm"]
        self.floor = [[0.0] * C for _ in range(R)]
        self.ceil = [[0.0] * C for _ in range(R)]
        self.gx = [[0.0] * C for _ in range(R)]
        self.gy = [[0.0] * C for _ in range(R)]
        self.light = [[1.0] * C for _ in range(R)]
        for y in range(R):
            for x in range(C):
                if grid[y // 2][x // 2] == bg.FLOOR:
                    self.ceil[y][x] = ceil_norm
                    open_cells.append((x, y))
        self.open_set = set(open_cells)

        self._add_tall_halls(rng)
        self._add_crawlspaces(rng)
        self._add_sunken_wings(rng)
        self._add_raked_floors(rng)
        self._add_pitfalls(rng)
        self._add_blackouts(rng)
        self._add_doorways(rng)
        self._place_panels(rng)

    # -- zone helpers -------------------------------------------------------

    def _blob(self, rng, size):
        if not self.open_set:
            return set()
        start = rng.choice(sorted(self.open_set))
        blob = {start}
        frontier = [start]
        while frontier and len(blob) < size:
            x, y = frontier[rng.randrange(len(frontier))]
            nbrs = [(x + dx, y + dy) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
                    if (x + dx, y + dy) in self.open_set and (x + dx, y + dy) not in blob]
            if not nbrs:
                frontier.remove((x, y))
                continue
            n = rng.choice(nbrs)
            blob.add(n)
            frontier.append(n)
        return blob

    def _add_tall_halls(self, rng):
        for _ in range(rng.randint(*STYLE["tall"])):
            h = rng.uniform(*STYLE["tall_h"])
            for x, y in self._blob(rng, rng.randint(150, 600)):
                if self.ceil[y][x] > 0:
                    self.ceil[y][x] = h

    def _add_crawlspaces(self, rng):
        lo, hi = STYLE["crawl"]
        for _ in range(rng.randint(lo, hi)):
            for x, y in self._blob(rng, rng.randint(80, 300)):
                if self.ceil[y][x] > 0:
                    self.ceil[y][x] = 0.45
                    self.light[y][x] = min(self.light[y][x], 0.7)

    def _add_sunken_wings(self, rng):
        """Lower wings: the floor descends ring by ring from the edge.
        Some wings keep hard 0.25-unit steps (stairs); others get smooth
        per-cell slopes (ramps — every wing in the parking garage)."""
        for _ in range(rng.randint(*STYLE["sunken"])):
            blob = self._blob(rng, rng.randint(150, 450))
            if not blob:
                continue
            edge = [c for c in blob
                    if any((c[0] + dx, c[1] + dy) not in blob
                           for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))]
            depth = {c: 0 for c in edge}
            queue = deque(edge)
            while queue:
                x, y = queue.popleft()
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    n = (x + dx, y + dy)
                    if n in blob and n not in depth:
                        depth[n] = depth[(x, y)] + 1
                        queue.append(n)
            for (x, y), d in depth.items():
                if self.ceil[y][x] > 0:
                    self.floor[y][x] = -min(0.25 * d, 1.0)
            if rng.random() < STYLE["ramp_chance"]:
                self._smooth_ramps(blob)

    def _smooth_ramps(self, blob):
        """Turn a stepped wing into continuous slopes: per-cell gradients
        from neighboring floor heights (central differences)."""
        R, C = self.rows, self.cols
        for x, y in blob:
            if self.ceil[y][x] <= 0:
                continue
            f0 = self.floor[y][x]

            def fl(nx, ny):
                nx %= C
                ny %= R
                if self.floor[ny][nx] < self.ceil[ny][nx]:
                    return self.floor[ny][nx]
                return f0

            gx = (fl(x + 1, y) - fl(x - 1, y)) * 0.5
            gy = (fl(x, y + 1) - fl(x, y - 1)) * 0.5
            self.gx[y][x] = max(-0.28, min(0.28, gx))
            self.gy[y][x] = max(-0.28, min(0.28, gy))

    def _add_raked_floors(self, rng):
        """Kane Pixels set design: subtly tilted floors. A whole area leans
        a few degrees in one direction — barely enough to notice, exactly
        enough to be wrong."""
        for _ in range(rng.randint(*STYLE["raked"])):
            blob = self._blob(rng, rng.randint(80, 250))
            if not blob:
                continue
            cx = sum(c[0] for c in blob) / len(blob)
            cy = sum(c[1] for c in blob) / len(blob)
            angle = rng.uniform(0, math.tau)
            slope = rng.uniform(0.05, 0.10)
            sx, sy = math.cos(angle) * slope, math.sin(angle) * slope
            for x, y in blob:
                if self.ceil[y][x] <= 0 or self.floor[y][x] != 0.0:
                    continue
                off = sx * (x - cx) + sy * (y - cy)
                self.floor[y][x] = max(-0.35, min(0.35, off))
                if abs(off) < 0.35:
                    self.gx[y][x] = sx
                    self.gy[y][x] = sy

    def _add_pitfalls(self, rng):
        for _ in range(rng.randint(*STYLE["pits"])):
            blob = self._blob(rng, rng.randint(120, 350))
            for x, y in blob:
                if self.ceil[y][x] > 0 and x % 3 != 0 and y % 3 != 0:
                    if self.floor[y][x] == 0.0:
                        self.floor[y][x] = PIT_FLOOR

    def _add_blackouts(self, rng):
        for _ in range(rng.randint(*STYLE["blackouts"])):
            for x, y in self._blob(rng, rng.randint(100, 300)):
                self.light[y][x] = 0.3

    def _add_doorways(self, rng):
        """Punch door-height lintels into wall gaps so the segmented rooms
        read as rooms with doorways, not just missing wall."""
        R, C = self.rows, self.cols
        DOOR_H = 0.82

        def open_norm(x, y):
            x %= C
            y %= R
            return (self.floor[y][x] == 0.0 and 0.9 < self.ceil[y][x] <= 1.3
                    and not self.gx[y][x] and not self.gy[y][x])

        lintels = []
        for y in range(1, R - 1):
            for x in range(1, C - 1):
                if not open_norm(x, y):
                    continue
                L, Rt = self.solid(x - 1, y), self.solid(x + 1, y)
                U, D = self.solid(x, y - 1), self.solid(x, y + 1)
                # single-width gap in a wall line
                if (L and Rt and not U and not D) or (U and D and not L and not Rt):
                    if rng.random() < 0.6:
                        lintels.append((x, y))
                # double-width gap
                elif (L and not Rt and open_norm(x + 1, y) and self.solid(x + 2, y)
                        and not U and not D and rng.random() < 0.45):
                    lintels += [(x, y), (x + 1, y)]
                elif (U and not D and open_norm(x, y + 1) and self.solid(x, y + 2)
                        and not L and not Rt and rng.random() < 0.45):
                    lintels += [(x, y), (x, y + 1)]
        for x, y in lintels:
            self.ceil[y][x] = DOOR_H

    def _place_panels(self, rng):
        self.panel = [[False] * self.cols for _ in range(self.rows)]
        pattern = STYLE["panel"]
        prob = STYLE["panel_prob"]
        for y in range(self.rows):
            for x in range(self.cols):
                if (self.ceil[y][x] > 0.9 and pattern(x, y)
                        and self.light[y][x] > 0.5 and rng.random() < prob):
                    self.panel[y][x] = True

    # -- queries ------------------------------------------------------------

    def solid(self, x: int, y: int) -> bool:
        x %= self.cols
        y %= self.rows
        return self.floor[y][x] >= self.ceil[y][x]

    def cell(self, x: int, y: int):
        x %= self.cols
        y %= self.rows
        return self.floor[y][x], self.ceil[y][x], self.light[y][x], self.panel[y][x]

    def floor_at(self, px: float, py: float) -> float:
        """Floor height at an exact point, honoring ramp slopes."""
        xi = int(px) % self.cols
        yi = int(py) % self.rows
        base = self.floor[yi][xi]
        gx = self.gx[yi][xi]
        gy = self.gy[yi][xi]
        if gx or gy:
            base += gx * (px - math.floor(px) - 0.5) + gy * (py - math.floor(py) - 0.5)
        return base

    def passable(self, from_z: float, x: int, y: int, max_drop: float | None) -> bool:
        f, c, _, _ = self.cell(x, y)
        if f >= c:
            return False
        if f - from_z > STEP_UP:
            return False
        if c - max(f, from_z) < 0.42:
            return False
        if max_drop is not None and from_z - f > max_drop:
            return False
        return True

    def edge_ok(self, a, b) -> bool:
        fa, ca, _, _ = self.cell(*a)
        fb, cb, _, _ = self.cell(*b)
        if fb >= cb:
            return False
        if fb - fa > STEP_UP or fa - fb > WALKER_MAX_DROP:
            return False
        if cb - max(fa, fb) < 0.42 or ca - max(fa, fb) < 0.42:
            return False
        return True

    def wrap_delta(self, ax, ay, bx, by):
        dx = (bx - ax + self.cols / 2) % self.cols - self.cols / 2
        dy = (by - ay + self.rows / 2) % self.rows - self.rows / 2
        return dx, dy

    def peripheral_shift(self, px, py, rng):
        for _ in range(20):
            x, y = rng.randrange(self.cols), rng.randrange(self.rows)
            if math.hypot(*self.wrap_delta(px, py, x, y)) > SHIFT_SAFE_RADIUS:
                break
        else:
            return
        if rng.random() < 0.7:
            for _ in range(rng.randint(20, 70)):
                if self.solid(x, y):
                    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        f, c, l, _ = self.cell(x + dx, y + dy)
                        if f < c:
                            self.floor[y][x], self.ceil[y][x] = f, c
                            self.light[y][x] = l
                            break
                    else:
                        self.floor[y][x] = 0.0
                        self.ceil[y][x] = STYLE["ceil_norm"]
                x = (x + rng.choice((1, -1, 0, 0))) % self.cols
                y = (y + rng.choice((0, 0, 1, -1))) % self.rows
                if math.hypot(*self.wrap_delta(px, py, x, y)) <= SHIFT_SAFE_RADIUS:
                    return
        else:
            for _ in range(rng.randint(3, 10)):
                wx = (x + rng.randint(-4, 4)) % self.cols
                wy = (y + rng.randint(-4, 4)) % self.rows
                if (math.hypot(*self.wrap_delta(px, py, wx, wy)) > SHIFT_SAFE_RADIUS
                        and self.floor[wy][wx] == 0.0):
                    self.ceil[wy][wx] = 0.0


# ---------------------------------------------------------------------------
# Player
# ---------------------------------------------------------------------------

class Player:
    def __init__(self, x, y, angle):
        self.x, self.y, self.angle = x, y, angle
        self.z = 0.0
        self.vz = 0.0
        self.vx = self.vy = 0.0
        self.want_vx = self.want_vy = 0.0
        self.eye = EYE_STAND
        self.bob_phase = 0.0
        self.bob = 0.0
        self.sway = 0.0
        self.fell = False
        self.fear = 0.0          # 0 calm .. 1 terror; spikes fast, decays slow
        self.exertion = 0.0      # builds while running, recovers while walking
        self.crouch_hint = False  # controller sees a low ceiling coming up
        self.running = False
        self.presence_bearing = None   # perception of the thing, if any
        self.presence_dist = None
        self.presence_seen = False
        self.presence_heard = False

    def eye_z(self):
        return self.z + self.eye + self.bob

    def _clear(self, world, x, y, max_drop):
        r = PLAYER_RADIUS
        return all(world.passable(self.z, int(x + ox) % world.cols,
                                  int(y + oy) % world.rows, max_drop)
                   for ox in (-r, r) for oy in (-r, r))

    def _eye_target(self, world):
        f, c, _, _ = world.cell(int(self.x), int(self.y))
        clearance = c - f
        if clearance <= 0.75:
            return max(0.22, clearance - 0.15)
        if self.crouch_hint:
            return 0.36          # pre-ducking before the crawlspace
        return EYE_STAND

    def apply(self, world: World, dt, max_drop):
        # Terrain and effort shape the pace: uphill and stair-climbs are
        # slower, downhill a touch faster, exhaustion drags, and moving
        # while mid-crouch is slow going.
        want_x, want_y = self.want_vx, self.want_vy
        want_speed = math.hypot(want_x, want_y)
        eye_target = self._eye_target(world)
        if want_speed > 1e-4:
            mult = 1.0
            xi, yi = int(self.x) % world.cols, int(self.y) % world.rows
            gx, gy = world.gx[yi][xi], world.gy[yi][xi]
            if gx or gy:
                uphill = (want_x * gx + want_y * gy) / want_speed
                if uphill > 0:
                    mult /= 1.0 + 2.8 * uphill
                else:
                    mult *= 1.0 + min(0.15, -0.3 * uphill)
            if self.z < world.floor_at(self.x, self.y) - 0.02:
                mult *= 0.72                     # hauling up a stair riser
            if self.exertion > 0.7:
                mult *= 1.0 - 0.25 * (self.exertion - 0.7) / 0.3
            if abs(self.eye - eye_target) > 0.06:
                mult *= 0.75                     # ducking down / rising up
            want_x *= mult
            want_y *= mult

        k = min(1.0, ACCEL * dt)
        self.vx += (want_x - self.vx) * k
        self.vy += (want_y - self.vy) * k

        dx, dy = self.vx * dt, self.vy * dt
        if self._clear(world, self.x + dx, self.y, max_drop):
            self.x = (self.x + dx) % world.cols
        else:
            self.vx = 0.0
        if self._clear(world, self.x, self.y + dy, max_drop):
            self.y = (self.y + dy) % world.rows
        else:
            self.vy = 0.0

        speed = math.hypot(self.vx, self.vy)
        if speed > MOVE_SPEED * 1.25:
            self.exertion = min(1.0, self.exertion + dt / 7.0)
        else:
            self.exertion = max(0.0, self.exertion - dt / 11.0)

        # Vertical: slopes are followed directly, gravity handles drops.
        f = world.floor_at(self.x, self.y)
        if self.z > f + 0.03:
            self.vz -= GRAVITY * dt
            self.z = max(f, self.z + self.vz * dt)
            if self.z <= f:
                self.vz = 0.0
        elif self.z < f:
            self.z = min(f, self.z + 5.0 * dt)
        else:
            self.z = f
        if self.z <= FALL_LIMIT:
            self.fell = True

        # Crouching is deliberate: over a second to fold down, slower to
        # trust standing back up — unless he's running for it.
        if eye_target < self.eye:
            rate = 4.5 if self.running else 2.1
        else:
            rate = 1.5
        self.eye += (eye_target - self.eye) * min(1.0, rate * dt)

        # Stride-synced bob; running lengthens and deepens it. A slight
        # lateral sway at half the stride rate keeps the head organic.
        ratio = min(1.5, speed / MOVE_SPEED)
        self.bob_phase += dt * math.tau * BOB_STRIDE_HZ * (0.4 + 0.75 * min(ratio, 1.0))
        amp = BOB_AMPLITUDE * (1.0 + 0.55 * max(0.0, ratio - 1.0))
        self.bob = math.sin(self.bob_phase) * amp * min(ratio, 1.0)
        self.sway = math.sin(self.bob_phase * 0.5) * 0.009 * min(ratio, 1.0)

    def crouched(self):
        return self.eye < EYE_STAND - 0.1


# ---------------------------------------------------------------------------
# Auto-walker: BFS route planning + carrot-point pursuit
# ---------------------------------------------------------------------------

class AutoWalker:
    BFS_LIMIT = 1600
    CARROT_RADIUS = 1.0

    def __init__(self, rng):
        self.rng = rng
        self.path: list[tuple[int, int]] = []
        self.idx = 0
        self.explore_angle = rng.uniform(0, math.tau)
        self.repath_timer = 0.0
        self.pause = 0.0
        self.pause_turn = 0.0
        self.panic = False
        self.look = 0.0          # >0: turning to look toward the thing
        self.look_cd = 0.0

    def plan(self, world: World, p: Player, flee_from=None):
        start = (int(p.x) % world.cols, int(p.y) % world.rows)
        prev = {start: None}
        order = []
        queue = deque([start])
        while queue and len(order) < self.BFS_LIMIT:
            cur = queue.popleft()
            order.append(cur)
            cx, cy = cur
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                n = ((cx + dx) % world.cols, (cy + dy) % world.rows)
                if n not in prev and world.edge_ok(cur, n):
                    prev[n] = cur
                    queue.append(n)

        far = order[len(order) // 2:]
        if not far:
            self.path = []
            return

        if flee_from is not None:
            fx, fy = flee_from

            def score(c):
                dx, dy = world.wrap_delta(fx, fy, c[0] + 0.5, c[1] + 0.5)
                away = math.hypot(dx, dy)
                dpx, dpy = world.wrap_delta(p.x, p.y, c[0] + 0.5, c[1] + 0.5)
                return away * 2.0 + math.hypot(dpx, dpy) * 0.3 + self.rng.uniform(0.0, 2.0)
        else:
            def score(c):
                dx, dy = world.wrap_delta(p.x, p.y, c[0] + 0.5, c[1] + 0.5)
                d = math.hypot(dx, dy)
                if d < 1e-6:
                    return -1.0
                align = math.cos(math.atan2(dy, dx) - self.explore_angle)
                return d * (1.0 + 0.9 * align) + self.rng.uniform(0.0, 4.0)

        goal = max(far, key=score)
        path = []
        node = goal
        while node is not None:
            path.append(node)
            node = prev[node]
        path.reverse()
        self.path = path
        self.idx = min(1, len(path) - 1)
        self.repath_timer = 14.0
        self.explore_angle += self.rng.uniform(-0.7, 0.7)

    def update(self, world: World, p: Player, dt, presence=None):
        # Panic with hysteresis: something got too close. Run, and keep
        # running until the fear has genuinely faded.
        was_panic = self.panic
        if p.fear > 0.55:
            self.panic = True
        elif p.fear < 0.30:
            self.panic = False
        p.running = self.panic
        if self.panic and not was_panic:
            self.pause = 0.0
            self.repath_timer = 0.0     # drop everything, plan an escape

        if self.pause > 0:
            self.pause -= dt
            p.want_vx = p.want_vy = 0.0
            p.angle += self.pause_turn * dt
            return

        # Looking is how he knows. Hearing something behind him makes him
        # stop and turn to check; mid-flight he throws glances over his
        # shoulder every few seconds without breaking stride.
        self.look_cd -= dt
        if self.look <= 0 and p.presence_bearing is not None and self.look_cd <= 0:
            if self.panic:
                self.look = 0.45
                self.look_cd = self.rng.uniform(3.5, 7.0)
            elif p.presence_heard and not p.presence_seen:
                self.look = 0.9
                self.look_cd = self.rng.uniform(6.0, 12.0)
        if self.look > 0 and p.presence_bearing is not None:
            self.look -= dt
            diff = (p.presence_bearing - p.angle + math.pi) % math.tau - math.pi
            turn = TURN_SPEED * 1.8 * dt
            p.angle += max(-turn, min(turn, diff))
            if self.panic and self.path and self.idx < len(self.path):
                # keep running the route while the head is turned
                cx, cy = self.path[self.idx]
                dx, dy = world.wrap_delta(p.x, p.y, cx + 0.5, cy + 0.5)
                d = math.hypot(dx, dy) or 1.0
                speed = MOVE_SPEED * 1.35
                p.want_vx = dx / d * speed
                p.want_vy = dy / d * speed
            else:
                p.want_vx = p.want_vy = 0.0      # stop. listen. look.
            return

        flee = None
        if self.panic and presence is not None:
            flee = (presence.x, presence.y)

        self.repath_timer -= dt
        if self.repath_timer <= 0 or self.idx >= len(self.path):
            self.plan(world, p, flee)
            if self.panic:
                self.repath_timer = 3.5
            if not self.path:
                p.want_vx = p.want_vy = 0.0
                p.angle += TURN_SPEED * 0.4 * dt
                return

        nxt = self.path[self.idx]
        if not world.passable(p.z, nxt[0], nxt[1], WALKER_MAX_DROP):
            self.plan(world, p, flee)
            if not self.path:
                return

        while self.idx < len(self.path) - 1:
            cx, cy = self.path[self.idx]
            dx, dy = world.wrap_delta(p.x, p.y, cx + 0.5, cy + 0.5)
            if math.hypot(dx, dy) < self.CARROT_RADIUS:
                self.idx += 1
            else:
                break

        # See the crawlspace coming: start folding down before the door.
        def low_ahead(i):
            f, c, _, _ = world.cell(*self.path[i])
            return c - f < 0.8

        p.crouch_hint = any(low_ahead(i) for i in
                            range(self.idx, min(self.idx + 3, len(self.path))))

        cx, cy = self.path[self.idx]
        dx, dy = world.wrap_delta(p.x, p.y, cx + 0.5, cy + 0.5)
        dist = math.hypot(dx, dy)

        if self.idx >= len(self.path) - 1 and dist < 0.5:
            self.path = []
            if not self.panic and p.fear < 0.3 and self.rng.random() < 0.5:
                self.pause = self.rng.uniform(0.7, 1.8)
                self.pause_turn = self.rng.choice((-1, 1)) * TURN_SPEED * 0.35
            return

        target = math.atan2(dy, dx)
        diff = (target - p.angle + math.pi) % math.tau - math.pi
        turn = TURN_SPEED * (1.6 if self.panic else 1.0)
        p.angle += max(-turn * dt, min(turn * dt, diff))

        alignment = math.cos(diff)
        if self.panic:
            speed = MOVE_SPEED * 1.8            # flat-out run
        else:
            # Calm is a stroll; unease quickens the step.
            speed = MOVE_SPEED * (0.78 + 0.35 * p.fear)
        if p.crouched():
            speed = min(speed, MOVE_SPEED * 0.55)
        speed *= max(0.0, alignment) if abs(diff) < 1.5 else 0.0
        p.want_vx = math.cos(p.angle) * speed
        p.want_vy = math.sin(p.angle) * speed


def line_of_sight(world: World, ax, ay, bx, by) -> bool:
    dx, dy = world.wrap_delta(ax, ay, bx, by)
    steps = int(math.hypot(dx, dy) * 3) + 1
    for i in range(1, steps):
        t = i / steps
        if world.solid(int(ax + dx * t), int(ay + dy * t)):
            return False
    return True


class Presence:
    """Something is always somewhere in the level, and it is always walking
    toward you. It never runs. You hear it before you see it; you see it as
    a dark figure at the end of a corridor; and if it reaches you, the
    lights go out and you are somewhere else. It was never confirmed.

    Faster than a stroll, slower than a sprint — you can outrun it, but
    you cannot make it stop."""

    SPEED = 1.05
    STRIDE = 0.62

    def __init__(self, world: World, p: Player, rng, ahead=False):
        self.rng = rng
        self.path = []
        self.idx = 0
        self.replan = 0.0
        self.stride = 0.0
        self.lost = 0.0
        self.x, self.y = self._pick_spot(world, p, ahead)

    def _pick_spot(self, world, p, ahead):
        if ahead:
            # For demos: place it down the current sightline.
            for d in range(16, 8, -1):
                x = int(p.x + math.cos(p.angle) * d) % world.cols
                y = int(p.y + math.sin(p.angle) * d) % world.rows
                if not world.solid(x, y):
                    return x + 0.5, y + 0.5
        cells = sorted(world.open_set)
        for _ in range(200):
            x, y = self.rng.choice(cells)
            d = math.hypot(*world.wrap_delta(p.x, p.y, x + 0.5, y + 0.5))
            if 25 < d < 45 and world.floor[y % world.rows][x % world.cols] > -1.5:
                return x + 0.5, y + 0.5
        x, y = self.rng.choice(cells)
        return x + 0.5, y + 0.5

    def relocate(self, world, p):
        self.x, self.y = self._pick_spot(world, p, ahead=False)
        self.path = []
        self.lost = 0.0

    def _plan(self, world: World, p: Player):
        start = (int(self.x) % world.cols, int(self.y) % world.rows)
        goal = (int(p.x) % world.cols, int(p.y) % world.rows)
        prev = {start: None}
        queue = deque([start])
        found = start == goal
        n = 0
        while queue and n < 2500:
            cur = queue.popleft()
            n += 1
            if cur == goal:
                found = True
                break
            cx, cy = cur
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nb = ((cx + dx) % world.cols, (cy + dy) % world.rows)
                if nb not in prev and world.edge_ok(cur, nb):
                    prev[nb] = cur
                    queue.append(nb)
        self.replan = 2.5
        if not found:
            self.path = []
            return
        path = []
        node = goal
        while node is not None:
            path.append(node)
            node = prev[node]
        path.reverse()
        self.path = path
        self.idx = min(1, len(path) - 1)

    def update(self, world: World, p: Player, dt, audio) -> bool:
        """Walk toward the player; drive fear and footstep audio.
        Returns True the moment it reaches him."""
        self.replan -= dt
        if self.replan <= 0:
            self._plan(world, p)
        if not self.path:
            # Sealed off by the Shift: it does not teleport. It waits.
            # Only if it stays walled off for a long time does it turn up
            # somewhere else — it always finds another way, eventually.
            self.lost += dt
            if self.lost > 60.0:
                self.relocate(world, p)
        else:
            self.lost = 0.0
            while self.idx < len(self.path) - 1:
                cx, cy = self.path[self.idx]
                dx, dy = world.wrap_delta(self.x, self.y, cx + 0.5, cy + 0.5)
                if math.hypot(dx, dy) < 0.6:
                    self.idx += 1
                else:
                    break
            cx, cy = self.path[self.idx]
            dx, dy = world.wrap_delta(self.x, self.y, cx + 0.5, cy + 0.5)
            d = math.hypot(dx, dy)
            if d > 1e-6:
                step = min(self.SPEED * dt, d)
                self.x = (self.x + dx / d * step) % world.cols
                self.y = (self.y + dy / d * step) % world.rows
            self.stride -= dt
            if self.stride <= 0:
                self.stride = self.STRIDE * self.rng.uniform(0.95, 1.05)
                if audio:
                    pdx, pdy = world.wrap_delta(p.x, p.y, self.x, self.y)
                    audio.presence_step(math.atan2(pdy, pdx),
                                        math.hypot(pdx, pdy), p)

        # Perception, not radar: he only fears what he can see or hear.
        # Seeing it requires line of sight AND facing it. Hearing it
        # (footsteps close by) unnerves him — enough to make him turn
        # around and look. Confirmation is what breaks him into a run.
        pdx, pdy = world.wrap_delta(p.x, p.y, self.x, self.y)
        dist = math.hypot(pdx, pdy)
        bearing = math.atan2(pdy, pdx)
        rel = (bearing - p.angle + math.pi) % math.tau - math.pi
        seen = (dist < 20.0 and abs(rel) < 1.0
                and line_of_sight(world, p.x, p.y, self.x, self.y))
        heard = dist < 13.0
        threat = max(0.0, 1.0 - dist / 16.0)
        if seen:
            target = threat
        elif heard:
            target = threat * (0.85 if dist < 5.0 else 0.5)
        else:
            target = 0.0
        xi, yi = int(p.x) % world.cols, int(p.y) % world.rows
        if world.light[yi][xi] < 0.4:
            target = max(target, 0.15)     # the dark is never comfortable
        if target > p.fear:
            p.fear += (target - p.fear) * min(1.0, 2.2 * dt)
        else:
            p.fear += (target - p.fear) * min(1.0, 0.12 * dt)

        p.presence_bearing = bearing
        p.presence_dist = dist
        p.presence_seen = seen
        p.presence_heard = heard

        return dist < 1.25


# ---------------------------------------------------------------------------
# Ambience: synthesized audio (stereo) + dying-light events
# ---------------------------------------------------------------------------

class Audio:
    """All sounds are synthesized: ballast hum, distant footsteps that pan
    and approach/recede, a clunk-whine for dying lights, water drips.
    The mixer is force-reinitialized to a known format — pygame.init()
    leaves it at 44.1 kHz stereo, which turns raw 22 kHz mono buffers
    into the infamous nonstop beep."""

    def __init__(self, pygame_module, rng, enabled=True):
        self.pg = pygame_module
        self.rng = rng
        self.ok = False
        if not enabled:
            return
        try:
            pygame_module.mixer.quit()
            pygame_module.mixer.init(frequency=SAMPLE_RATE, size=-16,
                                     channels=2, buffer=512)
            if pygame_module.mixer.get_init() is None:
                return
        except Exception:
            return
        self.ok = True
        self.hum = self._sound(self._synth_hum(STYLE["hum_freq"]))
        self.pstep = self._sound(self._synth_presence_step())
        self.zap = self._sound(self._synth_lightout())
        self.drip = self._sound(self._synth_drip())
        self.breath_calm = self._sound(self._synth_breath(False))
        self.breath_heavy = self._sound(self._synth_breath(True))
        self.heart = self._sound(self._synth_heartbeat())
        self.knock = self._sound(self._synth_knock())
        self.scrape = self._sound(self._synth_scrape())
        self.swell = self._sound(self._synth_swell())
        self.hum_ch = self.hum.play(loops=-1)
        self.hum_vol = 0.10
        self.hum_target = 0.10
        if self.hum_ch:
            self.hum_ch.set_volume(self.hum_vol, self.hum_vol)
        self.drip_timer = rng.uniform(5.0, 14.0)
        self.breath_timer = 2.0
        self.heart_timer = 1.0
        self.danger_timer = rng.uniform(10.0, 22.0)
        self.presence_dist = None
        self.presence_bearing = 0.0

    # -- synthesis ----------------------------------------------------------

    def _sound(self, mono):
        buf = array("h")
        for v in mono:
            s = int(max(-1.0, min(1.0, v)) * 16000)
            buf.append(s)
            buf.append(s)
        return self.pg.mixer.Sound(buffer=buf.tobytes())

    def _synth_hum(self, freq):
        """Fluorescent ballast buzz: harmonic stack + low-passed noise with
        a slow wobble, built as a seamless 2-second loop."""
        rng = random.Random(0)
        out = []
        n = 0.0
        dur = SAMPLE_RATE * 2
        for i in range(dur):
            t = i / SAMPLE_RATE
            v = 0.0
            for k, a in ((1, .26), (2, .17), (3, .13), (4, .08), (6, .05), (8, .04)):
                v += math.sin(math.tau * freq * k * t + k * 1.7) * a
            v *= 0.75 + 0.25 * math.sin(math.tau * 1.5 * t)   # 3 whole cycles
            n = n * 0.82 + rng.uniform(-1, 1) * 0.18          # one-pole lowpass
            out.append(v * 0.5 + n * 0.35)
        return out

    def _synth_presence_step(self):
        """A heavier tread than yours: low, unhurried, with a drag."""
        rng = random.Random(1)
        out = []
        for i in range(int(SAMPLE_RATE * 0.24)):
            t = i / SAMPLE_RATE
            thud = math.sin(math.tau * (68 - 90 * t) * t) * math.exp(-t * 20)
            drag = rng.uniform(-1, 1) * 0.25 * math.exp(-t * 12)
            out.append(thud * 0.9 + drag)
        return out

    def _synth_breath(self, heavy):
        """One breath cycle of band-limited noise: inhale swell, exhale."""
        rng = random.Random(3 if heavy else 4)
        out = []
        n = 0.0
        dur = 1.0 if heavy else 1.5
        for i in range(int(SAMPLE_RATE * dur)):
            t = i / SAMPLE_RATE
            n = n * 0.9 + rng.uniform(-1, 1) * 0.1
            if heavy:
                env = (math.exp(-((t - 0.18) / 0.12) ** 2) * 0.9
                       + math.exp(-((t - 0.62) / 0.16) ** 2) * 0.7)
            else:
                env = (math.exp(-((t - 0.30) / 0.18) ** 2) * 0.5
                       + math.exp(-((t - 0.95) / 0.22) ** 2) * 0.35)
            out.append(n * env)
        return out

    def _synth_heartbeat(self):
        """Lub-dub, felt more than heard."""
        out = []
        for i in range(int(SAMPLE_RATE * 0.4)):
            t = i / SAMPLE_RATE
            v = math.sin(math.tau * 52 * t) * math.exp(-t * 30)
            if t >= 0.14:
                te = t - 0.14
                v += math.sin(math.tau * 46 * te) * math.exp(-te * 30) * 0.6
            out.append(v)
        return out

    def _synth_lightout(self):
        """A fluorescent bank dying: electrical sputter bursts, then a thin
        whine ringing down into nothing. No bass — nothing thuds."""
        rng = random.Random(2)
        out = []
        n = 0.0
        bursts = ((0.00, 0.05), (0.10, 0.04), (0.19, 0.07))
        for i in range(int(SAMPLE_RATE * 1.1)):
            t = i / SAMPLE_RATE
            v = 0.0
            # sputter: buzzy noise gated into short bursts
            for b0, blen in bursts:
                if b0 <= t < b0 + blen:
                    n = n * 0.6 + rng.uniform(-1, 1) * 0.4
                    gate = 0.75 + 0.25 * math.sin(math.tau * 120 * t)
                    v += n * gate * 0.8
            # the tube's whine ringing down after the last burst
            if t > 0.26:
                te = t - 0.26
                f = 2600 * math.exp(-te * 5.5) + 320
                v += math.sin(math.tau * f * te) * math.exp(-te * 4.5) * 0.22
            out.append(v)
        return out

    def _synth_knock(self):
        """Three raps on something hollow, a few rooms away."""
        out = []
        hits = ((0.0, 1.0), (0.27, 0.75), (0.50, 0.9))
        for i in range(int(SAMPLE_RATE * 0.95)):
            t = i / SAMPLE_RATE
            v = 0.0
            for t0, a in hits:
                if t >= t0:
                    te = t - t0
                    v += a * (math.sin(math.tau * 185 * te) * 0.7
                              + math.sin(math.tau * 310 * te) * 0.3) * math.exp(-te * 38)
            out.append(v * 0.85)
        return out

    def _synth_scrape(self):
        """Something heavy dragged over carpet: resonated noise, sweeping."""
        rng = random.Random(5)
        out = []
        y1 = y2 = 0.0
        r = 0.985
        dur = 1.3
        for i in range(int(SAMPLE_RATE * dur)):
            t = i / SAMPLE_RATE
            f = 350 + 600 * (t / dur)
            w = math.tau * f / SAMPLE_RATE
            env = math.sin(math.pi * t / dur) ** 1.5
            x = rng.uniform(-1, 1) * env * 0.08
            y = 2 * r * math.cos(w) * y1 - r * r * y2 + x
            y2, y1 = y1, y
            out.append(max(-1.0, min(1.0, y)))
        return out

    def _synth_swell(self):
        """A low room-tone swell, like the building leaning in."""
        rng = random.Random(6)
        out = []
        n = 0.0
        dur = 2.8
        for i in range(int(SAMPLE_RATE * dur)):
            t = i / SAMPLE_RATE
            env = math.sin(math.pi * t / dur) ** 2
            n = n * 0.94 + rng.uniform(-1, 1) * 0.06
            v = (math.sin(math.tau * 54 * t) + math.sin(math.tau * 67 * t) * 0.7
                 + math.sin(math.tau * 41 * t) * 0.5) * 0.22 + n * 0.5
            out.append(v * env)
        return out

    def _synth_drip(self):
        """Water drip with a faint echo, for the parking garage."""
        out = []
        for i in range(int(SAMPLE_RATE * 0.35)):
            t = i / SAMPLE_RATE
            f = 1750 * math.exp(-t * 6) + 350
            v = math.sin(math.tau * f * t) * math.exp(-t * 26)
            if t >= 0.16:
                te = t - 0.16
                fe = 1750 * math.exp(-te * 6) + 350
                v += math.sin(math.tau * fe * te) * math.exp(-te * 26) * 0.35
            out.append(v)
        return out

    # -- playback -----------------------------------------------------------

    def _pan_play(self, sound, direction, p, vol):
        ch = sound.play()
        if ch:
            rel = direction - p.angle
            r = 0.5 * (1.0 + math.sin(rel))
            ch.set_volume(vol * (1.0 - r), vol * r)

    def play_lightout(self, direction, p):
        if self.ok:
            self._pan_play(self.zap, direction, p, 0.6)

    def presence_step(self, direction, dist, p):
        """A real footstep from a real position: volume falls off with
        distance (gently — it should carry), panned to where it is."""
        if not self.ok:
            return
        vol = min(0.55, 2.6 / max(dist, 1.5) ** 0.85)
        if vol > 0.02:
            self._pan_play(self.pstep, direction, p, vol)

    def set_presence(self, dist, bearing):
        self.presence_dist = dist
        self.presence_bearing = bearing

    def set_hum_proximity(self, panel_dist, brightness):
        """The hum belongs to the lights: loud under a live panel, faint in
        blackouts, dipping when the lights flicker."""
        near = max(0.0, 1.0 - panel_dist / 10.0)
        self.hum_target = (0.03 + 0.30 * near) * brightness

    def update(self, dt, p: Player):
        """Breath and heartbeat follow fear and exertion; drips are just
        the garage being the garage."""
        if not self.ok:
            return
        if self.hum_ch:
            self.hum_vol += (self.hum_target - self.hum_vol) * min(1.0, 3.0 * dt)
            self.hum_ch.set_volume(self.hum_vol, self.hum_vol)
        arousal = max(p.fear, p.exertion * 0.85)

        self.breath_timer -= dt
        if self.breath_timer <= 0:
            self.breath_timer = 4.2 - 2.9 * arousal
            sound = self.breath_heavy if arousal > 0.5 else self.breath_calm
            ch = sound.play()
            if ch:
                vol = 0.05 + 0.28 * arousal
                ch.set_volume(vol, vol)

        self.heart_timer -= dt
        if self.heart_timer <= 0:
            bpm = 58 + 80 * p.fear + 28 * p.exertion
            self.heart_timer = 60.0 / bpm
            ch = self.heart.play()
            if ch:
                vol = 0.04 + 0.24 * p.fear      # quiet; felt, not heard
                ch.set_volume(vol, vol)

        if STYLE["drips"]:
            self.drip_timer -= dt
            if self.drip_timer <= 0:
                self.drip_timer = self.rng.uniform(4.0, 13.0)
                self._pan_play(self.drip, self.rng.uniform(0, math.tau), p,
                               self.rng.uniform(0.10, 0.28))

        # Distant danger: knocks, drags, the building leaning in. The
        # closer it is, the more frantic the soundscape gets — and it all
        # comes from its actual direction.
        if self.presence_dist is not None and self.presence_dist < 26.0:
            d = self.presence_dist
            self.danger_timer -= dt * (1.6 if d < 8.0 else 1.0)
            if self.danger_timer <= 0:
                pool = [self.knock]
                if d < 14.0:
                    pool += [self.scrape, self.scrape]
                if d < 18.0:
                    pool.append(self.swell)
                vol = min(0.5, 2.2 / max(d, 2.0) ** 0.8)
                jitter = self.rng.uniform(-0.4, 0.4)
                self._pan_play(self.rng.choice(pool),
                               self.presence_bearing + jitter, p, vol)
                self.danger_timer = (max(4.0, min(26.0, 4.0 + d * 0.7))
                                     * self.rng.uniform(0.6, 1.4))


class LightsOut:
    """A bank of lights starts to misbehave: a long, irregular sputter —
    stuttering dips with uneasy pauses — while you wonder whether it will
    die at all. Usually it steadies itself. Rarely, it doesn't."""

    DIE_CHANCE = 0.35

    def __init__(self, rng, record=False):
        self.rng = rng
        self.record = record
        self.timer = 3.0 if record else rng.uniform(40.0, 110.0)
        self.state = None       # None | 'tease' | 'dying' | 'dead' | 'recover'
        self.t = 0.0
        self.saved = {}         # (x, y) -> (light, panel)
        self.sputter = 0.0      # time until the current dip/hold flips
        self.dipped = False

    def _trigger(self, world: World, p: Player):
        cx = p.x + math.cos(p.angle) * self.rng.uniform(5, 11)
        cy = p.y + math.sin(p.angle) * self.rng.uniform(5, 11)
        radius = self.rng.uniform(3.0, 6.0)
        cells = {}
        for y in range(int(cy - radius), int(cy + radius) + 1):
            for x in range(int(cx - radius), int(cx + radius) + 1):
                if math.hypot(x + 0.5 - cx, y + 0.5 - cy) > radius:
                    continue
                xi, yi = x % world.cols, y % world.rows
                if (world.floor[yi][xi] < world.ceil[yi][xi]
                        and world.light[yi][xi] > 0.45):
                    cells[(xi, yi)] = (world.light[yi][xi], world.panel[yi][xi])
        if len(cells) < 6:
            self.timer = 5.0
            return
        self.saved = cells
        self.state = "tease"
        self.t = self.rng.uniform(1.8, 4.5)
        self.sputter = 0.0
        self.bearing = math.atan2(cy - p.y, cx - p.x)

    def _set(self, world, level):
        for (x, y), (orig, _) in self.saved.items():
            world.light[y][x] = orig * level if level > 0.2 else level

    def _restore(self, world):
        for (x, y), (orig, panel) in self.saved.items():
            world.light[y][x] = orig
            world.panel[y][x] = panel

    def update(self, world: World, p: Player, dt, audio: Audio | None):
        if self.state is None:
            self.timer -= dt
            if self.timer <= 0:
                self._trigger(world, p)
            return

        self.t -= dt
        if self.state == "tease":
            # Irregular stutter: brief dips separated by uneasy holds of
            # normal light. Will it die? Usually not.
            self.sputter -= dt
            if self.sputter <= 0:
                self.dipped = not self.dipped
                if self.dipped:
                    self.sputter = self.rng.uniform(0.04, 0.18)
                    self._set(world, self.rng.uniform(0.05, 0.4))
                else:
                    self.sputter = self.rng.uniform(0.15, 0.9)
                    self._set(world, 1.0)
            if self.t <= 0:
                if self.record or self.rng.random() < self.DIE_CHANCE:
                    self.state = "dying"
                    self.t = 0.5
                    if audio:
                        audio.play_lightout(self.bearing, p)
                else:
                    # It steadies. This time.
                    self._restore(world)
                    self.saved = {}
                    self.state = None
                    self.timer = self.rng.uniform(30.0, 90.0)
        elif self.state == "dying":
            self._set(world, self.rng.uniform(0.05, 0.25))
            if self.t <= 0:
                for (x, y) in self.saved:
                    world.light[y][x] = 0.12
                    world.panel[y][x] = False
                self.state = "dead"
                self.t = self.rng.uniform(20.0, 40.0)
        elif self.state == "dead":
            if self.t <= 0:
                self.state = "recover"
                self.t = 1.5
        elif self.state == "recover":
            k = max(0.0, min(1.0, 1.0 - self.t / 1.5))
            for (x, y), (orig, _) in self.saved.items():
                world.light[y][x] = 0.12 + (orig - 0.12) * k
            if self.t <= 0:
                self._restore(world)
                self.saved = {}
                self.state = None
                self.record = False
                self.timer = self.rng.uniform(50.0, 120.0)


# ---------------------------------------------------------------------------
# Rendering: per-column sector casting, stepped heights, floor slopes
# ---------------------------------------------------------------------------

def render_frame(surface, world: World, p: Player, textures, pygame_module,
                 presence=None):
    half = HALF_H
    eye = p.eye_z()
    view_angle = p.angle + getattr(p, "sway", 0.0)
    dirx, diry = math.cos(view_angle), math.sin(view_angle)
    plane = math.tan(FOV / 2)
    planex, planey = -diry * plane, dirx * plane
    cols, rows = world.cols, world.rows
    wfloor, wceil = world.floor, world.ceil
    wgx, wgy = world.gx, world.gy
    wlight, wpanel = world.light, world.panel
    fill = surface.fill
    blit = surface.blit
    scale = pygame_module.transform.scale
    max_shade = SHADE_LEVELS - 1
    depth = [MAX_DEPTH] * INTERNAL_W    # per-column occlusion for the figure

    surface.fill(FOG)

    def blit_wall(col, y0, y1, d, u, base_h, dim):
        if y1 <= y0:
            return
        h0 = eye - (y0 - half) * d / PROJ_K
        h1 = eye - (y1 - half) * d / PROJ_K
        v0 = TEX_H - (h0 - base_h) * PX_PER_UNIT
        v1 = TEX_H - (h1 - base_h) * PX_PER_UNIT
        iv0 = max(0, min(TEX_H - 1, int(v0)))
        iv1 = max(iv0 + 1, min(TEX_H, int(math.ceil(v1))))
        eff = 1.0 - (1.0 - min(d / MAX_DEPTH, 1.0)) * dim
        tex = textures[int(eff * max_shade)]
        strip = tex.subsurface((int(u * TEX_W) % TEX_W, iv0, 1, iv1 - iv0))
        blit(scale(strip, (1, y1 - y0)), (col, y0))

    for col in range(INTERNAL_W):
        cam = 2.0 * col / INTERNAL_W - 1.0
        rdx, rdy = dirx + planex * cam, diry + planey * cam
        mx, my = int(p.x), int(p.y)
        ddx = abs(1.0 / rdx) if rdx else 1e30
        ddy = abs(1.0 / rdy) if rdy else 1e30
        stepx, sdx = (-1, (p.x - mx) * ddx) if rdx < 0 else (1, (mx + 1 - p.x) * ddx)
        stepy, sdy = (-1, (p.y - my) * ddy) if rdy < 0 else (1, (my + 1 - p.y) * ddy)

        ytop, ybot = 0, INTERNAL_H
        d_prev = 0.05
        cmx, cmy = mx, my           # current cell (raw, unwrapped)
        ci = (cmy % rows, cmx % cols)
        cur_f = wfloor[ci[0]][ci[1]]
        cur_c = wceil[ci[0]][ci[1]]
        cur_gx = wgx[ci[0]][ci[1]]
        cur_gy = wgy[ci[0]][ci[1]]
        cur_l = wlight[ci[0]][ci[1]]
        cur_pan = wpanel[ci[0]][ci[1]]

        while ytop < ybot:
            if sdx < sdy:
                d_next = sdx
                sdx += ddx
                mx += stepx
                side = 0
            else:
                d_next = sdy
                sdy += ddy
                my += stepy
                side = 1
            if d_next > MAX_DEPTH:
                depth[col] = MAX_DEPTH
                break
            depth[col] = d_next
            d_mid = (d_prev + d_next) * 0.5
            fog_mid = d_mid / MAX_DEPTH

            # boundary point where the ray leaves the current cell
            ex = p.x + rdx * d_next
            ey = p.y + rdy * d_next
            if cur_gx or cur_gy:
                f_cur_exit = (cur_f + cur_gx * (ex - cmx - 0.5)
                              + cur_gy * (ey - cmy - 0.5))
            else:
                f_cur_exit = cur_f

            # ceiling plane of the current cell
            y = half + int((eye - cur_c) * PROJ_K / d_next)
            if y > ytop:
                yend = min(y, ybot)
                if cur_pan:
                    color = shade(LIGHT_PANEL, fog_mid * 0.45)
                else:
                    color = shade(CEIL_TILE, fog_mid, cur_l)
                fill(color, (col, ytop, 1, yend - ytop))
                ytop = yend
                if ytop >= ybot:
                    break
            # floor plane (slope-aware at the exit point)
            y = half + int((eye - f_cur_exit) * PROJ_K / d_next)
            if y < ybot:
                ystart = max(y, ytop)
                if cur_f <= PIT_FLOOR + 0.01:
                    color = shade(PIT_BOTTOM, fog_mid)
                else:
                    dim = cur_l * min(1.0, 1.7 / max(cur_c - cur_f, 0.001))
                    color = shade(CARPET, fog_mid, dim)
                fill(color, (col, ystart, 1, ybot - ystart))
                ybot = ystart
                if ytop >= ybot:
                    break

            nxi, nyi = mx % cols, my % rows
            nf = wfloor[nyi][nxi]
            nc = wceil[nyi][nxi]
            ngx = wgx[nyi][nxi]
            ngy = wgy[nyi][nxi]
            if ngx or ngy:
                f_next_entry = nf + ngx * (ex - mx - 0.5) + ngy * (ey - my - 0.5)
            else:
                f_next_entry = nf
            wall_dim = cur_l * (0.85 if side else 1.0)
            u = (p.y + d_next * rdy) if side == 0 else (p.x + d_next * rdx)
            u -= int(u)

            if nf >= nc:
                blit_wall(col, ytop, ybot, d_next, u, f_cur_exit, wall_dim)
                ytop = ybot
                break

            if nc < cur_c:
                y = half + int((eye - nc) * PROJ_K / d_next)
                if y > ytop:
                    blit_wall(col, ytop, min(y, ybot), d_next, u,
                              f_cur_exit, wall_dim)
                    ytop = min(y, ybot)
                    if ytop >= ybot:
                        break
            if f_next_entry > f_cur_exit + 0.003:
                y = half + int((eye - f_next_entry) * PROJ_K / d_next)
                if y < ybot:
                    ystart = max(y, ytop)
                    if cur_f <= PIT_FLOOR + 0.01 and f_next_entry - f_cur_exit > 1.2:
                        fill(shade(PIT_SHAFT, d_next / MAX_DEPTH, wall_dim),
                             (col, ystart, 1, ybot - ystart))
                    else:
                        blit_wall(col, ystart, ybot, d_next, u,
                                  f_cur_exit, wall_dim)
                    ybot = ystart
                    if ytop >= ybot:
                        break

            cmx, cmy = mx, my
            cur_f, cur_c = nf, nc
            cur_gx, cur_gy = ngx, ngy
            cur_l = wlight[nyi][nxi]
            cur_pan = wpanel[nyi][nxi]
            d_prev = d_next

    # The figure: a dark silhouette, drawn only where the world is open
    # behind the walls already painted. At range it sinks into the fog.
    if presence is not None:
        relx, rely = world.wrap_delta(p.x, p.y, presence.x, presence.y)
        det = planex * diry - dirx * planey
        if abs(det) > 1e-9:
            inv = 1.0 / det
            tx = inv * (diry * relx - dirx * rely)
            ty = inv * (-planey * relx + planex * rely)
            if 0.4 < ty < MAX_DEPTH:
                sx = int(INTERNAL_W / 2 * (1.0 + tx / ty))
                pz = world.floor_at(presence.x, presence.y)
                feet = half + int((eye - pz) * PROJ_K / ty)
                top = half + int((eye - (pz + 0.88)) * PROJ_K / ty)
                w = max(1, int(0.34 * PROJ_K / ty))
                color = shade((13, 11, 9), (ty / MAX_DEPTH) * 0.9)
                height = feet - top
                for cx in range(sx - w // 2, sx + w // 2 + 1):
                    if 0 <= cx < INTERNAL_W and depth[cx] > ty:
                        frac = abs(cx - sx) / (w / 2 + 1e-6)
                        y0 = max(0, top + int(height * 0.12 * frac * frac))
                        y1 = min(INTERNAL_H, feet)
                        if y1 > y0:
                            fill(color, (cx, y0, 1, y1 - y0))


def render_minimap(world: World, p: Player, pygame_module):
    scale = 2
    surf = pygame_module.Surface((world.cols * scale, world.rows * scale))
    surf.set_alpha(210)
    surf.fill((10, 10, 10))
    for y in range(world.rows):
        for x in range(world.cols):
            f, c = world.floor[y][x], world.ceil[y][x]
            if f >= c:
                continue
            if f <= PIT_FLOOR + 0.01:
                color = (30, 26, 14)
            elif c > 1.5:
                color = (130, 118, 60)
            elif c < 0.6:
                color = (70, 60, 34)
            elif f < -0.05 or world.gx[y][x] or world.gy[y][x]:
                color = (100, 80, 46)
            else:
                color = (95, 86, 44)
            surf.fill(color, (x * scale, y * scale, scale, scale))
    px, py = int(p.x % world.cols * scale), int(p.y % world.rows * scale)
    pygame_module.draw.circle(surf, (255, 60, 60), (px, py), 3)
    pygame_module.draw.line(
        surf, (255, 60, 60), (px, py),
        (px + int(math.cos(p.angle) * 8), py + int(math.sin(p.angle) * 8)), 1)
    return surf


# ---------------------------------------------------------------------------
# Spawn / zones / export
# ---------------------------------------------------------------------------

def spawn(world: World, rng) -> Player:
    candidates = [(x, y) for (x, y) in sorted(world.open_set)
                  if world.floor[y % world.rows][x % world.cols] == 0.0
                  and world.ceil[y % world.rows][x % world.cols] >= 1.0]
    x, y = rng.choice(candidates or sorted(world.open_set))
    px, py = x + 0.5, y + 0.5
    best_angle, best_d = 0.0, -1.0
    for i in range(16):
        angle = i * math.tau / 16
        rdx, rdy = math.cos(angle), math.sin(angle)
        d = 0.0
        while d < 16.0:
            d += 0.5
            if world.solid(int(px + rdx * d), int(py + rdy * d)):
                break
        if d > best_d:
            best_angle, best_d = angle, d
    return Player(px, py, best_angle)


def move_to_zone(world: World, player: Player, zone: str) -> None:
    def is_zone(x, y):
        f, c = world.floor[y][x], world.ceil[y][x]
        if zone == "tall":
            return c > 1.5
        if zone == "crawl":
            return 0 < c < 0.6
        if zone == "pit":
            return f <= PIT_FLOOR + 0.01
        if zone == "ramp":
            return bool(world.gx[y][x] or world.gy[y][x])
        return -1.5 < f < -0.05

    targets = [(x, y) for y in range(world.rows) for x in range(world.cols)
               if is_zone(x, y)]
    if not targets:
        print(f"no '{zone}' zone in this map; leaving spawn unchanged")
        return
    tx, ty = targets[len(targets) // 2]
    best = None
    for y in range(max(0, ty - 8), min(world.rows, ty + 9)):
        for x in range(max(0, tx - 8), min(world.cols, tx + 9)):
            f, c = world.floor[y][x], world.ceil[y][x]
            if f == 0.0 and c >= 1.0:
                d = math.hypot(x - tx, y - ty)
                if 2.0 < d and (best is None or d < best[0]):
                    best = (d, x, y)
    if best:
        _, x, y = best
        player.x, player.y, player.z = x + 0.5, y + 0.5, 0.0
        player.angle = math.atan2(ty - y, tx - x)


def export_map(world: World, level: int, path: str) -> None:
    """Dump the full world as JSON for use in other engines. floor >= ceil
    means solid wall; heights are world units (1.0 ~ 2.7 m); gx/gy are
    per-cell floor slopes."""
    data = dict(
        format="backrooms-map", version=2, level=level, seed=world.seed,
        cols=world.cols, rows=world.rows, unit_meters=2.7,
        floor=[[round(v, 3) for v in row] for row in world.floor],
        ceil=[[round(v, 3) for v in row] for row in world.ceil],
        light=[[round(v, 2) for v in row] for row in world.light],
        panel=[[1 if v else 0 for v in row] for row in world.panel],
    )
    if any(any(row) for row in world.gx) or any(any(row) for row in world.gy):
        data["grad_x"] = [[round(v, 3) for v in row] for row in world.gx]
        data["grad_y"] = [[round(v, 3) for v in row] for row in world.gy]
    with open(path, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    print(f"seed {world.seed} -> {path}")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

def resource_path(rel: str) -> str:
    """Find bundled assets both from source and from a PyInstaller app."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="First-person walkthrough of a generated Backrooms map.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--level", type=int, choices=(0, 1), default=0,
                    help="0 = the classic yellow rooms, 1 = parking garage")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--map-cols", type=int, default=120, help="map width in cells (pre-upscale)")
    ap.add_argument("--map-rows", type=int, default=80, help="map height in cells (pre-upscale)")
    ap.add_argument("--manual", action="store_true", help="start in manual control instead of auto-walk")
    ap.add_argument("--windowed", action="store_true", help="start windowed instead of fullscreen")
    ap.add_argument("--no-shift", action="store_true", help="disable Peripheral Shift map warping")
    ap.add_argument("--no-entity", action="store_true",
                    help="nothing is walking toward you (screensaver mode)")
    ap.add_argument("--mute", action="store_true", help="no sound")
    ap.add_argument("--record", metavar="GIF", default=None,
                    help="record an auto-walk GIF headlessly and exit (needs pillow)")
    ap.add_argument("--seconds", type=float, default=10.0, help="GIF length with --record")
    ap.add_argument("--spawn-zone", choices=("tall", "crawl", "pit", "stairs", "ramp"),
                    default=None, help="spawn next to a specific zone type")
    ap.add_argument("--frame", metavar="PNG", default=None,
                    help="render a single frame headlessly to PNG and exit")
    ap.add_argument("--export", metavar="JSON", default=None,
                    help="write the generated world as JSON and exit")
    ap.add_argument("--no-fx", action="store_true",
                    help="disable bloom/vignette/grain post-processing")
    ap.add_argument("--hires", action="store_true",
                    help="render at 640x400 instead of 480x300")
    args = ap.parse_args(argv)

    if args.hires:
        set_resolution(640, 400)

    apply_style(args.level)

    if args.export:
        seed = args.seed if args.seed is not None else random.randrange(2**32)
        export_map(World(seed, args.map_cols, args.map_rows), args.level, args.export)
        return

    if args.record or args.frame:
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

    import pygame
    pygame.init()
    try:
        pygame.display.set_icon(pygame.image.load(resource_path("assets/icon.png")))
    except Exception:
        pass
    flags = 0
    if not (args.windowed or args.record or args.frame):
        flags = pygame.FULLSCREEN | pygame.SCALED
    screen = pygame.display.set_mode(
        (INTERNAL_W * WINDOW_SCALE, INTERNAL_H * WINDOW_SCALE), flags)
    clock = pygame.time.Clock()
    font = pygame.font.SysFont("menlo,consolas,monospace", 14)
    frame = pygame.Surface((INTERNAL_W, INTERNAL_H))
    textures = make_wall_textures(pygame)
    veil = pygame.Surface((INTERNAL_W, INTERNAL_H))
    veil.fill((0, 0, 0))
    fx = None if args.no_fx else PostFX(pygame, INTERNAL_W, INTERNAL_H)
    if fx is not None and not fx.ok:
        print("numpy not available: post-processing disabled")
        fx = None

    def new_world(seed):
        seed = random.randrange(2**32) if seed is None else seed
        world = World(seed, args.map_cols, args.map_rows)
        rng = random.Random(seed ^ 0xB4C4)
        player = spawn(world, rng)
        pygame.display.set_caption(
            f"The Backrooms — {STYLE['name']} — seed {seed}")
        return world, player, rng

    world, player, rng = new_world(args.seed)
    seed = world.seed

    if args.spawn_zone:
        move_to_zone(world, player, args.spawn_zone)

    if args.frame:
        render_frame(frame, world, player, textures, pygame)
        if fx:
            fx.apply(frame, 0.0, 0)
        pygame.image.save(pygame.transform.scale(
            frame, (INTERNAL_W * WINDOW_SCALE, INTERNAL_H * WINDOW_SCALE)), args.frame)
        print(f"seed {seed} -> {args.frame}")
        pygame.quit()
        return

    audio = Audio(pygame, rng, enabled=not (args.mute or args.record))
    walker = AutoWalker(rng)
    lights_out = LightsOut(rng, record=bool(args.record))
    presence = None if args.no_entity else Presence(
        world, player, rng, ahead=bool(args.record))
    auto = not args.manual or bool(args.record)
    show_map = False
    shift_timer = SHIFT_PERIOD
    fade = 0.0
    caught = False
    recorded = []
    record_frames = int(args.seconds * 15) if args.record else 0

    # Episodic flicker: steady light, occasional short buzzing dips.
    brightness = 1.0
    flicker_left = 0.0
    flicker_next = rng.uniform(4.0, 10.0)
    hum_scan = 0.0
    tick = 0

    # The guide overlay shows briefly at launch, then only after a keypress.
    hud_timer = 4.0

    running = True
    while running:
        dt = min(clock.tick(30) / 1000.0, 0.1) if not args.record else 1 / 30.0

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                hud_timer = 3.0
                if event.key == pygame.K_ESCAPE:
                    running = False
                elif event.key == pygame.K_f:
                    pygame.display.toggle_fullscreen()
                elif event.key == pygame.K_TAB:
                    auto = not auto
                elif event.key == pygame.K_m:
                    show_map = not show_map
                elif event.key == pygame.K_r:
                    world, player, rng = new_world(None)
                    seed = world.seed
                    walker = AutoWalker(rng)
                    lights_out = LightsOut(rng)
                    if presence is not None:
                        presence = Presence(world, player, rng)
                elif event.key == pygame.K_F12:
                    path = f"backrooms_walk_{seed}.png"
                    pygame.image.save(screen, path)
                    print(f"saved {path}")

        if fade <= 0.0:
            if auto:
                walker.update(world, player, dt, presence)
            else:
                keys = pygame.key.get_pressed()
                turn = ((keys[pygame.K_RIGHT] or keys[pygame.K_e])
                        - (keys[pygame.K_LEFT] or keys[pygame.K_q]))
                player.angle += turn * TURN_SPEED * dt
                fwd = keys[pygame.K_w] - keys[pygame.K_s]
                strafe = keys[pygame.K_d] - keys[pygame.K_a]
                run = keys[pygame.K_LSHIFT] or keys[pygame.K_RSHIFT]
                player.running = bool(run and (fwd or strafe))
                if fwd or strafe:
                    dx = math.cos(player.angle) * fwd - math.sin(player.angle) * strafe
                    dy = math.sin(player.angle) * fwd + math.cos(player.angle) * strafe
                    mag = math.hypot(dx, dy) or 1.0
                    speed = MOVE_SPEED * (1.8 if player.running else 1.0)
                    if player.crouched():
                        speed = min(speed, MOVE_SPEED * 0.55)
                    player.want_vx = dx / mag * speed
                    player.want_vy = dy / mag * speed
                else:
                    player.want_vx = player.want_vy = 0.0
                # Manual crouch anticipation: duck when the cell ahead is low.
                fx = int(player.x + math.cos(player.angle) * 1.2)
                fy = int(player.y + math.sin(player.angle) * 1.2)
                af, ac, _, _ = world.cell(fx, fy)
                player.crouch_hint = 0 < ac - af < 0.8
            player.apply(world, dt, WALKER_MAX_DROP if auto else None)
            if presence is not None and presence.update(world, player, dt, audio):
                caught = True
                fade = 1.3
                player.fear = 1.0
            if player.fell:
                fade = 1.2
        else:
            fade -= dt
            if fade < 0.6 and caught:
                # It reached him. Lights out — and he's somewhere else,
                # heart pounding. Nothing is ever confirmed on Level 0.
                presence.relocate(world, player)
                player.fear = 0.5
                caught = False
                walker = AutoWalker(rng)
            if player.fell and fade < 0.6:
                new_p = spawn(world, rng)
                player.x, player.y, player.angle = new_p.x, new_p.y, new_p.angle
                player.z = player.vz = 0.0
                player.vx = player.vy = player.want_vx = player.want_vy = 0.0
                player.fell = False
                walker = AutoWalker(rng)

        if not args.no_shift:
            shift_timer -= dt
            if shift_timer <= 0:
                shift_timer = SHIFT_PERIOD
                world.peripheral_shift(player.x, player.y, rng)

        # The hum belongs to the nearest live light panel.
        hum_scan -= dt
        if audio.ok and hum_scan <= 0:
            hum_scan = 0.25
            px, py = int(player.x), int(player.y)
            best = 12.0
            for yy in range(py - 10, py + 11):
                for xx in range(px - 10, px + 11):
                    if (world.panel[yy % world.rows][xx % world.cols]
                            and world.light[yy % world.rows][xx % world.cols] > 0.4):
                        d = math.hypot(xx + 0.5 - player.x, yy + 0.5 - player.y)
                        if d < best:
                            best = d
            audio.set_hum_proximity(best, brightness)

        audio.update(dt, player)
        lights_out.update(world, player, dt, audio)

        # When it gets close, the lights get nervous too — and the
        # soundscape knows where it is.
        if presence is not None:
            pd = math.hypot(*world.wrap_delta(player.x, player.y,
                                              presence.x, presence.y))
            if pd < 7.0:
                flicker_next = min(flicker_next, rng.uniform(0.3, 1.5))
            if audio.ok and player.presence_dist is not None:
                audio.set_presence(player.presence_dist, player.presence_bearing)

        if flicker_left > 0:
            flicker_left -= dt
            brightness = 1.0 if rng.random() < 0.4 else rng.uniform(0.55, 0.85)
            if flicker_left <= 0:
                brightness = 1.0
        else:
            flicker_next -= dt
            if flicker_next <= 0:
                flicker_left = rng.uniform(0.15, 0.5)
                flicker_next = rng.uniform(5.0, 14.0)

        render_frame(frame, world, player, textures, pygame, presence)
        tick += 1
        if fx:
            fx.apply(frame, player.fear, tick)
        if brightness < 0.999:
            veil.set_alpha(int((1.0 - brightness) * 220))
            frame.blit(veil, (0, 0))
        if fade > 0.0:
            k = min(1.0, (1.2 - abs(fade - 0.6) * 2) * 1.4)
            veil.set_alpha(int(k * 255))
            frame.blit(veil, (0, 0))
        pygame.transform.scale(frame, screen.get_size(), screen)

        if show_map:
            mm = render_minimap(world, player, pygame)
            if presence is not None:
                pygame.draw.circle(
                    mm, (110, 20, 20),
                    (int(presence.x % world.cols * 2),
                     int(presence.y % world.rows * 2)), 3)
            screen.blit(mm, (12, 12))
        hud_timer -= dt
        if hud_timer > 0:
            hud = f"{STYLE['name']}  seed {seed}  {'AUTO' if auto else 'MANUAL'}"
            if player.crouched():
                hud += "  [CRAWLSPACE]"
            hud += "  TAB=drive M=map R=new F=fullscreen ESC=quit"
            text = font.render(hud, True, (235, 225, 170))
            text.set_alpha(min(255, int(hud_timer * 400)))
            screen.blit(text, (12, screen.get_height() - 24))
        pygame.display.flip()

        if args.record:
            recorded.append(pygame.image.tobytes(frame, "RGB"))
            if len(recorded) >= record_frames:
                running = False

    pygame.quit()

    if args.record and recorded:
        try:
            from PIL import Image
        except ImportError:
            sys.exit("GIF recording needs pillow: pip install pillow")
        images = [Image.frombytes("RGB", (INTERNAL_W, INTERNAL_H), raw)
                  for raw in recorded]
        images[0].save(args.record, save_all=True, append_images=images[1:],
                       duration=66, loop=0, optimize=True)
        print(f"seed {seed} -> {args.record} ({len(images)} frames)")


if __name__ == "__main__":
    main()
