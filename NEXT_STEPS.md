# ReSprytile: Blender 5.2 follow-ups

Found while pressure testing against Blender 5.2.2 LTS (Windows, Vulkan backend).
These were left out of the compatibility pass because they are larger refactors or need hands-on testing.

## Needs a human at the mouse
The tool logic (build, paint, fill, decal, tile pick, snap cursor, set normal, double sided) was tested
by calling the real code with synthetic rays. The event plumbing around it was not. Check by hand:
- Build / Paint / Fill strokes with LMB drag, including undo after a stroke.
- Alt + LMB tile picker, `S` cursor snap (Ctrl toggles vertex/grid, wheel moves the layer), `N` set normal.
- `G` pixel translate (Sprytile intercepts the translate key while a tool is active).
- Palette: click/drag tile selection, Ctrl + drag to move it, Shift + MMB to move the palette,
  wheel to zoom, Ctrl + wheel / RMB to cycle grids.
- macOS (Metal) and Linux: only Windows + Vulkan was tested.

## Packaging
- Add a `blender_manifest.toml` so ReSprytile installs as a Blender Extension. Installing the release zip
  through Preferences > Get Extensions failed during testing; the legacy add-on path still works.
- Bump `bl_info["blender"]` from `(4, 1, 0)` once 5.x support is confirmed, and bump the version.

## Architecture
- Every module is imported twice: once as `ReSprytile.<module>` and once as a top level module through the
  `sys.path` insert in `__init__.py`. Registered classes come from one copy and module state
  (e.g. `sprytile_preview`, `VIEW3D_OP_SprytileGui` class attributes) from the other. It works today but
  is fragile, and `__package__` lookups only work in one copy. Move everything to package-relative imports.
- The vendored RxPY 1.x (`rx/`) is only used to fan modal events out to three tools. Replacing it with
  direct method calls would drop ~15k lines of Python 2-era code.
- The palette keeps a lot of state on the operator class (`VIEW3D_OP_SprytileGui.*`) and passes the invoke
  `context` into the draw handler. Grid references are now re-resolved per draw (that was a crash), but the
  rest should move to plain module state keyed by area.

## Drawing
- Work plane lines use `gpu.state.line_width_set(2)` with a custom shader. Wide lines are not supported on
  Vulkan/Metal this way; use the `POLYLINE_UNIFORM_COLOR` builtin shader if they render 1px.
- The palette's left padding ignores the toolbar when region overlap is on, so it slightly covers the toolbar.
- `draw_offscreen` re-renders the palette into the offscreen buffer on every redraw; it only needs to when the
  grid, selection or hover cell changes.

## Deprecations to clean up before Blender 6.0
- `Material.use_nodes` (set in `sprytile.material_setup`) is deprecated in 5.0.
- `Material.blend_method` fallback is only kept for pre-4.2; drop it when 4.1 support ends.

## Dead code
- `sprytile.update_check` queries the old itch.io page and reads `sys.modules['sprytile']`, which no longer
  exists. It is not in any menu; remove it.
- `sprytile.open_window_sprytile` toggles `sprytile_data.open_new_window`, a property that doesn't exist.
- `sprytile.start_tool` and the `EVT_TWEAK_*` event checks in the modal are leftovers from Blender 2.7x/2.8x.
