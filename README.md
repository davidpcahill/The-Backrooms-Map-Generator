# The Backrooms Map Generator

Generates top-down maps that look like Level 0 of the Backrooms: endless
overlapping corridors, arbitrarily placed rooms, pillar halls, and odd
polygonal chambers. Built with Python and pygame.

> If you're not careful and you noclip out of reality in the wrong areas
> you'll end up in the Backrooms, where it's nothing but the stink of old
> moist carpet, the madness of mono-yellow, the endless background noise of
> fluorescent lights at maximum hum-buzz, and approximately six hundred
> million square miles of randomly segmented empty rooms to be trapped in.
> God save you if you hear something wandering around nearby, because it
> sure as hell has heard you.

Sources: [Backrooms Wiki — Level 0](https://backrooms.fandom.com/wiki/Level_0),
[Wikipedia — The Backrooms](https://en.wikipedia.org/wiki/The_Backrooms)

## 3D walkthrough

`backrooms_walk.py` renders the generated maps first-person with a software
raycaster and walks itself through Level 0:

![walkthrough](examples/walkthrough.gif)

```bash
python backrooms_walk.py                 # auto-walk demo (it drives)
python backrooms_walk.py --manual        # you drive (WASD + arrows)
python backrooms_walk.py --record demo.gif --seconds 8   # headless GIF (needs pillow)
```

Details pulled from Level 0 canon:

- worn mono-yellow wallpaper with a darker band low on the wall, brownish
  Berber carpet, low drop ceiling
- fluorescent hum synthesized at 120 Hz + harmonics (`--mute` to silence),
  with light flicker
- **Peripheral Shift** — the map quietly re-carves itself in areas you are
  not looking at, so retracing your steps never quite works. Watch it happen
  on the minimap (`M`). Disable with `--no-shift`.
- no entities. Level 0 is empty. That's the point.

| Key | Action |
| --- | --- |
| `TAB` | Toggle auto-walk / manual |
| `W A S D` | Move / strafe (manual) |
| Arrows / `Q` `E` | Turn |
| `M` | Toggle minimap |
| `R` | New map (new seed) |
| `F12` | Screenshot |
| `Esc` | Quit |

## Examples

Mono-yellow theme (`--seed 1234`):

![backrooms theme](examples/backrooms_1234.png)

Classic black & white (`--theme mono --seed 42`):

![mono theme](examples/mono_42.png)

Blueprint (`--theme blueprint --seed 7 --fill 0.6`):

![blueprint theme](examples/blueprint_7.png)

## Install & run

```bash
pip install -r requirements.txt
python backrooms_generator.py
```

Render straight to a PNG without opening a window:

```bash
python backrooms_generator.py --save map.png --seed 1234
```

Maps are fully deterministic per seed — share a seed and anyone can
regenerate the exact same map.

## Controls

| Key | Action |
| --- | --- |
| `R` | Regenerate with a new seed |
| `S` | Save the current map as `backrooms_<seed>.png` |
| `C` | Cycle color themes (backrooms / mono / blueprint) |
| `F` | Toggle fullscreen |
| `Q` / `Esc` | Quit |

The window title shows the current seed and theme.

## Options

```
--width N         window width in pixels (default 1280)
--height N        window height in pixels (default 720)
--cell N          cell size in pixels (default 8) — bigger = chunkier maps
--fill F          target floor fraction 0-1 (default 0.55) — higher = more open
--rooms N         rectangular rooms (default 3)
--pillar-rooms N  halls with pillar grids (default 2)
--poly-rooms N    irregular polygonal rooms (default 2)
--theme NAME      backrooms | mono | blueprint
--seed N          seed for reproducible maps
--save PATH       render to a PNG and exit
--fullscreen      start fullscreen
```

Finer knobs (layer budgets, merge probability, room size ranges, pillar
spacing) live in the `Config` dataclass at the top of
[backrooms_generator.py](backrooms_generator.py).

## How it works

1. **Corridors** — hundreds of small, partial mazes are carved with a
   growing-tree algorithm. Each starts at a random spot and runs out of
   budget before it can become an orderly labyrinth; where layers collide
   they randomly merge or stop dead. Overlaying them produces the
   trademark "randomly segmented" floor plan.
2. **Rooms** — rectangular rooms, irregular polygonal chambers, and pillar
   halls are stamped on top.
3. **Cleanup** — orphan floor specks stranded in solid wall are removed.
   Lone wall cells in open floor are deliberately kept: they read as
   pillars.

## Ideas / contributions welcome

- Textured walls (real wallpaper pattern instead of flat shading)
- Ceiling light panels via floor/ceiling casting
- Entity-free scares: distant footstep audio, lights going out in banks
- Export maps as JSON/Tiled for use in other engines
- Other levels (Level 1 parking garage, poolrooms, etc.)

## History

The original version of this project was written with ChatGPT in 2023.
It was rewritten from scratch in 2026 with Claude: seeded/reproducible
generation, a cleaner layered-maze algorithm, color themes, PNG export,
a headless CLI mode, and an actual frame limiter.
