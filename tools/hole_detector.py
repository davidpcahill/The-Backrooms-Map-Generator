"""Hole detector: renders the GL mesh depth from random poses and compares
against analytic ray-marching of the World data. Any pixel where the mesh
is significantly FARTHER than the analytic first-hit is a missing face —
localized to the exact cell. Zero tolerance.

Usage: python tools/hole_detector.py [SEED] [POSES]
"""
import sys, os, math, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, moderngl, pygame
os.environ['SDL_VIDEODRIVER'] = 'dummy'
import backrooms_walk as bw, backrooms_gl as gl

pygame.init(); pygame.display.set_mode((64, 64))

SEED = int(sys.argv[1]) if len(sys.argv) > 1 else 1234
POSES = int(sys.argv[2]) if len(sys.argv) > 2 else 40
W, H = 200, 125
FAR = 24.0

world = bw.World(SEED, 120, 80); world.bounded = True
ctx = moderngl.create_standalone_context()
prog = ctx.program(vertex_shader=gl.SCENE_VS, fragment_shader=gl.SCENE_FS)
tex = gl.build_textures(ctx, pygame)
mesh = gl.WorldMesh(ctx, prog, world)
# glReadPixels(DEPTH) is broken on this driver (returns 1.0s or junk).
# Instead: render EYE DISTANCE into a float color target and read that.
DIST_VS = '''
#version 330
uniform mat4 mvp;
in vec3 in_pos; in vec2 in_uv; in vec3 in_norm; in float in_mat; in float in_shade;
out vec3 wpos; out vec2 _uv; out vec3 _n; out float _m; out float _s;
void main() { gl_Position = mvp * vec4(in_pos, 1.0); wpos = in_pos;
              _uv = in_uv; _n = in_norm; _m = in_mat; _s = in_shade; }
'''
DIST_FS = '''
#version 330
uniform vec3 cam;
in vec3 wpos; in vec2 _uv; in vec3 _n; in float _m; in float _s;
out vec4 fragment;
void main() { fragment = vec4(length(wpos - cam), 0.0, 0.0, 1.0); }
'''
prog_d = ctx.program(vertex_shader=DIST_VS, fragment_shader=DIST_FS)
mesh_d = gl.WorldMesh(ctx, prog_d, world)
dist_tex = ctx.texture((W, H), 4, dtype='f4')
depth_rb = ctx.depth_renderbuffer((W, H))
fbo = ctx.framebuffer([dist_tex], depth_rb)
lg = ctx.texture((world.cols, world.rows), 1, dtype='f4')
lg.write(np.array(world.light, dtype=np.float32).tobytes())

def analytic_ray(px, py, pz, dx, dy, dz):
    """First hit distance of ray vs world (y = height axis; world grid in
    x,z). Returns t or FAR."""
    x, z = px, pz
    ix, iz = int(x), int(z)
    t = 0.0
    for _ in range(64):
        if not (0 <= ix < world.cols and 0 <= iz < world.rows):
            return t
        f = world.floor[iz][ix]; c = world.ceil[iz][ix]
        if f >= c:
            return t                     # inside solid: wall hit at entry
        # exit t of this cell in x/z
        tx = ((ix + 1 - x) / dx) if dx > 0 else (((ix - x) / dx) if dx < 0 else 1e9)
        tz = ((iz + 1 - z) / dz) if dz > 0 else (((iz - z) / dz) if dz < 0 else 1e9)
        te = min(tx, tz)
        # floor/ceiling crossing within [0, te]?
        y0 = py + dy * t
        if dy < -1e-9:
            tf = (f - py) / dy
            if t - 1e-6 <= tf <= t + te + 1e-6:
                return tf
        elif dy > 1e-9:
            tc = (c - py) / dy
            if t - 1e-6 <= tc <= t + te + 1e-6:
                return tc
        t += te + 1e-5
        if t > FAR:
            return FAR
        x = px + dx * t; z = pz + dz * t
        ix, iz = int(x), int(z)
    return FAR

rng = random.Random(99)
open_cells = [(x, y) for (x, y) in sorted(world.open_set)
              if world.floor[y][x] == 0.0 and world.ceil[y][x] >= 1.0]
holes = {}
proj = gl.perspective(62, W / H, gl.NEAR, FAR + 10)
fovt = math.tan(math.radians(62) / 2)
aspect = W / H

checked = 0
for p_i in range(POSES):
    cx, cy = rng.choice(open_cells)
    px, pz, py = cx + 0.5, cy + 0.5, 0.55
    yaw = rng.uniform(0, math.tau)
    view = gl.view_matrix(np.array([px, py, pz], dtype=np.float32), yaw, 0.0)
    prog_d['mvp'].write((view @ proj).astype(np.float32).tobytes())
    prog_d['cam'].value = (px, py, pz)
    fbo.use(); ctx.viewport = (0, 0, W, H)
    ctx.clear(999.0, 0, 0, 1.0); ctx.enable(moderngl.DEPTH_TEST)
    mesh_d.render()
    raw = np.frombuffer(fbo.read(components=4, dtype='f4'), dtype=np.float32)
    draw = np.flipud(raw.reshape(H, W, 4)[:, :, 0])
    if p_i == 0:
        print('pose0 dist min/max:', float(draw.min()), float(draw.max()))

    fwd = np.array([math.cos(yaw), 0, math.sin(yaw)])
    right = np.array([-fwd[2], 0, fwd[0]])
    up = np.array([0, 1, 0])
    for sy in range(2, H - 2, 3):
        for sx in range(2, W - 2, 3):
            t_mesh = draw[sy, sx]
            u = (sx + 0.5) / W * 2 - 1
            v = ((H - 1 - sy) + 0.5) / H * 2 - 1
            d = fwd + right * (u * fovt * aspect) + up * (v * fovt)
            dn = d / np.linalg.norm(d)
            t_true = analytic_ray(px, py, pz, dn[0], dn[1], dn[2])
            checked += 1
            if t_true < FAR - 1 and t_mesh > t_true + 0.6:
                hx = px + dn[0] * t_true; hz = pz + dn[2] * t_true
                key = (int(hx), int(hz))
                holes.setdefault(key, []).append(
                    (p_i, round(t_true, 1), round(t_mesh, 1)))

per_pose = {}
for k, v in holes.items():
    for (pi, tt, tm) in v:
        per_pose.setdefault(pi, 0)
        per_pose[pi] += 1
print(f"seed {SEED}: {checked} rays checked, hole cells: {len(holes)}")
print("per-pose mismatches:", sorted(per_pose.items(), key=lambda kv: -kv[1])[:8])
for k, v in sorted(holes.items())[:10]:
    x, y = k
    f = world.floor[y % world.rows][x % world.cols]
    c = world.ceil[y % world.rows][x % world.cols]
    print(f"  cell {k} floor={f:.2f} ceil={c:.2f} hits={len(v)} e.g. {v[0]}")

# re-render the worst pose as an image for inspection
if per_pose:
    worst = max(per_pose, key=per_pose.get)
    rng2 = random.Random(99)
    for p_i in range(POSES):
        cx, cy = rng2.choice(open_cells)
        yaw = rng2.uniform(0, math.tau)
        if p_i == worst:
            px, pz, py = cx + 0.5, cy + 0.5, 0.55
            print(f"worst pose {worst}: cell ({cx},{cy}) yaw {yaw:.3f}")
            view = gl.view_matrix(np.array([px, py, pz], dtype=np.float32), yaw, 0.0)
            prog['mvp'].write((view @ proj).astype(np.float32).tobytes())
            prog['cam'].value = (px, py, pz)
            fbo.use(); ctx.viewport = (0, 0, W, H)
            ctx.clear(0.5, 0, 0.5, 1.0); ctx.enable(moderngl.DEPTH_TEST)
            mesh.render()
            from PIL import Image
            Image.frombytes('RGBA', (W, H), fbo.read(components=4)).transpose(
                Image.FLIP_TOP_BOTTOM).resize((800, 500), Image.NEAREST).save('worst_pose.png')
            break
