#!/usr/bin/env python3
"""First-person walkthrough of a generated Backrooms map.

A software raycaster (Wolfenstein-style DDA) rendered with pygame over maps
from backrooms_generator.py, styled after Level 0 canon:

- mono-yellow wallpaper with a darker lower band, beige Berber carpet,
  fluorescent flicker and distance fog
- "Peripheral Shift": the map quietly re-carves itself in areas you are
  not looking at, so retracing your steps never quite works
- no entities; Level 0 is empty. That's the point.

Run it:

    python backrooms_walk.py                 # auto-walk demo (it drives)
    python backrooms_walk.py --manual        # you drive
    python backrooms_walk.py --seed 1234

Record a GIF headlessly (needs pillow):

    python backrooms_walk.py --record demo.gif --seconds 8

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
INTERNAL_W, INTERNAL_H = 480, 300   # raycast resolution, scaled up to window
WINDOW_SCALE = 2
FOV = math.radians(72)
WALL_HEIGHT = 0.72                  # < 1.0 lowers the ceiling: wide + oppressive
MAX_DEPTH = 22.0

# Level 0 palette
WALL_UPPER = (206, 188, 110)        # worn mono-yellow wallpaper
WALL_LOWER = (166, 146, 78)         # darker band along the bottom
WALL_STRIPE = (128, 110, 56)
CEIL_NEAR = (194, 182, 120)
FLOOR_NEAR = (150, 128, 84)         # brownish-beige Berber carpet
FOG = (28, 24, 10)

MOVE_SPEED = 2.6                    # world cells / second
TURN_SPEED = math.radians(120)
PLAYER_RADIUS = 0.22
SHIFT_PERIOD = 1.6                  # seconds between peripheral shift events
SHIFT_SAFE_RADIUS = 9.0             # cells around the player left untouched


# ---------------------------------------------------------------------------
# World
# ---------------------------------------------------------------------------

def build_world(seed: int, cols: int, rows: int) -> list[list[int]]:
    """Generate a map and upscale it 2x so corridors are two cells wide."""
    cfg = bg.Config(width=cols * 8, height=rows * 8, cell_size=8,
                    rooms=4, pillar_rooms=3, poly_rooms=3)
    grid = bg.generate(cfg, seed)
    world = []
    for row in grid:
        wide = [cell for cell in row for _ in (0, 1)]
        world.append(wide)
        world.append(list(wide))
    return world


def largest_floor_region(world: list[list[int]]) -> set[tuple[int, int]]:
    rows, cols = len(world), len(world[0])
    seen: set[tuple[int, int]] = set()
    best: set[tuple[int, int]] = set()
    for y in range(rows):
        for x in range(cols):
            if world[y][x] != bg.FLOOR or (x, y) in seen:
                continue
            region = {(x, y)}
            queue = deque([(x, y)])
            seen.add((x, y))
            while queue:
                cx, cy = queue.popleft()
                for nx, ny in ((cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1)):
                    if (0 <= nx < cols and 0 <= ny < rows
                            and world[ny][nx] == bg.FLOOR and (nx, ny) not in seen):
                        seen.add((nx, ny))
                        region.add((nx, ny))
                        queue.append((nx, ny))
            if len(region) > len(best):
                best = region
    return best


def peripheral_shift(world: list[list[int]], px: float, py: float,
                     rng: random.Random) -> None:
    """Quietly rearrange the level far from the player: carve a new corridor
    run, or drop a few wall cells into open floor. Canon says the layout
    warps when unobserved; the minimap makes this fun to watch."""
    rows, cols = len(world), len(world[0])
    for _ in range(20):  # find a spot outside the safe radius
        x, y = rng.randrange(1, cols - 1), rng.randrange(1, rows - 1)
        if math.hypot(x - px, y - py) > SHIFT_SAFE_RADIUS:
            break
    else:
        return

    if rng.random() < 0.7:
        # Drunkard's-walk carve: a corridor that wasn't there before.
        for _ in range(rng.randint(20, 70)):
            world[y][x] = bg.FLOOR
            dx, dy = rng.choice(((1, 0), (-1, 0), (0, 1), (0, -1)))
            x = min(max(x + dx, 1), cols - 2)
            y = min(max(y + dy, 1), rows - 2)
            if math.hypot(x - px, y - py) <= SHIFT_SAFE_RADIUS:
                return
    else:
        # Walls close in: a scatter of new wall cells / pillars.
        for _ in range(rng.randint(3, 10)):
            wx = min(max(x + rng.randint(-4, 4), 1), cols - 2)
            wy = min(max(y + rng.randint(-4, 4), 1), rows - 2)
            if math.hypot(wx - px, wy - py) > SHIFT_SAFE_RADIUS:
                world[wy][wx] = bg.WALL


# ---------------------------------------------------------------------------
# Player + auto-walker
# ---------------------------------------------------------------------------

class Player:
    def __init__(self, x: float, y: float, angle: float):
        self.x, self.y, self.angle = x, y, angle

    def move(self, world: list[list[int]], dx: float, dy: float) -> None:
        """Axis-separated move with wall sliding."""
        r = PLAYER_RADIUS
        nx = self.x + dx
        if all(world[int(self.y + oy)][int(nx + ox)] == bg.FLOOR
               for ox in (-r, r) for oy in (-r, r)):
            self.x = nx
        ny = self.y + dy
        if all(world[int(ny + oy)][int(self.x + ox)] == bg.FLOOR
               for ox in (-r, r) for oy in (-r, r)):
            self.y = ny


class AutoWalker:
    """Wanders cell to cell, preferring to keep going straight, with the
    occasional pause to look around. Steering is smooth: it turns toward
    the next waypoint and only walks when roughly facing it."""

    def __init__(self, rng: random.Random):
        self.rng = rng
        self.waypoint: tuple[int, int] | None = None
        self.came_from: tuple[int, int] | None = None
        self.glance_timer = rng.uniform(6.0, 16.0)
        self.glancing = 0.0
        self.glance_dir = 1.0

    def pick_waypoint(self, world: list[list[int]], p: Player) -> None:
        cx, cy = int(p.x), int(p.y)
        options = [(nx, ny)
                   for nx, ny in ((cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1))
                   if world[ny][nx] == bg.FLOOR]
        if not options:
            self.waypoint = None
            return
        forward = [o for o in options if o != self.came_from]
        pool = forward or options
        # Prefer the option most aligned with the current heading.
        weights = []
        for nx, ny in pool:
            heading = math.atan2(ny + 0.5 - p.y, nx + 0.5 - p.x)
            align = math.cos(heading - p.angle)
            weights.append(1.0 + max(0.0, align) * 3.0)
        self.came_from = (cx, cy)
        self.waypoint = self.rng.choices(pool, weights)[0]

    def update(self, world: list[list[int]], p: Player, dt: float) -> None:
        if self.glancing > 0:
            self.glancing -= dt
            p.angle += self.glance_dir * TURN_SPEED * 0.5 * dt
            return
        self.glance_timer -= dt
        if self.glance_timer <= 0:
            self.glance_timer = self.rng.uniform(6.0, 16.0)
            self.glancing = self.rng.uniform(0.8, 1.8)
            self.glance_dir = self.rng.choice((-1.0, 1.0))
            return

        if self.waypoint is None or world[self.waypoint[1]][self.waypoint[0]] != bg.FLOOR:
            self.pick_waypoint(world, p)
            if self.waypoint is None:
                return
        wx, wy = self.waypoint[0] + 0.5, self.waypoint[1] + 0.5
        if math.hypot(wx - p.x, wy - p.y) < 0.2:
            self.pick_waypoint(world, p)
            return

        target = math.atan2(wy - p.y, wx - p.x)
        diff = (target - p.angle + math.pi) % math.tau - math.pi
        p.angle += max(-TURN_SPEED * dt, min(TURN_SPEED * dt, diff))
        if abs(diff) < 0.6:
            speed = MOVE_SPEED * 0.85
            p.move(world, math.cos(p.angle) * speed * dt,
                   math.sin(p.angle) * speed * dt)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def shade(color: tuple[int, int, int], t: float, brightness: float) -> tuple[int, int, int]:
    """Blend toward fog by t (0 near, 1 far), then apply flicker brightness."""
    t = min(max(t, 0.0), 1.0)
    return tuple(min(255, int((c + (f - c) * t) * brightness))
                 for c, f in zip(color, FOG))


def make_background(brightness: float, pygame_module) -> "pygame.Surface":
    """Ceiling and carpet as vertical gradients that sink into fog at the
    horizon. Redrawn only when flicker brightness changes noticeably."""
    surf = pygame_module.Surface((INTERNAL_W, INTERNAL_H))
    half = INTERNAL_H // 2
    for y in range(half):
        t = 1.0 - y / half            # 0 at top (near), 1 at horizon (far)
        surf.fill(shade(CEIL_NEAR, 1.0 - t, brightness),
                  (0, y, INTERNAL_W, 1))
    for y in range(half, INTERNAL_H):
        t = (y - half) / half         # 0 at horizon, 1 at bottom (near)
        surf.fill(shade(FLOOR_NEAR, 1.0 - t, brightness),
                  (0, y, INTERNAL_W, 1))
    return surf


def cast_ray(world, px, py, rdx, rdy):
    """DDA grid traversal. Returns (perpendicular distance, side, u) where
    side is 0 for x-walls, 1 for y-walls and u the position along the wall."""
    mx, my = int(px), int(py)
    ddx = abs(1.0 / rdx) if rdx else 1e30
    ddy = abs(1.0 / rdy) if rdy else 1e30
    stepx, sdx = (-1, (px - mx) * ddx) if rdx < 0 else (1, (mx + 1 - px) * ddx)
    stepy, sdy = (-1, (py - my) * ddy) if rdy < 0 else (1, (my + 1 - py) * ddy)
    rows, cols = len(world), len(world[0])

    side = 0
    while True:
        if sdx < sdy:
            sdx += ddx
            mx += stepx
            side = 0
        else:
            sdy += ddy
            my += stepy
            side = 1
        if not (0 <= mx < cols and 0 <= my < rows):
            return MAX_DEPTH, side, 0.0
        if world[my][mx] == bg.WALL:
            break

    if side == 0:
        dist = sdx - ddx
        u = py + dist * rdy
    else:
        dist = sdy - ddy
        u = px + dist * rdx
    return max(dist, 1e-6), side, u - int(u)


def render_frame(surface, world, p: Player, brightness: float, pygame_module) -> None:
    dirx, diry = math.cos(p.angle), math.sin(p.angle)
    plane = math.tan(FOV / 2)
    planex, planey = -diry * plane, dirx * plane
    half = INTERNAL_H // 2

    for col in range(INTERNAL_W):
        cam = 2.0 * col / INTERNAL_W - 1.0
        rdx, rdy = dirx + planex * cam, diry + planey * cam
        dist, side, u = cast_ray(world, p.x, p.y, rdx, rdy)
        if dist >= MAX_DEPTH:
            continue

        line_h = int(INTERNAL_H * WALL_HEIGHT / dist)
        top = half - line_h // 2
        fog_t = dist / MAX_DEPTH
        dim = brightness * (0.82 if side else 1.0)   # y-walls slightly darker

        upper = shade(WALL_UPPER, fog_t, dim)
        lower = shade(WALL_LOWER, fog_t, dim)
        stripe = shade(WALL_STRIPE, fog_t, dim)
        if int(u * 6) % 2:                            # faint wallpaper striping
            upper = tuple(max(0, c - 8) for c in upper)

        band_y = top + int(line_h * 0.66)             # darker band low on the wall
        stripe_h = max(1, line_h // 40)
        surface.fill(upper, (col, top, 1, max(1, band_y - top)))
        surface.fill(stripe, (col, band_y, 1, stripe_h))
        surface.fill(lower, (col, band_y + stripe_h, 1,
                             max(1, top + line_h - band_y - stripe_h)))


def render_minimap(world, p: Player, pygame_module) -> "pygame.Surface":
    rows, cols = len(world), len(world[0])
    scale = 2
    surf = pygame_module.Surface((cols * scale, rows * scale))
    surf.set_alpha(210)
    surf.fill((10, 10, 10))
    for y, row in enumerate(world):
        for x, cell in enumerate(row):
            if cell == bg.FLOOR:
                surf.fill((90, 82, 40), (x * scale, y * scale, scale, scale))
    px, py = int(p.x * scale), int(p.y * scale)
    pygame_module.draw.circle(surf, (255, 60, 60), (px, py), 3)
    pygame_module.draw.line(
        surf, (255, 60, 60), (px, py),
        (px + int(math.cos(p.angle) * 8), py + int(math.sin(p.angle) * 8)), 1)
    return surf


def make_hum_sound(pygame_module) -> "pygame.mixer.Sound | None":
    """Synthesize the fluorescent hum-buzz: 120 Hz mains hum plus harmonics
    and a little noise. Returns None if the mixer is unavailable."""
    try:
        pygame_module.mixer.init(frequency=22050, size=-16, channels=1)
        rate = 22050
        rng = random.Random(0)
        samples = array("h")
        for i in range(rate):  # 1-second seamless loop
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

def main(argv: list[str] | None = None) -> None:
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
    ap.add_argument("--seconds", type=float, default=8.0, help="GIF length with --record")
    args = ap.parse_args(argv)

    if args.record:
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

    import pygame
    pygame.init()
    screen = pygame.display.set_mode(
        (INTERNAL_W * WINDOW_SCALE, INTERNAL_H * WINDOW_SCALE))
    clock = pygame.time.Clock()
    font = pygame.font.SysFont("menlo,consolas,monospace", 14)
    frame = pygame.Surface((INTERNAL_W, INTERNAL_H))

    hum = None if (args.mute or args.record) else make_hum_sound(pygame)
    if hum:
        hum.set_volume(0.35)
        hum.play(loops=-1)

    def new_world(seed: int | None):
        seed = random.randrange(2**32) if seed is None else seed
        world = build_world(seed, args.map_cols, args.map_rows)
        region = largest_floor_region(world)
        rng = random.Random(seed ^ 0xB4C4)
        sx, sy = rng.choice(sorted(region))
        # Face whichever direction has the longest sightline, so we start
        # looking down a corridor instead of into a wall.
        best_angle, best_dist = 0.0, -1.0
        for i in range(16):
            angle = i * math.tau / 16
            dist, _, _ = cast_ray(world, sx + 0.5, sy + 0.5,
                                  math.cos(angle), math.sin(angle))
            if dist > best_dist:
                best_angle, best_dist = angle, dist
        player = Player(sx + 0.5, sy + 0.5, best_angle)
        pygame.display.set_caption(f"The Backrooms — Level 0 — seed {seed}")
        return seed, world, player, rng

    seed, world, player, rng = new_world(args.seed)
    walker = AutoWalker(rng)
    auto = not args.manual or bool(args.record)
    show_map = False
    brightness = 1.0
    bg_brightness = -1.0
    background = None
    shift_timer = SHIFT_PERIOD
    recorded: list[bytes] = []
    record_frames = int(args.seconds * 15) if args.record else 0
    elapsed = 0.0

    running = True
    while running:
        dt = min(clock.tick(30) / 1000.0, 0.1) if not args.record else 1 / 30.0
        elapsed += dt

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
                    seed, world, player, rng = new_world(None)
                    walker = AutoWalker(rng)
                elif event.key == pygame.K_F12:
                    path = f"backrooms_walk_{seed}.png"
                    pygame.image.save(screen, path)
                    print(f"saved {path}")

        if auto:
            walker.update(world, player, dt)
        else:
            keys = pygame.key.get_pressed()
            turn = (keys[pygame.K_RIGHT] or keys[pygame.K_e]) - (keys[pygame.K_LEFT] or keys[pygame.K_q])
            player.angle += turn * TURN_SPEED * dt
            fwd = keys[pygame.K_w] - keys[pygame.K_s]
            strafe = keys[pygame.K_d] - keys[pygame.K_a]
            if fwd or strafe:
                dx = math.cos(player.angle) * fwd - math.sin(player.angle) * strafe
                dy = math.sin(player.angle) * fwd + math.cos(player.angle) * strafe
                mag = math.hypot(dx, dy) or 1.0
                player.move(world, dx / mag * MOVE_SPEED * dt,
                            dy / mag * MOVE_SPEED * dt)

        if not args.no_shift:
            shift_timer -= dt
            if shift_timer <= 0:
                shift_timer = SHIFT_PERIOD
                peripheral_shift(world, player.x, player.y, rng)

        # Fluorescent flicker: a jittery drift around full brightness with
        # the occasional deep dip.
        brightness += rng.uniform(-0.06, 0.06)
        brightness = min(1.02, max(0.88, brightness + (1.0 - brightness) * 0.2))
        if rng.random() < 0.006:
            brightness = 0.55

        if background is None or abs(brightness - bg_brightness) > 0.04:
            bg_brightness = brightness
            background = make_background(brightness, pygame)

        frame.blit(background, (0, 0))
        render_frame(frame, world, player, brightness, pygame)
        pygame.transform.scale(frame, screen.get_size(), screen)

        if show_map:
            screen.blit(render_minimap(world, player, pygame), (12, 12))
        hud = f"seed {seed}  {'AUTO' if auto else 'MANUAL'}  TAB=drive M=map R=new ESC=quit"
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
