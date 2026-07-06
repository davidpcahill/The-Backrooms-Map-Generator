#!/usr/bin/env python3
"""The Backrooms Map Generator.

Generates top-down maps that look like Level 0 of the Backrooms: endless
overlapping corridors, arbitrarily placed rooms, pillar halls, and odd
polygonal chambers, all rendered as floor/wall cells.

The map is built in layers on a single cell grid:

1.  Many small, partial mazes are carved with a growing-tree algorithm,
    each starting at a random spot and running out of budget before it
    can become an orderly labyrinth. Where layers collide they merge or
    stop at random, which produces the trademark "segmented nonsense"
    floor plan.
2.  Rectangular rooms, pillar halls, and irregular polygonal rooms are
    stamped on top.
3.  A cleanup pass removes single-cell debris.

Run interactively (a window opens):

    python backrooms_generator.py

Or render straight to a PNG without opening a window:

    python backrooms_generator.py --save map.png --seed 1234

Interactive controls:

    R      regenerate with a new seed
    S      save the current map as a PNG (backrooms_<seed>.png)
    C      cycle color themes
    F      toggle fullscreen
    Q/ESC  quit
"""

from __future__ import annotations

import argparse
import math
import random
from dataclasses import dataclass

# Grid cell values
WALL = 0
FLOOR = 1

THEMES: dict[str, dict[str, tuple[int, int, int]]] = {
    # Classic black & white, like the original version of this project.
    "mono": {"wall": (0, 0, 0), "floor": (255, 255, 255)},
    # Mono-yellow: damp carpet floors, shadowed walls.
    "backrooms": {"wall": (46, 40, 14), "floor": (222, 209, 128)},
    # Architectural blueprint.
    "blueprint": {"wall": (18, 44, 92), "floor": (214, 226, 244)},
}


@dataclass
class Config:
    """Everything that shapes a map. All sizes are in cells unless noted."""

    width: int = 1280            # window width in pixels
    height: int = 720            # window height in pixels
    cell_size: int = 8           # size of one grid cell in pixels
    fill: float = 0.55           # stop carving corridors at this floor fraction
    max_layers: int = 4000       # hard cap on maze layers (safety valve)
    layer_budget: tuple[int, int] = (30, 300)   # carved cells per maze layer
    merge_stop: float = 0.5      # chance a layer stops when it hits existing floor
    rooms: int = 3               # rectangular rooms
    room_size: tuple[int, int] = (4, 32)        # room width/height range
    pillar_rooms: int = 2        # rectangular halls with pillar grids
    pillar_room_size: tuple[int, int] = (10, 36)
    pillar_spacing: tuple[int, int] = (3, 6)
    poly_rooms: int = 2          # irregular polygonal rooms
    poly_sides: tuple[int, int] = (3, 9)
    poly_radius: tuple[int, int] = (4, 18)
    theme: str = "backrooms"
    seed: int | None = None      # None = random seed each generation
    fullscreen: bool = False
    save_path: str | None = None  # render to PNG and exit instead of opening a window

    @property
    def cols(self) -> int:
        return max(9, self.width // self.cell_size)

    @property
    def rows(self) -> int:
        return max(9, self.height // self.cell_size)


Grid = list[list[int]]


def new_grid(cfg: Config) -> Grid:
    return [[WALL] * cfg.cols for _ in range(cfg.rows)]


def floor_fraction(grid: Grid) -> float:
    total = len(grid) * len(grid[0])
    return sum(sum(row) for row in grid) / total


# ---------------------------------------------------------------------------
# Corridor carving
# ---------------------------------------------------------------------------

def carve_maze_layer(grid: Grid, cfg: Config, rng: random.Random) -> None:
    """Carve one partial maze with a growing-tree walk.

    Cells live on an even lattice two apart, with the wall cell between
    them carved when a passage opens. The walk gets a limited budget so a
    single layer never dominates the map; overlapping hundreds of these
    is what creates the Backrooms texture.
    """
    cols, rows = cfg.cols, cfg.rows
    start = (rng.randrange(0, cols - 1, 2), rng.randrange(0, rows - 1, 2))
    active = [start]
    visited = {start}
    budget = rng.randint(*cfg.layer_budget)

    while active and budget > 0:
        x, y = active[rng.randrange(len(active))]
        grid[y][x] = FLOOR

        neighbors = [
            (nx, ny)
            for nx, ny in ((x - 2, y), (x + 2, y), (x, y - 2), (x, y + 2))
            if 0 <= nx < cols and 0 <= ny < rows and (nx, ny) not in visited
        ]
        if not neighbors:
            active.remove((x, y))
            continue

        nx, ny = rng.choice(neighbors)
        visited.add((nx, ny))

        # Hitting a corridor carved by an earlier layer: sometimes join it,
        # sometimes stop dead. Both outcomes look right; the mix matters.
        if grid[ny][nx] == FLOOR and rng.random() < cfg.merge_stop:
            continue

        grid[(y + ny) // 2][(x + nx) // 2] = FLOOR
        grid[ny][nx] = FLOOR
        active.append((nx, ny))
        budget -= 1


def carve_corridors(grid: Grid, cfg: Config, rng: random.Random) -> None:
    """Layer partial mazes until the floor fraction target is reached."""
    for _ in range(cfg.max_layers):
        if floor_fraction(grid) >= cfg.fill:
            break
        carve_maze_layer(grid, cfg, rng)


# ---------------------------------------------------------------------------
# Rooms
# ---------------------------------------------------------------------------

def stamp_rect(grid: Grid, x: int, y: int, w: int, h: int, value: int = FLOOR) -> None:
    for row in range(y, y + h):
        for col in range(x, x + w):
            grid[row][col] = value


def random_rect(cfg: Config, rng: random.Random, size: tuple[int, int]) -> tuple[int, int, int, int]:
    w = rng.randint(*size)
    h = rng.randint(*size)
    w = min(w, cfg.cols - 2)
    h = min(h, cfg.rows - 2)
    x = rng.randint(1, cfg.cols - w - 1)
    y = rng.randint(1, cfg.rows - h - 1)
    return x, y, w, h


def carve_rooms(grid: Grid, cfg: Config, rng: random.Random) -> None:
    for _ in range(cfg.rooms):
        x, y, w, h = random_rect(cfg, rng, cfg.room_size)
        stamp_rect(grid, x, y, w, h)


def carve_pillar_rooms(grid: Grid, cfg: Config, rng: random.Random) -> None:
    for _ in range(cfg.pillar_rooms):
        x, y, w, h = random_rect(cfg, rng, cfg.pillar_room_size)
        stamp_rect(grid, x, y, w, h)

        spacing = rng.randint(*cfg.pillar_spacing)
        # Inset pillars so the hall keeps a clear walkway around its edge.
        for row in range(y + spacing, y + h - 1, spacing):
            for col in range(x + spacing, x + w - 1, spacing):
                grid[row][col] = WALL


def point_in_polygon(x: float, y: float, vertices: list[tuple[float, float]]) -> bool:
    inside = False
    j = len(vertices) - 1
    for i in range(len(vertices)):
        xi, yi = vertices[i]
        xj, yj = vertices[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def carve_poly_rooms(grid: Grid, cfg: Config, rng: random.Random) -> None:
    """Stamp irregular polygonal rooms — jittered angles and radii, so they
    read as strange chambers rather than tidy regular polygons."""
    for _ in range(cfg.poly_rooms):
        sides = rng.randint(*cfg.poly_sides)
        radius = rng.randint(*cfg.poly_radius)
        radius = min(radius, (min(cfg.cols, cfg.rows) - 4) // 2)
        cx = rng.randint(radius + 1, cfg.cols - radius - 2)
        cy = rng.randint(radius + 1, cfg.rows - radius - 2)

        step = math.tau / sides
        vertices = []
        for i in range(sides):
            angle = i * step + rng.uniform(-step / 4, step / 4)
            r = radius * rng.uniform(0.55, 1.0)
            vertices.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))

        for row in range(cy - radius, cy + radius + 1):
            for col in range(cx - radius, cx + radius + 1):
                if point_in_polygon(col + 0.5, row + 0.5, vertices):
                    grid[row][col] = FLOOR


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def cleanup(grid: Grid) -> None:
    """Remove orphan floor specks stranded in solid wall. Lone wall cells in
    open floor are deliberately kept — they read as pillars."""
    rows, cols = len(grid), len(grid[0])
    changes = []
    for y in range(rows):
        for x in range(cols):
            if grid[y][x] != FLOOR:
                continue
            neighbors = sum(
                grid[ny][nx]
                for ny in range(max(0, y - 1), min(rows, y + 2))
                for nx in range(max(0, x - 1), min(cols, x + 2))
                if (nx, ny) != (x, y)
            )
            if neighbors == 0:
                changes.append((x, y))
    for x, y in changes:
        grid[y][x] = WALL


# ---------------------------------------------------------------------------
# Generation + rendering
# ---------------------------------------------------------------------------

def generate(cfg: Config, seed: int) -> Grid:
    rng = random.Random(seed)
    grid = new_grid(cfg)
    carve_corridors(grid, cfg, rng)
    carve_rooms(grid, cfg, rng)
    carve_poly_rooms(grid, cfg, rng)
    # Pillar halls go last so nothing stamps over their pillars.
    carve_pillar_rooms(grid, cfg, rng)
    cleanup(grid)
    return grid


def render(grid: Grid, cfg: Config, surface: "pygame.Surface") -> None:
    import pygame

    colors = THEMES[cfg.theme]
    size = cfg.cell_size
    surface.fill(colors["wall"])
    floor = colors["floor"]
    for y, row in enumerate(grid):
        for x, cell in enumerate(row):
            if cell == FLOOR:
                pygame.draw.rect(surface, floor, (x * size, y * size, size, size))


def save_png(grid: Grid, cfg: Config, path: str) -> None:
    """Render the map to an image file without needing a window."""
    import pygame

    surface = pygame.Surface((cfg.cols * cfg.cell_size, cfg.rows * cfg.cell_size))
    render(grid, cfg, surface)
    pygame.image.save(surface, path)


# ---------------------------------------------------------------------------
# Interactive app
# ---------------------------------------------------------------------------

def run_window(cfg: Config) -> None:
    import pygame

    pygame.init()
    fullscreen = cfg.fullscreen
    screen = pygame.display.set_mode(
        (cfg.width, cfg.height), pygame.FULLSCREEN if fullscreen else 0
    )
    clock = pygame.time.Clock()
    theme_names = list(THEMES)

    map_surface = pygame.Surface((cfg.cols * cfg.cell_size, cfg.rows * cfg.cell_size))

    def rebuild(seed: int | None) -> int:
        seed = random.randrange(2**32) if seed is None else seed
        grid = generate(cfg, seed)
        render(grid, cfg, map_surface)
        pygame.display.set_caption(f"The Backrooms Generator — seed {seed} [{cfg.theme}]")
        rebuild.grid = grid
        return seed

    seed = rebuild(cfg.seed)

    running = True
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key in (pygame.K_ESCAPE, pygame.K_q):
                    running = False
                elif event.key == pygame.K_r:
                    seed = rebuild(None)
                elif event.key == pygame.K_s:
                    path = f"backrooms_{seed}.png"
                    pygame.image.save(map_surface, path)
                    print(f"saved {path}")
                elif event.key == pygame.K_c:
                    cfg.theme = theme_names[(theme_names.index(cfg.theme) + 1) % len(theme_names)]
                    render(rebuild.grid, cfg, map_surface)
                    pygame.display.set_caption(
                        f"The Backrooms Generator — seed {seed} [{cfg.theme}]"
                    )
                elif event.key == pygame.K_f:
                    fullscreen = not fullscreen
                    screen = pygame.display.set_mode(
                        (cfg.width, cfg.height), pygame.FULLSCREEN if fullscreen else 0
                    )

        screen.blit(map_surface, (0, 0))
        pygame.display.flip()
        clock.tick(30)

    pygame.quit()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> Config:
    defaults = Config()
    p = argparse.ArgumentParser(
        description="Generate Backrooms-style maps.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--width", type=int, default=defaults.width, help="window width in pixels")
    p.add_argument("--height", type=int, default=defaults.height, help="window height in pixels")
    p.add_argument("--cell", type=int, default=defaults.cell_size, help="cell size in pixels")
    p.add_argument("--fill", type=float, default=defaults.fill,
                   help="target floor fraction for corridor carving (0-1)")
    p.add_argument("--rooms", type=int, default=defaults.rooms, help="rectangular rooms")
    p.add_argument("--pillar-rooms", type=int, default=defaults.pillar_rooms,
                   help="halls with pillar grids")
    p.add_argument("--poly-rooms", type=int, default=defaults.poly_rooms,
                   help="irregular polygonal rooms")
    p.add_argument("--theme", choices=sorted(THEMES), default=defaults.theme)
    p.add_argument("--seed", type=int, default=None, help="seed for reproducible maps")
    p.add_argument("--save", metavar="PATH", default=None,
                   help="render to a PNG and exit without opening a window")
    p.add_argument("--fullscreen", action="store_true", help="start fullscreen")
    a = p.parse_args(argv)

    if not 0.0 < a.fill <= 1.0:
        p.error("--fill must be in (0, 1]")
    if a.cell < 1:
        p.error("--cell must be at least 1")

    return Config(
        width=a.width, height=a.height, cell_size=a.cell, fill=a.fill,
        rooms=a.rooms, pillar_rooms=a.pillar_rooms, poly_rooms=a.poly_rooms,
        theme=a.theme, seed=a.seed, fullscreen=a.fullscreen, save_path=a.save,
    )


def main(argv: list[str] | None = None) -> None:
    cfg = parse_args(argv)
    if cfg.save_path:
        seed = cfg.seed if cfg.seed is not None else random.randrange(2**32)
        grid = generate(cfg, seed)
        save_png(grid, cfg, cfg.save_path)
        print(f"seed {seed} -> {cfg.save_path}")
    else:
        run_window(cfg)


if __name__ == "__main__":
    main()
