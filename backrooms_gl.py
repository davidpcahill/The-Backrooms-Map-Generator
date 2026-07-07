#!/usr/bin/env python3
"""The Backrooms — ModernGL renderer. Found footage that looks real.

Same simulation as backrooms_walk.py (imported from it: the world, the
wanderer, the Presence, the synthesized audio, dying lights, Peripheral
Shift) — but rendered on the GPU:

- real triangle geometry: slopes, stairs, doorway lintels, pillars
- every live ceiling panel is a point light (nearest ~24 shaded per
  fragment); blackouts, dying lights, and the Presence's darkness trail
  reach the shader through a light-grid texture
- GL bloom, so fluorescents glow like fluorescents
- the Howler as a lit, fog-fading billboard with its baked walk cycle

And a camcorder lens, because the wanderer is *filming* this:

- auto-zoom: he zooms down long corridors to check the dark — and the
  zoom genuinely extends how far he can see (it feeds his perception)
- telephoto shake, focus breathing while the zoom moves
- auto-exposure that overshoots when he walks between dark and light
- barrel distortion, radial chromatic aberration, grain, scanlines, REC

Run:

    python backrooms_gl.py                  # auto-walk, fullscreen
    python backrooms_gl.py --manual
    python backrooms_gl.py --record demo.gif --seconds 12    # headless
    python backrooms_gl.py --frame shot.png

Controls are the same as backrooms_walk.py.
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sys

import numpy as np

import backrooms_walk as bw

RENDER_W, RENDER_H = 1280, 800
NEAR, FAR = 0.07, 40.0   # tighter near plane wrecks depth precision far out
MAX_LIGHTS = 24
CHUNK = 16
BASE_FOV = 62.0

TRIM_H = 0.345          # wall split height: lower paper + trim vs upper


# ---------------------------------------------------------------------------
# Shaders
# ---------------------------------------------------------------------------

SCENE_VS = """
#version 330
uniform mat4 mvp;
in vec3 in_pos; in vec2 in_uv; in vec3 in_norm; in float in_mat; in float in_shade;
out vec2 uv; out vec3 norm; out vec3 wpos; out float shade; flat out int mat;
void main() {
    gl_Position = mvp * vec4(in_pos, 1.0);
    uv = in_uv; norm = in_norm; wpos = in_pos; shade = in_shade;
    mat = int(in_mat + 0.5);
}
"""

SCENE_FS = """
#version 330
uniform sampler2DArray tex;
uniform sampler2D lightgrid;
uniform vec2 gridsize;
uniform vec3 cam;
uniform int nlights;
uniform vec3 lpos[24];
uniform vec3 lcol[24];
uniform vec3 fogcol;
uniform float flick;
in vec2 uv; in vec3 norm; in vec3 wpos; in float shade; flat in int mat;
out vec4 fragment;
void main() {
    vec3 base = texture(tex, vec3(uv, float(mat))).rgb;
    float cell_l = texture(lightgrid,
        vec2((wpos.x + 0.5) / gridsize.x, (wpos.z + 0.5) / gridsize.y)).r;
    vec3 c;
    if (mat == 3) {                        // fluorescent panel: emissive
        c = base * (0.10 + 1.55 * cell_l * flick);
    } else {
        // Two-sided lighting: faces are emitted once per boundary and
        // visible from both sides — flip the normal toward the viewer or
        // every backside (stair risers seen from above, fascias from
        // ledges, pit shafts) renders near-black and reads as a HOLE.
        vec3 N = (dot(cam - wpos, norm) < 0.0) ? -norm : norm;
        // THE lighting model that ended the 'holes' saga: base
        // illumination comes from the LIGHT GRID at any distance — a lit
        // corridor 30 cells away is a lit corridor, not a black void with
        // emissive panels floating in it. Point lights (gathered within
        // ~13 cells) only ADD local pooling under fixtures. Darkness is
        // reserved for blackout zones, its trail, dead banks (cell_l),
        // flicker, and fog — never for distance.
        vec3 warm = vec3(1.0, 0.965, 0.86);
        vec3 acc = vec3(0.05) + warm * (0.52 * cell_l);
        for (int i = 0; i < nlights; i++) {
            vec3 L = lpos[i] - wpos;
            float d = length(L);
            float att = 1.0 / (0.9 + 0.6 * d + 0.38 * d * d);
            float nl = max(dot(normalize(L), N), 0.0);
            acc += lcol[i] * att * (nl * 0.9 + 0.12);
        }
        c = base * acc * shade;
    }
    float fd = length(wpos - cam);
    float fog = 1.0 - exp(-fd * 0.10);
    fragment = vec4(mix(c, fogcol, clamp(fog, 0.0, 1.0)), 1.0);
}
"""

SPRITE_VS = """
#version 330
uniform mat4 mvp;
in vec3 in_pos; in vec2 in_uv;
out vec2 uv; out vec3 wpos;
void main() { gl_Position = mvp * vec4(in_pos, 1.0); uv = in_uv; wpos = in_pos; }
"""

SPRITE_FS = """
#version 330
uniform sampler2DArray tex;
uniform float layer;
uniform vec3 cam;
uniform vec3 fogcol;
uniform float cell_l;
uniform int mode;        // 0 sprite, 1 contact blob, 2 projected shadow
uniform float shadow_k;
in vec2 uv; in vec3 wpos;
out vec4 fragment;
void main() {
    vec4 t = (mode == 1)
        ? vec4(0.0, 0.0, 0.0, 0.55 * (1.0 - length(uv * 2.0 - 1.0)))
        : texture(tex, vec3(uv, layer));
    if (t.a < 0.03) discard;
    float fd = length(wpos - cam);
    float fog = 1.0 - exp(-fd * 0.10);
    if (mode == 2) {     // its shadow, thrown across the floor by a light
        fragment = vec4(0.0, 0.0, 0.0, t.a * shadow_k * (1.0 - clamp(fog, 0.0, 1.0)));
        return;
    }
    // it is a black thing; light barely finds purchase on it
    vec3 c = t.rgb * 0.40 * (0.30 + 0.70 * cell_l);
    fragment = vec4(mix(c, fogcol, clamp(fog, 0.0, 1.0)), t.a * (1.0 - fog * 0.85));
}
"""

QUAD_VS = """
#version 330
in vec2 in_pos;
out vec2 uv;
void main() { uv = in_pos * 0.5 + 0.5; gl_Position = vec4(in_pos, 0.0, 1.0); }
"""

OVERLAY_VS = """
#version 330
uniform vec4 rect;      // x0, y0 (bottom-left, NDC), w, h
in vec2 in_pos;         // unit quad 0..1
out vec2 uv;
void main() {
    uv = vec2(in_pos.x, 1.0 - in_pos.y);   // pygame surfaces are top-down
    gl_Position = vec4(rect.xy + in_pos * rect.zw, 0.0, 1.0);
}
"""

OVERLAY_FS = """
#version 330
uniform sampler2D tex;
uniform float alpha;
in vec2 uv; out vec4 fragment;
void main() {
    vec4 t = texture(tex, uv);
    fragment = vec4(t.rgb, t.a * alpha);
}
"""

BRIGHT_FS = """
#version 330
uniform sampler2D scene;
in vec2 uv; out vec4 fragment;
void main() {
    vec3 c = texture(scene, uv).rgb;
    float lum = dot(c, vec3(0.33));
    fragment = vec4(c * smoothstep(0.82, 1.05, lum), 1.0);
}
"""

BLUR_FS = """
#version 330
uniform sampler2D src;
uniform vec2 dir;
in vec2 uv; out vec4 fragment;
void main() {
    vec3 a = vec3(0.0);
    float w[5] = float[](0.227, 0.194, 0.121, 0.054, 0.016);
    a += texture(src, uv).rgb * w[0];
    for (int i = 1; i < 5; i++) {
        a += texture(src, uv + dir * float(i)).rgb * w[i];
        a += texture(src, uv - dir * float(i)).rgb * w[i];
    }
    fragment = vec4(a, 1.0);
}
"""

COMPOSITE_FS = """
#version 330
uniform sampler2D scene;
uniform sampler2D bloom;
uniform float exposure;
uniform float time;
uniform float fear;
uniform float grain_amt;
uniform float tear;      // y center of a tracking tear, <0 = none
uniform float blood;     // blood on the lens, 0..1
uniform float static_amt;
uniform vec2 res;
in vec2 uv; out vec4 fragment;

float hash(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5); }

void main() {
    vec2 c = uv * 2.0 - 1.0;
    float r2 = dot(c, c);
    vec2 d = c * (1.0 + 0.045 * r2 + 0.02 * r2 * r2);   // barrel distortion
    vec2 suv = d * 0.5 + 0.5;
    if (tear > 0.0 && abs(uv.y - tear) < 0.012)
        suv.x += 0.02 + 0.03 * hash(vec2(time, uv.y));
    // radial chromatic aberration
    float ca = 0.0016 + 0.0026 * r2;
    vec3 col;
    col.r = texture(scene, suv + c * ca).r;
    col.g = texture(scene, suv).g;
    col.b = texture(scene, suv - c * ca).b;
    col += texture(bloom, suv).rgb * 0.55;
    col *= exposure;
    col = col / (col + 0.85) * 1.28;                     // filmic-ish
    float vig = 1.0 - (0.32 + 0.38 * fear) * r2;
    col *= clamp(vig, 0.0, 1.0);
    float g = (hash(uv * res + vec2(time * 61.7, time * 12.3)) - 0.5);
    col += g * grain_amt;
    col *= 1.0 - 0.05 * step(0.5, fract(uv.y * res.y * 0.5)); // scanlines
    if (suv.x < 0.0 || suv.x > 1.0 || suv.y < 0.0 || suv.y > 1.0) col = vec3(0.0);
    // blood on the lens: dark red splatter blobs, drying at the edges
    if (blood > 0.003) {
        vec2 bp[6] = vec2[](vec2(0.14, 0.22), vec2(0.87, 0.33), vec2(0.28, 0.86),
                            vec2(0.74, 0.82), vec2(0.52, 0.08), vec2(0.05, 0.6));
        float m = 0.0;
        for (int k = 0; k < 6; k++) {
            vec2 dv = (uv - bp[k]) * vec2(res.x / res.y, 1.0);
            float ang = atan(dv.y, dv.x);
            // slightly irregular edges — but blobs, not paint stars
            float lump = 0.88 + 0.16 * hash(vec2(float(k), floor(ang * 1.6)))
                       + 0.05 * sin(ang * 3.0 + float(k) * 9.0);
            float rr = (0.09 + 0.15 * hash(bp[k] * 7.3)) * lump;
            m += smoothstep(rr, rr * 0.3, length(dv))
                 * (0.7 + 0.3 * hash(uv * 40.0 + bp[k]));
        }
        m = clamp(m, 0.0, 1.0) * blood;
        col = mix(col, vec3(0.28, 0.012, 0.008) * (0.4 + 0.6 * dot(col, vec3(0.5))), m * 0.94);
    }
    if (static_amt > 0.0) {
        col = mix(col, vec3(hash(uv * res + vec2(time * 151.0, time * 77.0))),
                  static_amt * 0.92);
    }
    fragment = vec4(col, 1.0);
}
"""


# ---------------------------------------------------------------------------
# Matrices
# ---------------------------------------------------------------------------

def perspective(fov_deg, aspect, near, far):
    f = 1.0 / math.tan(math.radians(fov_deg) / 2)
    m = np.zeros((4, 4), dtype=np.float32)
    m[0, 0] = f / aspect
    m[1, 1] = f
    m[2, 2] = (far + near) / (near - far)
    m[2, 3] = -1.0
    m[3, 2] = 2 * far * near / (near - far)
    return m


def view_matrix(pos, yaw, pitch, roll=0.0):
    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    fwd = np.array([cy * cp, sp, sy * cp], dtype=np.float32)
    up0 = np.array([0, 1, 0], dtype=np.float32)
    right = np.cross(fwd, up0)
    right /= np.linalg.norm(right)
    up = np.cross(right, fwd)
    if roll:
        cr, sr = math.cos(roll), math.sin(roll)
        right, up = right * cr + up * sr, up * cr - right * sr
    m = np.identity(4, dtype=np.float32)
    m[0, :3] = [right[0], up[0], -fwd[0]]
    m[1, :3] = [right[1], up[1], -fwd[1]]
    m[2, :3] = [right[2], up[2], -fwd[2]]
    m[3, 0] = -np.dot(right, pos)
    m[3, 1] = -np.dot(up, pos)
    m[3, 2] = np.dot(fwd, pos)
    return m


# ---------------------------------------------------------------------------
# Procedural textures (uploaded as a 2D array)
# ---------------------------------------------------------------------------

def build_textures(ctx, pygame_module):
    """Layers: 0 wall upper, 1 floor, 2 ceiling tile, 3 panel,
    4 pit/shaft, 5 wall lower + chair-rail trim."""
    S = bw.STYLE
    size = 256
    rng = random.Random(12)
    surfs = []

    def new(color):
        s = pygame_module.Surface((size, size))
        s.fill(color)
        return s

    def speckle(s, base, n, dmin, dmax, ln=2):
        for _ in range(n):
            d = rng.randint(dmin, dmax)
            col = tuple(min(255, max(0, c - d)) for c in base)
            s.fill(col, (rng.randrange(size), rng.randrange(size), 1, rng.randint(1, ln)))

    # 0: upper wallpaper (stripes) or concrete
    wall = new(S["wall_upper"])
    if S["kind"] == "wallpaper":
        for x0 in range(0, size, 32):
            wall.fill(tuple(max(0, c - 10) for c in S["wall_upper"]), (x0, 0, 16, size))
        for x0 in range(0, size, 16):
            wall.fill(tuple(max(0, c - 15) for c in S["wall_upper"]), (x0, 0, 1, size))
    else:
        for y0 in range(0, size, 128):
            wall.fill(tuple(max(0, c - 16) for c in S["wall_upper"]), (0, y0, size, 3))
    speckle(wall, S["wall_upper"], 2200, 4, 12)
    surfs.append(wall)

    # 1: carpet / concrete floor
    floor = new(S["carpet"])
    speckle(floor, S["carpet"], 5200, 3, 16, 3)
    surfs.append(floor)

    # 2: ceiling tile with grid seams
    ceil = new(S["ceil_tile"])
    for k in range(0, size, 128):
        ceil.fill(tuple(max(0, c - 22) for c in S["ceil_tile"]), (k, 0, 2, size))
        ceil.fill(tuple(max(0, c - 22) for c in S["ceil_tile"]), (0, k, size, 2))
    speckle(ceil, S["ceil_tile"], 1300, 3, 9)
    surfs.append(ceil)

    # 3: fluorescent panel: bright diffuser with a frame
    panel = new(tuple(min(255, c + 18) for c in S["light_panel"]))
    panel.fill(tuple(max(0, c - 70) for c in S["light_panel"]), (0, 0, size, 8))
    panel.fill(tuple(max(0, c - 70) for c in S["light_panel"]), (0, size - 8, size, 8))
    panel.fill(tuple(max(0, c - 70) for c in S["light_panel"]), (0, 0, 8, size))
    panel.fill(tuple(max(0, c - 70) for c in S["light_panel"]), (size - 8, 0, 8, size))
    for x0 in range(24, size, 48):
        panel.fill(tuple(max(0, c - 26) for c in S["light_panel"]), (x0, 8, 4, size - 16))
    surfs.append(panel)

    # 4: pit shaft / bottom
    pit = new(S["pit_shaft"])
    speckle(pit, S["pit_shaft"], 2600, 4, 14, 4)
    surfs.append(pit)

    # 5: lower wallpaper with the trim rail baked at the top edge
    lower = new(S["wall_lower"])
    speckle(lower, S["wall_lower"], 1600, 3, 10)
    trim_rows = int(size * (0.045 / TRIM_H))
    lower.fill(S["wall_trim"], (0, 0, size, trim_rows))
    lower.fill(tuple(min(255, c + 26) for c in S["wall_trim"]), (0, 0, size, 2))
    surfs.append(lower)

    # 6: a wooden door — frame, two recessed panels, a knob
    wood = (118, 84, 52)
    door = new(wood)
    speckle(door, wood, 1800, 4, 14, 4)
    dark = tuple(max(0, c - 34) for c in wood)
    door.fill(dark, (0, 0, size, 10))
    door.fill(dark, (0, size - 10, size, 10))
    door.fill(dark, (0, 0, 10, size))
    door.fill(dark, (size - 10, 0, 10, size))
    for py_, ph_ in ((34, 82), (140, 82)):
        door.fill(dark, (40, py_, size - 80, 6))
        door.fill(dark, (40, py_ + ph_, size - 80, 6))
        door.fill(dark, (40, py_, 6, ph_))
        door.fill(dark, (size - 46, py_, 6, ph_))
    door.fill((200, 180, 120), (size - 34, 118, 10, 10))   # knob
    surfs.append(door)

    data = b"".join(
        pygame_module.image.tobytes(s, "RGBA") for s in surfs)
    tex = ctx.texture_array((size, size, len(surfs)), 4, data)
    tex.build_mipmaps()
    tex.filter = (0x2703, 0x2601)   # LINEAR_MIPMAP_LINEAR, LINEAR
    tex.anisotropy = 8.0
    return tex


# ---------------------------------------------------------------------------
# World mesh (chunked)
# ---------------------------------------------------------------------------

class WorldMesh:
    def __init__(self, ctx, prog, world: bw.World):
        self.ctx = ctx
        self.prog = prog
        self.world = world
        self.nx = (world.cols + CHUNK - 1) // CHUNK
        self.ny = (world.rows + CHUNK - 1) // CHUNK
        self.chunks = {}
        for cy in range(self.ny):
            for cx in range(self.nx):
                self._build(cx, cy)

    def rebuild_cells(self, cells):
        # A changed cell alters the wall faces of its NEIGHBORS too — and
        # those may live in adjacent chunks. Missing this is how see-through
        # holes accumulate at chunk borders as the Shift keeps carving.
        w = self.world
        expanded = set()
        for x, y in cells:
            for nx, ny in ((x, y), (x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if 0 <= nx < w.cols and 0 <= ny < w.rows:
                    expanded.add((nx, ny))
        dirty = {(x // CHUNK, y // CHUNK) for x, y in expanded}
        for cx, cy in dirty:
            self._build(cx, cy)

    def _build(self, cx, cy):
        w = self.world
        verts = []
        emit = verts.extend

        def fh(x, y, px, pz):
            """Floor height of cell (x,y) evaluated at world point (px,pz)."""
            xi, yi = x % w.cols, y % w.rows
            base = w.floor[yi][xi]
            gx, gy = w.gx[yi][xi], w.gy[yi][xi]
            if gx or gy:
                base += gx * (px - x - 0.5) + gy * (pz - y - 0.5)
            return base

        # NOTE: no corner welding. Every clever cross-cell corner scheme
        # tried here produced either hairline cracks (per-cell averaging)
        # or degenerate wedge spikes (cluster means at 3-level corners).
        # Instead: floors use their own plane, and every edge gets ONE
        # slightly-oversized sealing rectangle covering the full height
        # mismatch. Rectangles cannot wedge; overlap cannot crack.

        def quad(p1, p2, p3, p4, uvs, n, mat, sh=(1, 1, 1, 1)):
            a = (*p1, *uvs[0], *n, mat, sh[0])
            b = (*p2, *uvs[1], *n, mat, sh[1])
            c = (*p3, *uvs[2], *n, mat, sh[2])
            d = (*p4, *uvs[3], *n, mat, sh[3])
            emit(a); emit(b); emit(c); emit(a); emit(c); emit(d)

        def wall(x0, z0, x1, z1, h0, h1, nrm, base_floor, pit=False):
            """Vertical wall from h0 (bottom) to h1 (top) along segment.
            Split at the trim height above base_floor; lower gets mat 5.
            Ends are extended slightly past the corners so the 3mm insets
            of adjacent faces overlap — otherwise rays can graze through
            the diagonal micro-gap where two solid cells meet at a corner."""
            if h1 - h0 < 1e-4:
                return
            seg_l = math.hypot(x1 - x0, z1 - z0)
            if seg_l > 1e-6:
                ux_, uz_ = (x1 - x0) / seg_l, (z1 - z0) / seg_l
                x0 -= ux_ * 0.015; z0 -= uz_ * 0.015
                x1 += ux_ * 0.015; z1 += uz_ * 0.015
            ulen = math.hypot(x1 - x0, z1 - z0)
            split = base_floor + TRIM_H
            mats = []
            if pit:
                mats = [(h0, h1, 4, None)]
            else:
                if h0 < split:
                    mats.append((h0, min(h1, split), 5, base_floor))
                if h1 > split:
                    mats.append((max(h0, split), h1, 0, None))
            for b0, b1, mat, bf in mats:
                if mat == 5:
                    v0 = (split - b0) / TRIM_H
                    v1 = (split - b1) / TRIM_H
                elif mat == 4:
                    v0, v1 = b0, b1
                else:
                    v0, v1 = b0, b1
                sh_b = 0.82 if b0 <= base_floor + 0.02 else 1.0
                quad((x0, b0, z0), (x1, b0, z1), (x1, b1, z1), (x0, b1, z0),
                     ((0, v0), (ulen, v0), (ulen, v1), (0, v1)),
                     nrm, mat, (sh_b, sh_b, 1.0, 1.0))

        x_lo, x_hi = cx * CHUNK, min((cx + 1) * CHUNK, w.cols)
        y_lo, y_hi = cy * CHUNK, min((cy + 1) * CHUNK, w.rows)
        for y in range(y_lo, y_hi):
            for x in range(x_lo, x_hi):
                f, c = w.floor[y][x], w.ceil[y][x]
                if f >= c:
                    continue    # solid: faces drawn by open neighbors
                # floor: this cell's own plane, no cross-cell blending
                h00 = fh(x, y, x, y)
                h10 = fh(x, y, x + 1, y)
                h11 = fh(x, y, x + 1, y + 1)
                h01 = fh(x, y, x, y + 1)
                gx, gy = w.gx[y][x], w.gy[y][x]
                n = np.array([-gx, 1.0, -gy]); n = n / np.linalg.norm(n)
                pit_floor = f <= bw.PIT_FLOOR + 0.01
                quad((x, h00, y), (x + 1, h10, y), (x + 1, h11, y + 1), (x, h01, y + 1),
                     ((x, y), (x + 1, y), (x + 1, y + 1), (x, y + 1)),
                     tuple(n), 4 if pit_floor else 1)
                # ceiling (panel or tile)
                mat = 3 if w.panel[y][x] else 2
                uv = ((0, 0), (1, 0), (1, 1), (0, 1)) if mat == 3 else \
                     ((x * 0.5, y * 0.5), ((x + 1) * 0.5, y * 0.5),
                      ((x + 1) * 0.5, (y + 1) * 0.5), (x * 0.5, (y + 1) * 0.5))
                quad((x, c, y + 1), (x + 1, c, y + 1), (x + 1, c, y), (x, c, y),
                     uv, (0, -1, 0), mat)
                # walls to the 4 neighbors
                for dx, dy, nrm, seg in (
                        (1, 0, (-1, 0, 0), ((x + 1, y + 1), (x + 1, y))),
                        (-1, 0, (1, 0, 0), ((x, y), (x, y + 1))),
                        (0, 1, (0, 0, -1), ((x, y + 1), (x + 1, y + 1))),
                        (0, -1, (0, 0, 1), ((x + 1, y), (x, y)))):
                    outside = not (0 <= x + dx < w.cols and 0 <= y + dy < w.rows)
                    nxi, nyi = (x + dx) % w.cols, (y + dy) % w.rows
                    nf, nc = w.floor[nyi][nxi], w.ceil[nyi][nxi]
                    if outside:
                        nf, nc = 0.0, 0.0   # beyond the border: sealed
                    (ax, az), (bx, bz) = seg
                    # THE RULE (one rule, no special cases): this cell seals
                    # its own edge. The shared open window between me and
                    # the neighbor is [max(floors), min(ceils)]; I draw my
                    # edge face everywhere outside that window, inset a
                    # hair into my own cell. Both cells do this
                    # independently, so a viewable edge without a wall
                    # cannot exist — regardless of chunks, shifts, or
                    # which neighbor changed.
                    ix, iz = ax + nrm[0] * 0.003, az + nrm[2] * 0.003
                    jx, jz = bx + nrm[0] * 0.003, bz + nrm[2] * 0.003
                    ma = fh(x, y, ax, az)
                    mb = fh(x, y, bx, bz)
                    my_lo = min(ma, mb)
                    if nf >= nc:        # solid (or border): full face
                        # border faces cap high — a low-ceiling cell at the
                        # map edge must not let taller neighbors see over
                        # its wall into the void
                        top = max(c, 3.6) if outside else c
                        wall(ix, iz, jx, jz, my_lo - 0.02, top, nrm, my_lo)
                    else:
                        ta = fh(x + dx, y + dy, ax, az)
                        tb = fh(x + dx, y + dy, bx, bz)
                        win_lo = max(ma, mb, ta, tb)
                        win_hi = min(c, nc)
                        if win_hi <= win_lo:            # no shared opening
                            wall(ix, iz, jx, jz, my_lo - 0.02, c, nrm, my_lo)
                        else:
                            # bottom skirt: my floor up to the window
                            if win_lo - my_lo > 0.045:
                                deep = (my_lo <= bw.PIT_FLOOR + 0.01
                                        and win_lo - my_lo > 1.2)
                                wall(ix, iz, jx, jz, my_lo - 0.02,
                                     win_lo + 0.01, nrm, my_lo, pit=deep)
                            # top skirt: the window up to my ceiling
                            if c - win_hi > 0.003:
                                wall(ix, iz, jx, jz, win_hi, c, nrm, my_lo)

        key = (cx, cy)
        old = self.chunks.pop(key, None)
        if old:
            old[0].release()
            old[1].release()
        if not verts:
            return
        data = np.array(verts, dtype=np.float32)
        vbo = self.ctx.buffer(data.tobytes())
        vao = self.ctx.vertex_array(
            self.prog, [(vbo, "3f 2f 3f 1f 1f",
                         "in_pos", "in_uv", "in_norm", "in_mat", "in_shade")])
        self.chunks[key] = (vbo, vao, len(data) // 10)

    def render(self):
        for vbo, vao, n in self.chunks.values():
            vao.render()


# ---------------------------------------------------------------------------
# Camcorder
# ---------------------------------------------------------------------------

class Camcorder:
    """The wanderer films everything. Zoom is how he checks the dark."""

    def __init__(self, rng):
        self.rng = rng
        self.zoom = 1.0
        self.zoom_target = 1.0
        self.scan_timer = rng.uniform(8.0, 18.0)
        self.hold = 0.0
        self.shake_t = rng.uniform(0, 100)
        self.pitch = 0.0

    def update(self, dt, p, walker, world):
        self.shake_t += dt
        # Investigate-zoom: he heard something, he's looking — punch in.
        investigating = (getattr(walker, "look", 0) > 0
                         or (p.presence_heard and not p.presence_seen))
        if p.fear > 0.55:
            self.zoom_target = 1.0           # no one zooms while running
            self.hold = 0.0
        elif investigating:
            self.zoom_target = 2.4
            self.hold = 0.6
        elif self.hold > 0:
            self.hold -= dt
            if self.hold <= 0:
                self.zoom_target = 1.0
        else:
            self.scan_timer -= dt
            if self.scan_timer <= 0:
                # Curiosity: zoom down whatever is ahead to see more.
                self.scan_timer = self.rng.uniform(14.0, 34.0)
                self.zoom_target = self.rng.uniform(1.8, 3.1)
                self.hold = self.rng.uniform(1.2, 2.4)

        rate = 3.2 if self.zoom_target > self.zoom else 2.0
        self.zoom += (self.zoom_target - self.zoom) * min(1.0, rate * dt)
        p.zoom_boost = 1.0 + (self.zoom - 1.0) * 0.9

        # Handheld: subtle at wide, obvious at telephoto, ragged with fear.
        t = self.shake_t
        amp = (0.0016 + 0.004 * (self.zoom - 1.0) + 0.006 * p.fear)
        sy = (math.sin(t * 1.7) * 0.6 + math.sin(t * 3.9 + 1.3) * 0.3
              + math.sin(t * 9.2 + 4.1) * 0.1) * amp
        sp = (math.sin(t * 1.3 + 2.2) * 0.6 + math.sin(t * 4.6) * 0.3
              + math.sin(t * 11.0 + 0.7) * 0.1) * amp
        # focus breathing while the zoom is moving
        breathing = abs(self.zoom_target - self.zoom) * 1.6
        fov = BASE_FOV / self.zoom * (1.0 + math.sin(t * 6.0) * 0.015 * breathing)
        self.pitch += (sp - self.pitch) * min(1.0, 6.0 * dt)
        # handheld roll noise: no human keeps a camera level
        rn = (math.sin(t * 1.1 + 0.9) * 0.6 + math.sin(t * 3.1 + 2.7) * 0.3
              + math.sin(t * 7.7) * 0.1) * amp * 1.6
        return fov, sy, self.pitch, rn


class Exposure:
    """A camcorder's AE: adapts with a lag, so dark-to-light overshoots."""

    def __init__(self):
        self.ev = 1.0

    def update(self, dt, world, p, flick):
        xi, yi = int(p.x) % world.cols, int(p.y) % world.rows
        acc = 0.0
        n = 0
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                acc += world.light[(yi + dy) % world.rows][(xi + dx) % world.cols]
                n += 1
        avg = acc / n
        target = max(0.8, min(1.5, 0.85 / max(avg, 0.4)))
        self.ev += (target - self.ev) * min(1.0, 1.6 * dt)
        return self.ev * (0.75 + 0.25 * flick)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

def gather_lights(world, p, flick):
    """Nearest live panels become point lights."""
    xi, yi = int(p.x), int(p.y)
    found = []
    for dy in range(-13, 14):
        for dx in range(-13, 14):
            x, y = (xi + dx) % world.cols, (yi + dy) % world.rows
            if world.panel[y][x] and world.light[y][x] > 0.3:
                d2 = dx * dx + dy * dy
                found.append((d2, x, y))
    found.sort()
    lpos = np.zeros((MAX_LIGHTS, 3), dtype=np.float32)
    lcol = np.zeros((MAX_LIGHTS, 3), dtype=np.float32)
    n = min(MAX_LIGHTS, len(found))
    warm = np.array([1.0, 0.96, 0.82], dtype=np.float32)
    for i in range(n):
        _, x, y = found[i]
        lpos[i] = (x + 0.5, world.ceil[y][x] - 0.06, y + 0.5)
        lcol[i] = warm * (1.0 * world.light[y][x] * flick)
    return n, lpos, lcol


def load_sprite_layers(ctx, pygame_module):
    """Howler sheet -> texture array, frames padded bottom-center so the
    ground contact is consistent. Returns (tex, layer_count) or None."""
    try:
        sheet = pygame_module.image.load(
            bw.resource_path("assets/bacteria_sheet.png"))
    except Exception:
        return None
    fw, fh = sheet.get_width() // 8, sheet.get_height() // 8
    frames = []
    max_w = max_h = 1
    rects = []
    for a in range(8):
        for p_ in range(8):
            f = sheet.subsurface((p_ * fw, a * fh, fw, fh))
            r = f.get_bounding_rect(min_alpha=8)
            rects.append((f, r))
            max_w = max(max_w, r.width)
            max_h = max(max_h, r.height)
    data = b""
    for f, r in rects:
        canvas = pygame_module.Surface((max_w, max_h), pygame_module.SRCALPHA)
        canvas.blit(f, ((max_w - r.width) // 2, max_h - r.height), r)
        canvas = pygame_module.transform.flip(canvas, False, True)
        data += pygame_module.image.tobytes(canvas, "RGBA")
    tex = ctx.texture_array((max_w, max_h, 64), 4, data)
    tex.filter = (0x2601, 0x2601)   # LINEAR
    return tex, max_w / max_h


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="The Backrooms, on the GPU, through a camcorder.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--level", type=int, choices=(0, 1), default=0)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--map-cols", type=int, default=120)
    ap.add_argument("--map-rows", type=int, default=80)
    ap.add_argument("--manual", action="store_true")
    ap.add_argument("--windowed", action="store_true")
    ap.add_argument("--no-shift", action="store_true")
    ap.add_argument("--no-entity", action="store_true")
    ap.add_argument("--mute", action="store_true")
    ap.add_argument("--record", metavar="GIF", default=None)
    ap.add_argument("--seconds", type=float, default=12.0)
    ap.add_argument("--frame", metavar="PNG", default=None)
    ap.add_argument("--spawn-zone",
                    choices=("tall", "crawl", "pit", "stairs", "ramp"), default=None)
    ap.add_argument("--test-death", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--debug-overlay", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--autoshot", metavar="DIR", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--auto-quit", type=float, default=None, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    bw.apply_style(args.level)
    headless = bool(args.record or args.frame)
    if headless:
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

    import pygame
    import moderngl
    pygame.init()

    if headless:
        ctx = moderngl.create_standalone_context()
        screen_size = (RENDER_W, RENDER_H)
        window_fbo = None
    else:
        pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MAJOR_VERSION, 3)
        pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MINOR_VERSION, 3)
        pygame.display.gl_set_attribute(
            pygame.GL_CONTEXT_PROFILE_MASK, pygame.GL_CONTEXT_PROFILE_CORE)
        flags = pygame.OPENGL | pygame.DOUBLEBUF
        if not args.windowed:
            flags |= pygame.FULLSCREEN
        pygame.display.set_mode((0, 0) if not args.windowed else (1280, 800), flags)
        try:
            pygame.display.set_icon(
                pygame.image.load(bw.resource_path("assets/icon.png")))
        except Exception:
            pass
        ctx = moderngl.create_context()
        window_fbo = ctx.detect_framebuffer()
        screen_size = window_fbo.size
    pygame.display.set_caption("The Backrooms — found footage")

    W, H = screen_size
    scene_prog = ctx.program(vertex_shader=SCENE_VS, fragment_shader=SCENE_FS)
    sprite_prog = ctx.program(vertex_shader=SPRITE_VS, fragment_shader=SPRITE_FS)
    bright_prog = ctx.program(vertex_shader=QUAD_VS, fragment_shader=BRIGHT_FS)
    blur_prog = ctx.program(vertex_shader=QUAD_VS, fragment_shader=BLUR_FS)
    comp_prog = ctx.program(vertex_shader=QUAD_VS, fragment_shader=COMPOSITE_FS)

    quad = ctx.buffer(np.array(
        [-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1], dtype=np.float32).tobytes())
    q_bright = ctx.vertex_array(bright_prog, [(quad, "2f", "in_pos")])
    q_blur = ctx.vertex_array(blur_prog, [(quad, "2f", "in_pos")])
    q_comp = ctx.vertex_array(comp_prog, [(quad, "2f", "in_pos")])

    overlay_prog = ctx.program(vertex_shader=OVERLAY_VS, fragment_shader=OVERLAY_FS)
    unit = ctx.buffer(np.array(
        [0, 0, 1, 0, 0, 1, 0, 1, 1, 0, 1, 1], dtype=np.float32).tobytes())
    q_overlay = ctx.vertex_array(overlay_prog, [(unit, "2f", "in_pos")])
    overlay_texs = {}

    def draw_overlay(name, surf, x_px, y_px, alpha):
        """Blit a pygame surface into the frame at pixel coords (top-left)."""
        w_px, h_px = surf.get_size()
        data = pygame.image.tobytes(surf, "RGBA")
        tex = overlay_texs.get(name)
        if tex is None or tex.size != (w_px, h_px):
            if tex is not None:
                tex.release()
            tex = ctx.texture((w_px, h_px), 4)
            tex.filter = (0x2601, 0x2601)
            overlay_texs[name] = tex
        tex.write(data)
        tex.use(0)
        overlay_prog["tex"].value = 0
        overlay_prog["alpha"].value = alpha
        overlay_prog["rect"].value = (
            -1.0 + 2.0 * x_px / W,
            1.0 - 2.0 * (y_px + h_px) / H,
            2.0 * w_px / W,
            2.0 * h_px / H)
        q_overlay.render()

    scene_tex = ctx.texture((W, H), 4)
    scene_depth = ctx.depth_renderbuffer((W, H))
    scene_fbo = ctx.framebuffer([scene_tex], scene_depth)
    bw_, bh_ = W // 4, H // 4
    ping_t = ctx.texture((bw_, bh_), 4)
    pong_t = ctx.texture((bw_, bh_), 4)
    ping = ctx.framebuffer([ping_t])
    pong = ctx.framebuffer([pong_t])
    out_tex = ctx.texture((W, H), 4)
    out_fbo = ctx.framebuffer([out_tex])

    def new_world(seed):
        seed = random.randrange(2**32) if seed is None else seed
        world = bw.World(seed, args.map_cols, args.map_rows)
        world.bounded = True    # the mesh doesn't wrap, so neither may they
        rng = random.Random(seed ^ 0xB4C4)
        player = bw.spawn(world, rng)
        return world, player, rng

    world, player, rng = new_world(args.seed)
    seed = world.seed
    if args.spawn_zone:
        bw.move_to_zone(world, player, args.spawn_zone)

    textures = build_textures(ctx, pygame)
    mesh = WorldMesh(ctx, scene_prog, world)
    lg_tex = ctx.texture((world.cols, world.rows), 1, dtype="f4")
    lg_tex.filter = (0x2601, 0x2601)
    sprite = load_sprite_layers(ctx, pygame)
    sprite_vbo = ctx.buffer(reserve=6 * 5 * 4)
    sprite_vao = ctx.vertex_array(
        sprite_prog, [(sprite_vbo, "3f 2f", "in_pos", "in_uv")])
    doors_vbo = ctx.buffer(reserve=65536)
    doors_vao = ctx.vertex_array(
        scene_prog, [(doors_vbo, "3f 2f 3f 1f 1f",
                      "in_pos", "in_uv", "in_norm", "in_mat", "in_shade")])

    def build_door_verts():
        """Door panels as dynamic geometry: hinged at one jamb, swinging
        by their anim state. Same vertex layout as the world mesh."""
        verts = []
        for (dx_, dy_), door in world.doors.items():
            if math.hypot(*world.wrap_delta(player.x, player.y,
                                            dx_ + 0.5, dy_ + 0.5)) > 26:
                continue
            a = door["anim"] * 1.85 if door["open"] else 0.0
            if door["axis"] == "x":
                hx, hz = dx_ + 0.05, dy_ + 0.5
                ux, uz = math.cos(a), math.sin(a)
            else:
                hx, hz = dx_ + 0.5, dy_ + 0.05
                ux, uz = math.sin(a), math.cos(a)
            ex, ez = hx + ux * 0.9, hz + uz * 0.9
            nx_, nz_ = -uz, ux
            for (x0, z0, u0), (x1, z1, u1) in (((hx, hz, 0.0), (ex, ez, 1.0)),):
                a_ = (x0, 0.0, z0, u0, 1.0, nx_, 0.0, nz_, 6.0, 1.0)
                b_ = (x1, 0.0, z1, u1, 1.0, nx_, 0.0, nz_, 6.0, 1.0)
                c_ = (x1, 0.8, z1, u1, 0.0, nx_, 0.0, nz_, 6.0, 1.0)
                d_ = (x0, 0.8, z0, u0, 0.0, nx_, 0.0, nz_, 6.0, 1.0)
                verts += [*a_, *b_, *c_, *a_, *c_, *d_]
        return np.array(verts, dtype=np.float32) if verts else None

    audio = bw.Audio(pygame, rng, enabled=not (args.mute or headless))
    walker = bw.AutoWalker(rng)
    lights_out = bw.LightsOut(rng, record=bool(args.record))
    presence = None if args.no_entity else bw.Presence(
        world, player, rng, ahead=bool(args.record))
    cam = Camcorder(rng)
    exposure = Exposure()
    clock = pygame.time.Clock()

    auto = not args.manual or headless
    fade = 0.0
    death_t = None          # time into the death sequence
    blood_resid = 0.0       # blood stays on the lens after a respawn
    death_pitch = 0.0
    death_roll = 0.0
    death_roll_dir = 1.0
    prev_yaw = player.angle
    yaw_rate_s = 0.0
    roll = 0.0
    shift_timer = bw.SHIFT_PERIOD
    brightness = 1.0
    flicker_left = 0.0
    flicker_next = rng.uniform(4.0, 10.0)
    hum_scan = 0.0
    tear_timer = 0.0
    show_map = bool(args.debug_overlay)
    hud_timer = 0.0 if headless and not args.debug_overlay else 4.0
    hud_font = pygame.font.SysFont("menlo,consolas,monospace", 15)
    mm_timer = 0.0
    mm_surf = None
    t_now = 0.0
    recorded = []
    record_frames = int(args.seconds * 15) if args.record else 0

    light_np = np.zeros((world.rows, world.cols), dtype=np.float32)

    running = True
    while running:
        dt = 1 / 30.0 if headless else min(clock.tick(60) / 1000.0, 0.05)
        t_now += dt

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                hud_timer = 3.0
                if event.key == pygame.K_ESCAPE:
                    running = False
                elif event.key == pygame.K_TAB:
                    auto = not auto
                elif event.key == pygame.K_m:
                    show_map = not show_map
                elif event.key == pygame.K_F12:
                    data = (window_fbo or out_fbo).read(components=3)
                    from PIL import Image
                    Image.frombytes("RGB", (W, H), data).transpose(
                        Image.FLIP_TOP_BOTTOM).save(f"backrooms_gl_{seed}.png")
                    print(f"saved backrooms_gl_{seed}.png")
                elif event.key == pygame.K_r:
                    world, player, rng = new_world(None)
                    seed = world.seed
                    mesh = WorldMesh(ctx, scene_prog, world)
                    lg_tex.release()
                    lg_tex = ctx.texture((world.cols, world.rows), 1, dtype="f4")
                    lg_tex.filter = (0x2601, 0x2601)
                    light_np = np.zeros((world.rows, world.cols), dtype=np.float32)
                    walker = bw.AutoWalker(rng)
                    lights_out = bw.LightsOut(rng)
                    if presence is not None:
                        presence = bw.Presence(world, player, rng)

        if args.test_death and death_t is None and t_now > 1.5:
            death_t = 0.0
            args.test_death = False

        # ------- death sequence: it caught him, and the camera saw -------
        if death_t is not None:
            death_t += dt
            player.want_vx = player.want_vy = player.vx = player.vy = 0.0
            player.fear = 1.0
            # it is ON him, filling the frame, thrashing
            d = max(0.55, 1.5 - death_t * 1.6)
            presence.x = player.x + math.cos(player.angle) * d
            presence.y = player.y + math.sin(player.angle) * d
            presence.heading = player.angle + math.pi
            presence.anim_phase += dt * 6.0
            if death_t > 0.85:      # the camera goes down with him, sideways
                k = min(1.0, (death_t - 0.85) / 0.5)
                player.eye = bw.EYE_STAND * (1 - k) + 0.10 * k
                death_pitch = -0.85 * k
                death_roll = 0.62 * k * death_roll_dir
            if death_t > 2.55:      # tape cuts; somewhere else, later
                np_ = bw.spawn(world, rng)
                player.x, player.y, player.angle = np_.x, np_.y, np_.angle
                player.z = player.vz = 0.0
                player.eye = bw.EYE_STAND
                player.fear = 0.5
                presence.relocate(world, player)
                presence.tension = 0.25     # it has had its fun. for now.
                walker = bw.AutoWalker(rng)
                blood_resid = 0.55
                death_pitch = 0.0
                death_roll = 0.0
                death_t = None
        # ------- simulation (same beats as backrooms_walk.main) -------
        elif fade <= 0.0:
            if auto:
                walker.update(world, player, dt, presence)
            else:
                keys = pygame.key.get_pressed()
                turn = ((keys[pygame.K_RIGHT] or keys[pygame.K_e])
                        - (keys[pygame.K_LEFT] or keys[pygame.K_q]))
                player.angle += turn * bw.TURN_SPEED * dt
                fwd = keys[pygame.K_w] - keys[pygame.K_s]
                strafe = keys[pygame.K_d] - keys[pygame.K_a]
                run = keys[pygame.K_LSHIFT] or keys[pygame.K_RSHIFT]
                player.running = bool(run and (fwd or strafe))
                if fwd or strafe:
                    dx = math.cos(player.angle) * fwd - math.sin(player.angle) * strafe
                    dy = math.sin(player.angle) * fwd + math.cos(player.angle) * strafe
                    mag = math.hypot(dx, dy) or 1.0
                    speed = bw.MOVE_SPEED * (1.45 if player.running else 1.0)
                    if player.crouched():
                        speed = min(speed, bw.MOVE_SPEED * 0.55)
                    player.want_vx = dx / mag * speed
                    player.want_vy = dy / mag * speed
                else:
                    player.want_vx = player.want_vy = 0.0
            player.apply(world, dt, bw.WALKER_MAX_DROP if auto else None)
            if presence is not None and presence.update(world, player, dt, audio):
                death_t = 0.0
                death_roll_dir = rng.choice((-1.0, 1.0))
                if audio.ok:
                    audio.play_scream(player.presence_bearing, 1.5, player)
                    audio.play_death()
            if player.fell:
                fade = 1.2
        else:
            fade -= dt
            if player.fell and fade < 0.6:
                np_ = bw.spawn(world, rng)
                player.x, player.y, player.angle = np_.x, np_.y, np_.angle
                player.z = player.vz = 0.0
                player.vx = player.vy = player.want_vx = player.want_vy = 0.0
                player.fell = False
                walker = bw.AutoWalker(rng)

        if not args.no_shift:
            shift_timer -= dt
            if shift_timer <= 0:
                shift_timer = bw.SHIFT_PERIOD
                changed = world.peripheral_shift(player.x, player.y, rng)
                if changed:
                    mesh.rebuild_cells(changed)

        # doors: open for whoever walks at them
        agents = [(player.x, player.y, "player")]
        if presence is not None:
            agents.append((presence.x, presence.y, "presence"))
        for kind, dx_, dy_ in world.update_doors(dt, agents):
            audio.play_door(kind, dx_, dy_, player)

        # hum follows the nearest live panel; echoes follow the room size
        hum_scan -= dt
        if audio.ok and hum_scan <= 0:
            hum_scan = 0.25
            audio.set_hum_proximity(
                bw.nearest_panel_dist(world, player), brightness)
            audio.set_space(bw.estimate_space(world, player))

        audio.update(dt, player)
        lights_out.update(world, player, dt, audio)
        if presence is not None and audio.ok and player.presence_dist is not None:
            audio.set_presence(player.presence_dist, player.presence_bearing)
            if player.presence_dist < 7.0:
                flicker_next = min(flicker_next, rng.uniform(0.3, 1.5))

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

        # ------- camera + exposure -------
        fov, sway_yaw, pitch, roll_noise = cam.update(dt, player, walker, world)
        ev = exposure.update(dt, world, player, brightness)
        eye = (player.x, player.eye_z(), player.y)
        yaw = player.angle + getattr(player, "sway", 0.0) + sway_yaw

        # Roll: the camera banks into fast pans (a frantic look-back tilts
        # hard), sways with the stride, and never sits perfectly level.
        d_yaw = (yaw - prev_yaw + math.pi) % math.tau - math.pi
        prev_yaw = yaw
        yaw_rate = d_yaw / max(dt, 1e-3)
        yaw_rate_s += (yaw_rate - yaw_rate_s) * min(1.0, 8.0 * dt)
        speed = math.hypot(player.vx, player.vy)
        stride_roll = math.sin(player.bob_phase * 0.5) * 0.014 * min(
            1.0, speed / bw.MOVE_SPEED)
        roll_target = max(-0.38, min(0.38, -yaw_rate_s * 0.055)) \
            + stride_roll + roll_noise
        roll += (roll_target - roll) * min(1.0, 9.0 * dt)

        pitch += death_pitch
        total_roll = roll + death_roll
        if death_t is not None:     # violent struggle shake
            yaw += rng.uniform(-0.05, 0.05)
            pitch += rng.uniform(-0.04, 0.04)
            total_roll += rng.uniform(-0.05, 0.05)
        blood_resid = max(0.0, blood_resid - dt * 0.02)

        # ------- render scene -------
        for row in range(world.rows):
            light_np[row, :] = world.light[row]
        lg_tex.write(light_np.tobytes())

        proj = perspective(fov, W / H, NEAR, FAR)
        viewm = view_matrix(np.array(eye, dtype=np.float32), yaw, pitch,
                            total_roll)
        mvp = (viewm @ proj).astype(np.float32)

        scene_fbo.use()
        ctx.viewport = (0, 0, W, H)
        ctx.clear(*[c / 255 for c in bw.FOG], 1.0)
        ctx.enable(moderngl.DEPTH_TEST)
        ctx.disable(moderngl.BLEND)
        n, lpos, lcol = gather_lights(world, player, brightness)
        scene_prog["mvp"].write(mvp.tobytes())
        scene_prog["cam"].value = eye
        scene_prog["nlights"].value = n
        scene_prog["lpos"].write(lpos.tobytes())
        scene_prog["lcol"].write(lcol.tobytes())
        scene_prog["fogcol"].value = tuple(c / 255 for c in bw.FOG)
        scene_prog["gridsize"].value = (float(world.cols), float(world.rows))
        scene_prog["flick"].value = brightness
        textures.use(0)
        lg_tex.use(1)
        scene_prog["tex"].value = 0
        scene_prog["lightgrid"].value = 1
        mesh.render()

        # door panels: hinged, swinging, drawn with the world's shader
        dverts = build_door_verts()
        if dverts is not None:
            doors_vbo.write(dverts.tobytes())
            doors_vao.render(vertices=len(dverts) // 10)

        # the Howler billboard + blob shadow
        if presence is not None and sprite is not None:
            stex, aspect = sprite
            rx, ry = world.wrap_delta(player.x, player.y, presence.x, presence.y)
            pz = world.floor_at(presence.x, presence.y)
            hgt = 1.15
            wdt = hgt * aspect
            fwdx, fwdz = math.cos(yaw), math.sin(yaw)
            rightx, rightz = -fwdz, fwdx
            cxp, czp = player.x + rx, player.y + ry
            to_player = math.atan2(-ry, -rx)
            viewa = (to_player - getattr(presence, "heading", 0.0)) % math.tau
            row_ = int((viewa + math.pi / 8) / (math.pi / 4)) % 8
            col_ = int(getattr(presence, "anim_phase", 0.0) * 8) % 8
            layer = row_ * 8 + col_
            xi, yi = int(presence.x) % world.cols, int(presence.y) % world.rows
            cell_l = world.light[yi][xi]
            ctx.enable(moderngl.BLEND)      # BLEND
            ctx.blend_func = (0x0302, 0x0303)
            sprite_prog["mvp"].write(mvp.tobytes())
            sprite_prog["cam"].value = eye
            sprite_prog["fogcol"].value = tuple(c / 255 for c in bw.FOG)
            sprite_prog["cell_l"].value = cell_l
            stex.use(2)
            sprite_prog["tex"].value = 2
            # soft contact blob under the feet (skip on uneven ground —
            # a blob hovering over a ledge reads as a floating slab)
            blob_ok = all(
                abs(world.floor_at(presence.x + ox, presence.y + oy) - pz) < 0.08
                for ox, oy in ((-0.45, -0.45), (0.45, -0.45),
                               (-0.45, 0.45), (0.45, 0.45)))
            s = 0.45
            sh = np.array([
                cxp - s, pz + 0.012, czp - s, 0, 0,
                cxp + s, pz + 0.012, czp - s, 1, 0,
                cxp - s, pz + 0.012, czp + s, 0, 1,
                cxp - s, pz + 0.012, czp + s, 0, 1,
                cxp + s, pz + 0.012, czp - s, 1, 0,
                cxp + s, pz + 0.012, czp + s, 1, 1], dtype=np.float32)
            if blob_ok:
                sprite_prog["mode"].value = 1
                sprite_prog["shadow_k"].value = 0.0
                sprite_prog["layer"].value = 0.0
                sprite_vbo.write(sh.tobytes())
                sprite_vao.render()

            hw = wdt / 2
            bl = (cxp - rightx * hw, pz, czp - rightz * hw)
            br = (cxp + rightx * hw, pz, czp + rightz * hw)
            tl = (bl[0], pz + hgt, bl[2])
            tr = (br[0], pz + hgt, br[2])

            # Its shadow, cast across the floor by the nearest live panel —
            # project the billboard corners from the light onto the floor
            # plane. Through a doorway, the shadow arrives first.
            best = None
            sxi, syi = int(presence.x), int(presence.y)
            for yy in range(syi - 9, syi + 10):
                for xx in range(sxi - 9, sxi + 10):
                    x_, y_ = xx % world.cols, yy % world.rows
                    if world.panel[y_][x_] and world.light[y_][x_] > 0.35:
                        d2 = (xx + 0.5 - presence.x) ** 2 + (yy + 0.5 - presence.y) ** 2
                        if best is None or d2 < best[0]:
                            best = (d2, xx + 0.5, world.ceil[y_][x_] - 0.06, yy + 0.5)
            if best is not None and best[2] > pz + hgt + 0.05:
                _, lx, ly, lz = best
                L = np.array([lx, ly, lz])
                projected = []
                for P in (bl, br, tl, tr):
                    Pv = np.array(P)
                    denom = ly - Pv[1]
                    if denom < 0.05:
                        projected = None
                        break
                    t_ = (ly - (pz + 0.008)) / denom
                    projected.append(L + (Pv - L) * t_)
                if projected is not None:
                    # The shadow lives on ITS floor plane. If any corner
                    # hangs over ground at a different height (plateau
                    # rims, terraces), the quad floats in space and reads
                    # as dark spikes/slabs across the room. Clip it.
                    for S in projected:
                        if (math.hypot(S[0] - presence.x, S[2] - presence.y) > 4.0
                                or abs(world.floor_at(float(S[0]), float(S[2]))
                                       - pz) > 0.08):
                            projected = None
                            break
                if projected is not None:
                    ldist = math.sqrt(best[0])
                    k = max(0.15, min(0.6, 1.5 / (1.0 + ldist)))
                    pbl, pbr, ptl, ptr = projected
                    shq = np.array([
                        *pbl, 0, 0, *pbr, 1, 0, *ptl, 0, 1,
                        *ptl, 0, 1, *pbr, 1, 0, *ptr, 1, 1], dtype=np.float32)
                    sprite_prog["mode"].value = 2
                    sprite_prog["shadow_k"].value = k
                    sprite_prog["layer"].value = float(layer)
                    sprite_vbo.write(shq.tobytes())
                    sprite_vao.render()

            # the billboard itself
            bb = np.array([
                *bl, 0, 0, *br, 1, 0, *tl, 0, 1,
                *tl, 0, 1, *br, 1, 0, *tr, 1, 1], dtype=np.float32)
            sprite_prog["mode"].value = 0
            sprite_prog["shadow_k"].value = 0.0
            sprite_prog["layer"].value = float(layer)
            sprite_vbo.write(bb.tobytes())
            sprite_vao.render()
            ctx.disable(moderngl.BLEND)

        # ------- post: bloom -------
        ctx.disable(moderngl.DEPTH_TEST)
        ping.use()
        ctx.viewport = (0, 0, bw_, bh_)
        scene_tex.use(0)
        bright_prog["scene"].value = 0
        q_bright.render()
        for _ in range(2):
            pong.use()
            ping_t.use(0)
            blur_prog["src"].value = 0
            blur_prog["dir"].value = (1.5 / bw_, 0.0)
            q_blur.render()
            ping.use()
            pong_t.use(0)
            blur_prog["dir"].value = (0.0, 1.5 / bh_)
            q_blur.render()

        # ------- post: composite -------
        tear_timer -= dt
        tear = -1.0
        if tear_timer <= 0:
            if rng.random() < 0.02 + 0.06 * player.fear:
                tear = rng.uniform(0.1, 0.9)
            tear_timer = 0.12
        target = window_fbo if window_fbo is not None else out_fbo
        target.use()
        ctx.viewport = (0, 0, W, H)
        scene_tex.use(0)
        ping_t.use(1)
        comp_prog["scene"].value = 0
        comp_prog["bloom"].value = 1
        comp_prog["exposure"].value = ev * (1.0 if fade <= 0 else
                                            max(0.05, 1.0 - (1.2 - abs(fade - 0.6) * 2)))
        comp_prog["time"].value = t_now % 97.0
        comp_prog["fear"].value = player.fear
        comp_prog["grain_amt"].value = 0.035 + 0.05 * player.fear
        comp_prog["tear"].value = tear
        blood_now = blood_resid
        static_now = 0.0
        if death_t is not None:
            blood_now = max(blood_now, min(1.0, max(0.0, (death_t - 0.2) / 0.7)))
            if death_t > 2.0:
                static_now = 1.0
        comp_prog["blood"].value = blood_now
        comp_prog["static_amt"].value = static_now
        comp_prog["res"].value = (float(W), float(H))
        q_comp.render()

        # ------- overlays: minimap + auto-hiding guide -------
        if show_map or hud_timer > 0:
            ctx.enable(moderngl.BLEND)
            ctx.blend_func = (0x0302, 0x0303)
            if show_map:
                mm_timer -= dt
                if mm_timer <= 0 or mm_surf is None:
                    mm_timer = 0.5
                    mm_surf = bw.render_minimap(world, player, pygame)
                    if presence is not None:
                        pygame.draw.circle(
                            mm_surf, (140, 25, 25),
                            (int(presence.x % world.cols * 2),
                             int(presence.y % world.rows * 2)), 3)
                if mm_surf is not None:
                    draw_overlay("map", mm_surf, 16, 16, 0.85)
            if hud_timer > 0:
                hud_timer -= dt
                hud = (f"{bw.STYLE['name']}  seed {seed}  "
                       f"{'AUTO' if auto else 'MANUAL'}  "
                       "TAB=drive M=map R=new F12=shot ESC=quit")
                text = hud_font.render(hud, True, (235, 225, 170))
                draw_overlay("hud", text, 16, H - 34,
                             min(1.0, hud_timer / 0.5))
            ctx.disable(moderngl.BLEND)

        if args.autoshot and not headless:
            if int(t_now / 2.0) != int((t_now - dt) / 2.0):
                os.makedirs(args.autoshot, exist_ok=True)
                data = window_fbo.read(components=3)
                from PIL import Image
                Image.frombytes("RGB", (W, H), data).transpose(
                    Image.FLIP_TOP_BOTTOM).save(
                    f"{args.autoshot}/shot_{int(t_now):03d}.png")
                with open(f"{args.autoshot}/poses.txt", "a") as fh:
                    fh.write(f"{int(t_now):03d} {player.x:.3f} {player.y:.3f} "
                             f"{yaw:.4f} {pitch:.4f} {player.eye_z():.3f}\n")
        if args.auto_quit and t_now >= args.auto_quit:
            running = False

        if not headless:
            pygame.display.flip()
        else:
            if args.frame:
                data = out_fbo.read(components=3)
                from PIL import Image
                img = Image.frombytes("RGB", (W, H), data).transpose(
                    Image.FLIP_TOP_BOTTOM)
                img.save(args.frame)
                print(f"seed {seed} -> {args.frame}")
                return
            recorded.append(out_fbo.read(components=3))
            if len(recorded) >= record_frames:
                running = False

    pygame.quit()
    if args.record and recorded:
        from PIL import Image
        imgs = [Image.frombytes("RGB", (W, H), d).transpose(
            Image.FLIP_TOP_BOTTOM).resize((W // 2, H // 2), Image.LANCZOS)
            for d in recorded]
        imgs[0].save(args.record, save_all=True, append_images=imgs[1:],
                     duration=66, loop=0, optimize=True)
        print(f"seed {seed} -> {args.record} ({len(imgs)} frames)")


if __name__ == "__main__":
    main()
