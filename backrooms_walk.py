#!/usr/bin/env python3
"""First-person walkthrough of a generated Backrooms map — with verticality.

A software renderer in the spirit of Build-engine sector casting: every cell
has its own floor and ceiling height, so Level 0 can do what the canon says
it does:

- grand halls with 30-foot ceilings where the fluorescent light barely
  reaches the carpet
- crawlspaces around four feet tall (auto-crouch)
- the Pitfalls: lattice-pattern fields of carpeted shafts ~8 m deep;
  falling in "noclips you deeper" and you respawn elsewhere
- stepped sunken areas — stairs down into lower wings
- drop-ceiling light panels, inconsistently placed, plus blackout zones
- textured wallpaper (striped, with a dark chair-rail trim) matched to the
  original Level 0 photo
- Peripheral Shift: the map re-carves itself where you aren't looking

The auto-walker plans real routes: it BFS-paths to a distant goal along a
slowly drifting exploration heading, strides there with smoothed steering
and a stride-synced camera bob, and re-plans when the level shifts under it.

Scale: 1 world unit = one normal room height (~2.7 m / 9 ft). Eye height
0.55 (~1.5 m). Tall halls reach 3.4 (~30 ft), crawlspaces 0.45 (~4 ft),
pits drop to -3.0 (~8 m).

Run it:

    python backrooms_walk.py                 # auto-walk demo (it drives)
    python backrooms_walk.py --manual        # you drive
    python backrooms_walk.py --record demo.gif --seconds 10   # headless GIF

Controls:

    TAB        toggle auto-walk
    W/A/S/D    move / strafe        arrows or Q/E   turn
    M          toggle minimap       R               new map (new seed)
    F12        save screenshot      ESC             quit
"""

from __future__ import annotations

import argparse
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

# Wall texture: PX_PER_UNIT rows per world unit, TEX_UNITS units tall.
TEX_W = 128
PX_PER_UNIT = 128
TEX_UNITS = 4
TEX_H = PX_PER_UNIT * TEX_UNITS
SHADE_LEVELS = 16

# Heights (world units; 1.0 = normal ceiling)
EYE_STAND = 0.55
EYE_CROUCH = 0.30
STEP_UP = 0.27          # max auto-step, like a stair riser
WALKER_MAX_DROP = 0.45  # auto-walker won't walk off bigger ledges
PIT_FLOOR = -3.0
FALL_LIMIT = -1.6       # below this you've fallen into a pit: respawn

# Palette, matched to the OG Level 0 photo: pale cream wallpaper, dark
# chair-rail trim, near-white drop ceiling with fluorescent panels.
WALL_UPPER = (221, 210, 156)
WALL_LOWER = (196, 180, 122)
WALL_TRIM = (118, 100, 60)
CEIL_TILE = (209, 201, 168)
LIGHT_PANEL = (255, 252, 224)
CARPET = (177, 157, 112)
PIT_SHAFT = (58, 50, 32)        # carpeted shaft walls, barely lit
PIT_BOTTOM = (8, 7, 4)
FOG = (24, 20, 8)

MOVE_SPEED = 2.2
TURN_SPEED = math.radians(115)
ACCEL = 6.0                     # velocity smoothing (1/s)
BOB_AMPLITUDE = 0.022
BOB_STRIDE_HZ = 1.85            # strides per second at full speed
PLAYER_RADIUS = 0.22
GRAVITY = 9.0
SHIFT_PERIOD = 1.6
SHIFT_SAFE_RADIUS = 10.0

TRIM_LO, TRIM_HI = 0.30, 0.345  # chair-rail band, in units above local floor


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
# Wall texture: procedural wallpaper, pre-shaded into fog levels
# ---------------------------------------------------------------------------

def make_wall_textures(pygame_module):
    """One TEX_W x TEX_H strip: striped cream wallpaper above, dark trim
    band and darker paper at the bottom (heights are measured up from the
    local floor; v=TEX_H is floor level). Returns SHADE_LEVELS copies,
    progressively blended into fog, ready for subsurface column blits."""
    rng = random.Random(7)
    tex = pygame_module.Surface((TEX_W, TEX_H))
    tex.fill(WALL_UPPER)

    # Vertical stripe bands of alternating tone, like the photo's paper.
    band_w = 16
    for x0 in range(0, TEX_W, band_w * 2):
        tex.fill(tuple(max(0, c - 12) for c in WALL_UPPER),
                 (x0, 0, band_w, TEX_H))
    # Thin pinstripes on band edges.
    for x0 in range(0, TEX_W, band_w):
        tex.fill(tuple(max(0, c - 26) for c in WALL_UPPER), (x0, 0, 1, TEX_H))
    # Sparse motif speckle so the paper reads as patterned, not flat.
    for _ in range(1400):
        x = rng.randrange(TEX_W)
        y = rng.randrange(TEX_H)
        delta = rng.choice((-18, -10, 8))
        tex.fill(tuple(min(255, max(0, c + delta)) for c in WALL_UPPER),
                 (x, y, 1, rng.randint(1, 3)))

    # Lower paper and trim, only in the bottom world unit.
    lower_top = TEX_H - int(TRIM_LO * PX_PER_UNIT)
    trim_top = TEX_H - int(TRIM_HI * PX_PER_UNIT)
    tex.fill(WALL_LOWER, (0, lower_top, TEX_W, TEX_H - lower_top))
    for _ in range(500):
        x = rng.randrange(TEX_W)
        y = rng.randrange(lower_top, TEX_H)
        tex.fill(tuple(max(0, c - rng.randint(4, 16)) for c in WALL_LOWER),
                 (x, y, 1, 2))
    tex.fill(WALL_TRIM, (0, trim_top, TEX_W, lower_top - trim_top))
    tex.fill(tuple(min(255, c + 30) for c in WALL_TRIM), (0, trim_top, TEX_W, 1))

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
# World: per-cell floor/ceiling heights + colors
# ---------------------------------------------------------------------------

class World:
    """floor[y][x] / ceil[y][x] in world units. Solid wall: floor >= ceil.
    light[y][x] scales colors (blackout zones), panel[y][x] marks a
    fluorescent tile in the ceiling. Coordinates wrap at the edges."""

    def __init__(self, seed: int, cols: int, rows: int):
        self.seed = seed
        rng = random.Random(seed ^ 0x5EED)
        cfg = bg.Config(width=cols * 8, height=rows * 8, cell_size=8,
                        rooms=4, pillar_rooms=3, poly_rooms=3)
        grid = bg.generate(cfg, seed)

        # Upscale 2x so corridors are two cells (~5.4 m) wide.
        open_cells = []
        self.rows, self.cols = len(grid) * 2, len(grid[0]) * 2
        R, C = self.rows, self.cols
        self.floor = [[0.0] * C for _ in range(R)]
        self.ceil = [[0.0] * C for _ in range(R)]
        self.light = [[1.0] * C for _ in range(R)]
        for y in range(R):
            for x in range(C):
                if grid[y // 2][x // 2] == bg.FLOOR:
                    self.ceil[y][x] = 1.0
                    open_cells.append((x, y))
        self.open_set = set(open_cells)

        self._add_tall_halls(rng)
        self._add_crawlspaces(rng)
        self._add_sunken_wings(rng)
        self._add_pitfalls(rng)
        self._add_blackouts(rng)
        self._place_panels(rng)

    # -- zone helpers -------------------------------------------------------

    def _blob(self, rng, size):
        """Grow a random connected blob of open cells."""
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
        # "rooms so massive... the lights get too high to reach the ground"
        for _ in range(rng.randint(3, 5)):
            h = rng.uniform(1.8, 3.4)
            for x, y in self._blob(rng, rng.randint(150, 600)):
                if self.ceil[y][x] > 0:
                    self.ceil[y][x] = h

    def _add_crawlspaces(self, rng):
        # ~4 ft: like the space above the drop ceiling, but you're in it
        for _ in range(rng.randint(2, 4)):
            for x, y in self._blob(rng, rng.randint(80, 300)):
                if self.ceil[y][x] > 0:
                    self.ceil[y][x] = 0.45
                    self.light[y][x] = min(self.light[y][x], 0.7)

    def _add_sunken_wings(self, rng):
        # Lower wings reached by stairs: floor steps down ring by ring.
        for _ in range(rng.randint(2, 3)):
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

    def _add_pitfalls(self, rng):
        # The Pitfalls: a lattice floor full of ~8 m carpeted shafts.
        for _ in range(rng.randint(1, 2)):
            blob = self._blob(rng, rng.randint(120, 350))
            for x, y in blob:
                if self.ceil[y][x] > 0 and x % 3 != 0 and y % 3 != 0:
                    if self.floor[y][x] == 0.0:
                        self.floor[y][x] = PIT_FLOOR

    def _add_blackouts(self, rng):
        # Entire sections devoid of lighting.
        for _ in range(rng.randint(1, 2)):
            for x, y in self._blob(rng, rng.randint(100, 300)):
                self.light[y][x] = 0.3

    def _place_panels(self, rng):
        # Inconsistently placed fluorescent tiles on a loose grid.
        self.panel = [[False] * self.cols for _ in range(self.rows)]
        for y in range(self.rows):
            for x in range(self.cols):
                if (self.ceil[y][x] > 0.5 and x % 2 == 1 and y % 3 == 1
                        and self.light[y][x] > 0.5 and rng.random() < 0.7):
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
        """Can the walker step from cell a to adjacent cell b?"""
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
        """Shortest vector a->b on the wrapped map."""
        dx = (bx - ax + self.cols / 2) % self.cols - self.cols / 2
        dy = (by - ay + self.rows / 2) % self.rows - self.rows / 2
        return dx, dy

    def peripheral_shift(self, px, py, rng):
        """Rearrange the level far from the player: carve corridor runs
        (inheriting a neighbor's heights) or grow new wall cells."""
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
                        self.floor[y][x], self.ceil[y][x] = 0.0, 1.0
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
        self.z = 0.0                 # feet height
        self.vz = 0.0
        self.vx = self.vy = 0.0      # smoothed horizontal velocity
        self.want_vx = self.want_vy = 0.0
        self.eye = EYE_STAND         # eye offset above feet, lerps for crouch
        self.bob_phase = 0.0
        self.bob = 0.0
        self.fell = False

    def eye_z(self):
        return self.z + self.eye + self.bob

    def _clear(self, world, x, y, max_drop):
        r = PLAYER_RADIUS
        return all(world.passable(self.z, int(x + ox) % world.cols,
                                  int(y + oy) % world.rows, max_drop)
                   for ox in (-r, r) for oy in (-r, r))

    def apply(self, world: World, dt, max_drop):
        """Velocity smoothing, wall-sliding move, gravity, crouch, bob."""
        k = min(1.0, ACCEL * dt)
        self.vx += (self.want_vx - self.vx) * k
        self.vy += (self.want_vy - self.vy) * k

        dx, dy = self.vx * dt, self.vy * dt
        if self._clear(world, self.x + dx, self.y, max_drop):
            self.x = (self.x + dx) % world.cols
        else:
            self.vx = 0.0
        if self._clear(world, self.x, self.y + dy, max_drop):
            self.y = (self.y + dy) % world.rows
        else:
            self.vy = 0.0

        # Vertical: gravity, stair step-up smoothing, pit detection.
        f, c, _, _ = world.cell(int(self.x), int(self.y))
        if self.z > f + 0.005:
            self.vz -= GRAVITY * dt
            self.z = max(f, self.z + self.vz * dt)
            if self.z <= f:
                self.vz = 0.0
        elif self.z < f:
            self.z = min(f, self.z + 5.0 * dt)
        if self.z <= FALL_LIMIT:
            self.fell = True

        # Crouch under low clearance.
        clearance = c - f
        target = EYE_STAND if clearance > 0.75 else max(0.22, clearance - 0.15)
        self.eye += (target - self.eye) * min(1.0, 8.0 * dt)

        # Stride-synced head bob, fading with speed.
        speed = math.hypot(self.vx, self.vy)
        ratio = min(1.0, speed / MOVE_SPEED)
        self.bob_phase += dt * math.tau * BOB_STRIDE_HZ * (0.4 + 0.6 * ratio)
        self.bob = math.sin(self.bob_phase) * BOB_AMPLITUDE * ratio

    def crouched(self):
        return self.eye < EYE_STAND - 0.1


# ---------------------------------------------------------------------------
# Auto-walker: BFS route planning + carrot-point pursuit
# ---------------------------------------------------------------------------

class AutoWalker:
    """Explores with intent: BFS from the current cell over walkable edges,
    picks a distant goal biased along a slowly drifting exploration heading,
    then pursues the path via a moving carrot point — smooth cornering, no
    per-cell dithering. Re-plans when Peripheral Shift breaks the route,
    and pauses to look around only on arrival."""

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

    def plan(self, world: World, p: Player):
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

        # Goal: far cells, preferring ones along the exploration heading.
        far = order[len(order) // 2:]
        if not far:
            self.path = []
            return

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
        # The heading drifts so long-term wandering covers the level.
        self.explore_angle += self.rng.uniform(-0.7, 0.7)

    def update(self, world: World, p: Player, dt):
        if self.pause > 0:
            self.pause -= dt
            p.want_vx = p.want_vy = 0.0
            p.angle += self.pause_turn * dt
            return

        self.repath_timer -= dt
        if self.repath_timer <= 0 or self.idx >= len(self.path):
            self.plan(world, p)
            if not self.path:
                p.want_vx = p.want_vy = 0.0
                p.angle += TURN_SPEED * 0.4 * dt   # boxed in: scan for an exit
                return

        # Validate the route just ahead (Peripheral Shift can sever it).
        here = (int(p.x) % world.cols, int(p.y) % world.rows)
        nxt = self.path[self.idx]
        if not world.passable(p.z, nxt[0], nxt[1], WALKER_MAX_DROP):
            self.plan(world, p)
            if not self.path:
                return
            nxt = self.path[self.idx]

        # Advance the carrot: swallow nodes we're already close to.
        while self.idx < len(self.path) - 1:
            cx, cy = self.path[self.idx]
            dx, dy = world.wrap_delta(p.x, p.y, cx + 0.5, cy + 0.5)
            if math.hypot(dx, dy) < self.CARROT_RADIUS:
                self.idx += 1
            else:
                break

        cx, cy = self.path[self.idx]
        dx, dy = world.wrap_delta(p.x, p.y, cx + 0.5, cy + 0.5)
        dist = math.hypot(dx, dy)

        # Arrived at the goal: brief look-around, then a new plan.
        if self.idx >= len(self.path) - 1 and dist < 0.5:
            self.path = []
            if self.rng.random() < 0.5:
                self.pause = self.rng.uniform(0.7, 1.8)
                self.pause_turn = self.rng.choice((-1, 1)) * TURN_SPEED * 0.35
            return

        target = math.atan2(dy, dx)
        diff = (target - p.angle + math.pi) % math.tau - math.pi
        p.angle += max(-TURN_SPEED * dt, min(TURN_SPEED * dt, diff))

        # Walk along the facing direction; slow through sharp corners.
        alignment = math.cos(diff)
        speed = MOVE_SPEED * (0.55 if p.crouched() else 0.9)
        speed *= max(0.0, alignment) if abs(diff) < 1.5 else 0.0
        p.want_vx = math.cos(p.angle) * speed
        p.want_vy = math.sin(p.angle) * speed


# ---------------------------------------------------------------------------
# Rendering: per-column sector casting with stepped heights + textures
# ---------------------------------------------------------------------------

def render_frame(surface, world: World, p: Player, textures, pygame_module):
    half = HALF_H
    eye = p.eye_z()
    dirx, diry = math.cos(p.angle), math.sin(p.angle)
    plane = math.tan(FOV / 2)
    planex, planey = -diry * plane, dirx * plane
    cols, rows = world.cols, world.rows
    wfloor, wceil, wlight, wpanel = world.floor, world.ceil, world.light, world.panel
    fill = surface.fill
    blit = surface.blit
    scale = pygame_module.transform.scale
    max_shade = SHADE_LEVELS - 1

    surface.fill(FOG)

    def blit_wall(col, y0, y1, d, u, base_h, dim):
        """Textured wall span: world heights are recovered from the screen
        rows, then mapped into the texture strip (v = up from base_h)."""
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
        cur_f, cur_c, cur_l, cur_pan = world.cell(mx, my)

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
                break
            d_mid = (d_prev + d_next) * 0.5
            fog_mid = d_mid / MAX_DEPTH

            # ceiling plane of the current cell: the near ceiling occludes
            # everything steeper than its exit projection
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
            # floor plane of the current cell, symmetric
            y = half + int((eye - cur_f) * PROJ_K / d_next)
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

            nx, ny = mx % cols, my % rows
            nf, nc = wfloor[ny][nx], wceil[ny][nx]
            nl, npan = wlight[ny][nx], wpanel[ny][nx]
            wall_dim = cur_l * (0.85 if side else 1.0)
            if side == 0:
                u = p.y + d_next * rdy
            else:
                u = p.x + d_next * rdx
            u -= int(u)

            if nf >= nc:
                # solid wall fills the rest of the window
                blit_wall(col, ytop, ybot, d_next, u, cur_f, wall_dim)
                ytop = ybot
                break

            # upper step (next ceiling is lower)
            if nc < cur_c:
                y = half + int((eye - nc) * PROJ_K / d_next)
                if y > ytop:
                    blit_wall(col, ytop, min(y, ybot), d_next, u, cur_f, wall_dim)
                    ytop = min(y, ybot)
                    if ytop >= ybot:
                        break
            # lower step (next floor is higher) — stairs, ledges, shafts
            if nf > cur_f:
                y = half + int((eye - nf) * PROJ_K / d_next)
                if y < ybot:
                    ystart = max(y, ytop)
                    if cur_f <= PIT_FLOOR + 0.01 and nf - cur_f > 1.2:
                        fill(shade(PIT_SHAFT, d_next / MAX_DEPTH, wall_dim),
                             (col, ystart, 1, ybot - ystart))
                    else:
                        blit_wall(col, ystart, ybot, d_next, u, cur_f, wall_dim)
                    ybot = ystart
                    if ytop >= ybot:
                        break

            cur_f, cur_c, cur_l, cur_pan = nf, nc, nl, npan
            d_prev = d_next


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
                color = (130, 118, 60)      # tall halls brighter
            elif c < 0.6:
                color = (70, 60, 34)        # crawlspaces darker
            elif f < -0.05:
                color = (100, 80, 46)       # sunken wings
            else:
                color = (95, 86, 44)
            surf.fill(color, (x * scale, y * scale, scale, scale))
    px, py = int(p.x % world.cols * scale), int(p.y % world.rows * scale)
    pygame_module.draw.circle(surf, (255, 60, 60), (px, py), 3)
    pygame_module.draw.line(
        surf, (255, 60, 60), (px, py),
        (px + int(math.cos(p.angle) * 8), py + int(math.sin(p.angle) * 8)), 1)
    return surf


def make_hum_sound(pygame_module):
    """Fluorescent hum-buzz: 120 Hz mains hum + harmonics + a little noise."""
    try:
        pygame_module.mixer.init(frequency=22050, size=-16, channels=1)
        rate = 22050
        rng = random.Random(0)
        samples = array("h")
        for i in range(rate):
            t = i / rate
            v = (math.sin(math.tau * 120 * t) * 0.45
                 + math.sin(math.tau * 240 * t) * 0.25
                 + math.sin(math.tau * 360 * t) * 0.12
                 + rng.uniform(-1, 1) * 0.08)
            samples.append(int(v * 3200))
        return pygame_module.mixer.Sound(buffer=samples.tobytes())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

def spawn(world: World, rng) -> Player:
    """Spawn on a normal-height floor cell facing the longest sightline."""
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
    """Reposition the player on a normal cell near a zone of the given type,
    facing it. Used for demos and testing."""
    def is_zone(f, c):
        if zone == "tall":
            return c > 1.5
        if zone == "crawl":
            return 0 < c < 0.6
        if zone == "pit":
            return f <= PIT_FLOOR + 0.01
        return -1.5 < f < -0.05        # stairs / sunken wing

    targets = [(x, y) for y in range(world.rows) for x in range(world.cols)
               if is_zone(world.floor[y][x], world.ceil[y][x])]
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


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="First-person walkthrough of a generated Backrooms map.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--map-cols", type=int, default=120, help="map width in cells (pre-upscale)")
    ap.add_argument("--map-rows", type=int, default=80, help="map height in cells (pre-upscale)")
    ap.add_argument("--manual", action="store_true", help="start in manual control instead of auto-walk")
    ap.add_argument("--no-shift", action="store_true", help="disable Peripheral Shift map warping")
    ap.add_argument("--mute", action="store_true", help="no fluorescent hum")
    ap.add_argument("--record", metavar="GIF", default=None,
                    help="record an auto-walk GIF headlessly and exit (needs pillow)")
    ap.add_argument("--seconds", type=float, default=10.0, help="GIF length with --record")
    ap.add_argument("--spawn-zone", choices=("tall", "crawl", "pit", "stairs"),
                    default=None, help="spawn next to a specific zone type")
    ap.add_argument("--frame", metavar="PNG", default=None,
                    help="render a single frame headlessly to PNG and exit")
    args = ap.parse_args(argv)

    if args.record or args.frame:
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

    import pygame
    pygame.init()
    screen = pygame.display.set_mode(
        (INTERNAL_W * WINDOW_SCALE, INTERNAL_H * WINDOW_SCALE))
    clock = pygame.time.Clock()
    font = pygame.font.SysFont("menlo,consolas,monospace", 14)
    frame = pygame.Surface((INTERNAL_W, INTERNAL_H))
    textures = make_wall_textures(pygame)
    flicker_veil = pygame.Surface((INTERNAL_W, INTERNAL_H))
    flicker_veil.fill((0, 0, 0))

    hum = None if (args.mute or args.record or args.frame) else make_hum_sound(pygame)
    if hum:
        hum.set_volume(0.35)
        hum.play(loops=-1)

    def new_world(seed):
        seed = random.randrange(2**32) if seed is None else seed
        world = World(seed, args.map_cols, args.map_rows)
        rng = random.Random(seed ^ 0xB4C4)
        player = spawn(world, rng)
        pygame.display.set_caption(f"The Backrooms — Level 0 — seed {seed}")
        return world, player, rng

    world, player, rng = new_world(args.seed)
    seed = world.seed

    if args.spawn_zone:
        move_to_zone(world, player, args.spawn_zone)

    if args.frame:
        render_frame(frame, world, player, textures, pygame)
        pygame.image.save(pygame.transform.scale(
            frame, (INTERNAL_W * WINDOW_SCALE, INTERNAL_H * WINDOW_SCALE)), args.frame)
        print(f"seed {seed} -> {args.frame}")
        pygame.quit()
        return

    walker = AutoWalker(rng)
    auto = not args.manual or bool(args.record)
    show_map = False
    brightness = 1.0
    shift_timer = SHIFT_PERIOD
    fade = 0.0                      # >0 while respawning after a pit fall
    recorded = []
    record_frames = int(args.seconds * 15) if args.record else 0

    running = True
    while running:
        dt = min(clock.tick(30) / 1000.0, 0.1) if not args.record else 1 / 30.0

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    running = False
                elif event.key == pygame.K_TAB:
                    auto = not auto
                elif event.key == pygame.K_m:
                    show_map = not show_map
                elif event.key == pygame.K_r:
                    world, player, rng = new_world(None)
                    seed = world.seed
                    walker = AutoWalker(rng)
                elif event.key == pygame.K_F12:
                    path = f"backrooms_walk_{seed}.png"
                    pygame.image.save(screen, path)
                    print(f"saved {path}")

        if fade <= 0.0:
            if auto:
                walker.update(world, player, dt)
            else:
                keys = pygame.key.get_pressed()
                turn = ((keys[pygame.K_RIGHT] or keys[pygame.K_e])
                        - (keys[pygame.K_LEFT] or keys[pygame.K_q]))
                player.angle += turn * TURN_SPEED * dt
                fwd = keys[pygame.K_w] - keys[pygame.K_s]
                strafe = keys[pygame.K_d] - keys[pygame.K_a]
                if fwd or strafe:
                    dx = math.cos(player.angle) * fwd - math.sin(player.angle) * strafe
                    dy = math.sin(player.angle) * fwd + math.cos(player.angle) * strafe
                    mag = math.hypot(dx, dy) or 1.0
                    speed = MOVE_SPEED * (0.5 if player.crouched() else 1.0)
                    player.want_vx = dx / mag * speed
                    player.want_vy = dy / mag * speed
                else:
                    player.want_vx = player.want_vy = 0.0
            # Manual movement may walk off ledges (max_drop=None).
            player.apply(world, dt, WALKER_MAX_DROP if auto else None)
            if player.fell:
                fade = 1.2      # fell into the Pitfalls: noclip deeper
        else:
            fade -= dt
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

        # Fluorescent flicker: a jittery drift under full brightness with
        # the occasional deep dip, applied as a global veil so the textures
        # flicker too.
        brightness += rng.uniform(-0.05, 0.05)
        brightness = min(1.0, max(0.9, brightness + (1.0 - brightness) * 0.2))
        if rng.random() < 0.006:
            brightness = 0.55

        render_frame(frame, world, player, textures, pygame)
        if brightness < 0.999:
            flicker_veil.set_alpha(int((1.0 - brightness) * 230))
            frame.blit(flicker_veil, (0, 0))
        if fade > 0.0:
            veil = min(1.0, (1.2 - abs(fade - 0.6) * 2) * 1.4)
            flicker_veil.set_alpha(int(veil * 255))
            frame.blit(flicker_veil, (0, 0))
        pygame.transform.scale(frame, screen.get_size(), screen)

        if show_map:
            screen.blit(render_minimap(world, player, pygame), (12, 12))
        hud = f"seed {seed}  {'AUTO' if auto else 'MANUAL'}"
        if player.crouched():
            hud += "  [CRAWLSPACE]"
        hud += "  TAB=drive M=map R=new ESC=quit"
        screen.blit(font.render(hud, True, (235, 225, 170)),
                    (12, screen.get_height() - 24))
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
