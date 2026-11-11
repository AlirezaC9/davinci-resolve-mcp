# DaVinci Resolve MCP

An MCP server that lets AI assistants (Claude, Codex, Antigravity, Cursor, …) **drive DaVinci Resolve Studio**
through Resolve's scripting API: editing, color, Fusion, audio, subtitles and delivery. The assistant can also
**see** timeline frames to check its own work.

> Describe the edit ("cut the silence at 0:12, add a Persian title on V2, warm teal-orange look, export for
> Instagram") and the assistant does it in Resolve.

- 102 tools, tested live on DaVinci Resolve Studio 21.1 (macOS)
- Frame-accurate razor / trim / move / ripple, which Resolve's API does not have natively
- Generated color looks (exposure, contrast, temperature, lift/gamma/gain, split toning) as LUTs
- Fusion node building with keyframes, Text+ titles (Persian/Arabic supported)
- Transcription with word timings for text-based editing, auto subtitles, AI voice isolation
- One-call render, plus frame previews / contact sheets returned as images

## Requirements

- **DaVinci Resolve Studio** (the free edition does not allow external scripting), running
- In Resolve: **Preferences → System → General → External scripting using: Local**
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (it installs Python and dependencies automatically)
- macOS is tested. Windows and Linux use Resolve's standard install paths but are **untested**.

## Install

Every client runs the same command, which downloads and runs the server from GitHub:

```bash
uvx --from git+https://github.com/AlirezaC9/davinci-resolve-mcp davinci-resolve-mcp
```

On macOS, GUI apps may not find `uvx` on PATH; then use its full path
(`which uvx`, usually `~/.local/bin/uvx`).

### Claude Code

```bash
claude mcp add davinci-resolve --scope user -- uvx --from git+https://github.com/AlirezaC9/davinci-resolve-mcp davinci-resolve-mcp
```

### Claude Desktop

Settings → Developer → Edit Config (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "davinci-resolve": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/AlirezaC9/davinci-resolve-mcp", "davinci-resolve-mcp"]
    }
  }
}
```

### Codex (CLI and app)

`~/.codex/config.toml`:

```toml
[mcp_servers.davinci-resolve]
command = "uvx"
args = ["--from", "git+https://github.com/AlirezaC9/davinci-resolve-mcp", "davinci-resolve-mcp"]
startup_timeout_sec = 60
tool_timeout_sec = 3600
```

or `codex mcp add davinci-resolve -- uvx --from git+https://github.com/AlirezaC9/davinci-resolve-mcp davinci-resolve-mcp`

### Antigravity

Agent panel → `…` → MCP Servers → Manage → View raw config (`~/.gemini/config/mcp_config.json`):

```json
{
  "mcpServers": {
    "davinci-resolve": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/AlirezaC9/davinci-resolve-mcp", "davinci-resolve-mcp"]
    }
  }
}
```

### Cursor / VS Code / others

Any MCP client that supports stdio servers works. Use the same `command` + `args` as above
(Cursor: `~/.cursor/mcp.json`).

### From a local clone (for development)

```bash
git clone https://github.com/AlirezaC9/davinci-resolve-mcp && cd davinci-resolve-mcp
uv sync
uv run python tests/smoke_test_stdio.py        # with Resolve running
```

Then point your client at `<clone>/.venv/bin/davinci-resolve-mcp` (Windows: `.venv\Scripts\davinci-resolve-mcp.exe`).

## Configuration (optional environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `RESOLVE_MCP_TIMEOUT` | `120` | Seconds before a call that Resolve does not answer returns an error (e.g. a dialog is open) |
| `RESOLVE_MCP_HOME` | `~/Documents/ResolveMCP` | Previews, snapshots, renders |
| `RESOLVE_MCP_RTL_FONT` | `Geeza Pro` (mac) / `Tahoma` | Font used automatically for Persian/Arabic titles |
| `RESOLVE_APP_PATH`, `RESOLVE_SCRIPT_API`, `RESOLVE_SCRIPT_LIB`, `RESOLVE_LUT_DIR` | standard install paths | Non-standard Resolve installs |

## Tools

| Area | Tools |
|---|---|
| App / project | status, launch, page, save, undo, redo, projects_list, project_open, project_create, project_settings, project_snapshot, project_import, project_export, project_manage, presets, playhead |
| Media pool | media_import, media_pool, bin_manage, clip_info, clip_update, media_link, media_storage, media_select, metadata_export, transcribe, transcript, media_ai, audio_sync, multicam_create |
| Timeline | timelines, timeline_open, timeline_create, timeline_manage, timeline_settings, timeline_get, track_add, track_set, in_out, timeline_export, timeline_import, scene_cut_detect, subtitles_from_audio, clips_select_info |
| Editing | clips_place, clips_assemble, clips_delete, razor, clip_trim, clip_move, gaps_close, range_remove, clip_properties, clip_speed, clip_fade, clip_label, clips_link, transition_add, compound_create, title_add, title_update, generator_add, markers |
| Color | grade_look, grade_cdl, grade_lut, lut_list, nodes, grade_reset, grade_copy, grade_drx, color_versions, color_groups, stills, lut_export, clip_ai (magic mask, stabilize, smart reframe), dctl |
| Fusion | fusion_comps, fusion_comp_info, fusion_tool_add, fusion_set (values, keyframes, expressions), fusion_connect, fusion_tool_delete, fusion_render_settings |
| Audio | audio_normalize, voice_isolation, tts_generate, audio_insert, audio_mapping (+ volume/pan via clip_properties) |
| Deliver | render_options, render_job_add, render_start, render_status, render_jobs, render_timeline, quick_export |
| Visual check | frame_preview, frames_contact, media_frames |
| Escape hatches | run_python (full API + helpers), api_search, batch |

Conventions: times are **seconds from the timeline start** (or `"120f"`, or a timecode); tracks are `V1/A1/S1`;
clips are `"V1:0"` (transitions not counted), `{"track":"V1","time":3.2}`, an id, or `"selected"`.

## How missing API features are handled

Resolve's scripting API has **no razor, trim, move or undo**, and titles/generators can only be *inserted*
(rippling track V1):

- **razor / clip_trim / clip_move / range_remove / gaps_close** rebuild the affected clips from source media,
  frame-accurately. They keep Inspector properties, fades, grades (copied through a temporary holder clip),
  Fusion comps, clip color and enabled state, and re-link audio/video. Retimed clips must be set back to 100%
  first; clip markers and extra color versions are not carried over.
- **title_add / generator_add** insert at the end of V1 and wrap the result in a compound clip (bin
  "Titles (MCP)") placed at any track/time; `title_update` rebuilds such titles from stored parameters.
- **undo / redo** send Cmd+Z / Cmd+Shift+Z (macOS, needs Accessibility permission). The reliable safety net is
  **project_snapshot** (exports a .drp backup).

## Known Resolve quirks

- Changing an existing timeline's frame rate by script can freeze Resolve's scripting bridge (sometimes with a
  "frame rate mismatch" dialog). Set the frame rate when creating the project. Every call has a watchdog, so a
  blocked Resolve returns an error instead of hanging the assistant; Resolve may need a restart.
- `MediaPool.ImportMedia([{"FilePath": …}])` (the documented form) fails on 21.1; plain path lists work. The
  server tries both.
- Fusion comps edited by script must not be wrapped in `comp.Lock()`, or Resolve won't re-render the changes.

## License

MIT
