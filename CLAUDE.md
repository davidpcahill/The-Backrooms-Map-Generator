# backrooms — project notes for Claude

Found-footage Backrooms sim: `backrooms_generator.py` (2D maps),
`backrooms_walk.py` (sim core + CPU renderer), `backrooms_gl.py`
(ModernGL showcase renderer; imports walk as `bw`).

## Hard-won rules — do not relearn these

- **moderngl flag constants are NOT GL enums** (BLEND=1, DEPTH_TEST=2,
  CULL_FACE=4). Passing raw GL enums silently enables the wrong state;
  this caused the months-long "holes in walls" saga.
- **Prove rendering, don't eyeball it**: `python tools/hole_detector.py
  SEED POSES` compares rendered eye-distance against analytic
  ray-marching. Run it after any mesh/generation change. Zero tolerance.
- glReadPixels(DEPTH) is broken on macOS CGL — the detector renders
  distance into a float color target instead.
- pygame.init() pre-inits the mixer at 44100/stereo; the Audio class
  must mixer.quit() + re-init(22050) or synth buffers play as noise.
- The user tests every round live and reports what looks wrong — ship
  small, verifiable rounds, send an example frame with each.

## Architecture landmarks

- Tank model: `Player.body` (feet/heading) vs `Player.head_off`
  (camera); `p.angle` is always body+head. Never write p.angle directly.
- Senses are stimulus-driven: `Player.hear_sound()` (distance, wall
  muffling, self-noise masking, noisy bearing). No proximity radar.
- Presence: stalk/lurk/hunt + pacing director (`agg` formula) tuned for
  ~3 min mean time-to-death (sim-verify with 5 seeded lives when
  touching it).
- Levels via `STYLES` dict + `apply_style` ("0","1","2","37","run").
- Asset pipeline: rigged model -> Blender headless bake -> sprite sheet
  (Howler); FBX -> OBJ -> `_load_pipekit_models` instancing (pipes).
  Licenses in CREDITS.md.

## Build / release

- `./build_app.sh` -> dist/Backrooms.app (GL). Releases: `gh release
  create vX.Y.Z dist/Backrooms-macOS-arm64.zip` (ditto -c -k zip).
- CI: .github/workflows/ci.yml (generator, 5-level sim life, CPU
  renderer — all SDL-dummy).

## Open roadmap

- Per-level creatures: user has more rigged monster models; ask which
  file maps to which level, then reuse the Howler bake pipeline.
  - SMILER (Level 2, user-picked 2026-07-08; he may still research L1):
    candidates, both CC-BY 4.0, both rigged, download needs his
    Sketchfab login (drop into ~/Downloads):
    - PREFERRED: "Accurate Smiler" by Speed12, 5.9k tris —
      https://skfb.ly/oAyYE (grab original .blend if offered)
    - alt: "Backrooms Smiler Rig" by Flying_dragons800, 185.7k tris,
      rough rig — sketchfab.com/3d-models/backrooms-smiler-rig-fed28f4534f94fcebd97db8e5b6e3ed4
    Design: INVERSE of the Howler — visible only in darkness, vanishes
    when lit. IMPLEMENTED 2026-07-08 with PROCEDURAL placeholder art
    (make_smiler_layers in backrooms_gl.py; STYLE monster="smiler";
    sprite mode 3 = self-lit dark-gated; Presence: inverted vis_range,
    _slip_to_dark on lit_t>1.1, 0.4x step loudness, 1.4x director
    heat). Swap make_smiler_layers for a baked sheet when the model
    lands. Sim: L2 TTD 156-420s all caught; L0 unaffected (mean 145s;
    the blind-belief ring-tip fix in Presence keeps tails bounded).
- Machine-room prop dressing via the OBJ instancing pipeline.
- Real shadow map for the nearest light.
- Isolation multiplayer (canon: two wanderers who hear but never meet).
