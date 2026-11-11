"""Timeline level tools: create/open/inspect timelines, tracks, settings, import/export."""

from __future__ import annotations

from typing import Any

from .core import (
    TL, all_tracks, describe_item, ensure_track, fail, find_folder, find_items, find_mpitem, media_pool, parse_track,
    pick_const, project, resolve, safe, timeline, tool, track_items, track_label,
)

EXPORT_TYPES = {
    "aaf": ("EXPORT_AAF", "EXPORT_AAF_NEW"), "drt": ("EXPORT_DRT", None), "edl": ("EXPORT_EDL", "EXPORT_NONE"),
    "edl_cdl": ("EXPORT_EDL", "EXPORT_CDL"), "edl_sdl": ("EXPORT_EDL", "EXPORT_SDL"),
    "edl_missing": ("EXPORT_EDL", "EXPORT_MISSING_CLIPS"), "fcp7xml": ("EXPORT_FCP_7_XML", None),
    "fcpxml": ("EXPORT_FCPXML_1_10", None), "fcpxml_1_9": ("EXPORT_FCPXML_1_9", None), "fcpxml_1_8": ("EXPORT_FCPXML_1_8", None),
    "otio": ("EXPORT_OTIO", None), "csv": ("EXPORT_TEXT_CSV", None), "tab": ("EXPORT_TEXT_TAB", None),
    "ale": ("EXPORT_ALE", None), "ale_cdl": ("EXPORT_ALE_CDL", None), "hdr10_a": ("EXPORT_HDR_10_PROFILE_A", None),
    "hdr10_b": ("EXPORT_HDR_10_PROFILE_B", None), "dolby_vision": ("EXPORT_DOLBY_VISION_VER_5_1", None),
}


@tool(read_only=True)
def timelines() -> dict:
    """List all timelines in the project (name, id, duration, fps, tracks) and which one is current."""
    p = project()
    cur = p.GetCurrentTimeline()
    out = []
    for i in range(1, p.GetTimelineCount() + 1):
        t = p.GetTimelineByIndex(i)
        T = TL(t)
        out.append({"index": i, "name": t.GetName(), "id": t.GetUniqueId(), "fps": T.fps,
                    "duration": round((t.GetEndFrame() - t.GetStartFrame()) / T.fps, 3), "start_tc": t.GetStartTimecode(),
                    "tracks": {k: t.GetTrackCount(k) for k in ("video", "audio", "subtitle")}})
    return {"current": cur.GetName() if cur else None, "timelines": out}


@tool()
def timeline_open(name: str) -> dict:
    """Make a timeline (by name or id) the current timeline."""
    t = timeline(name)
    if not project().SetCurrentTimeline(t):
        fail("SetCurrentTimeline failed")
    return {"current": t.GetName()}


@tool()
def timeline_create(name: str, clips: list[Any] | None = None, width: int | None = None, height: int | None = None,
                    fps: str | None = None, start_timecode: str | None = None, bin: str | None = None,
                    settings: dict | None = None) -> dict:
    """Create a new timeline and make it current. Optional custom resolution (e.g. 1080x1920 for vertical),
    fps ('25', '29.97' — prefer setting the PROJECT fps with project_create before any timeline exists; changing a
    timeline's fps by script is fragile in Resolve), start timecode, other timeline settings, and initial `clips`: media refs (whole clips) or
    segments {"media":..., "in":sec, "out":sec} appended in order."""
    mp = media_pool()
    if bin:
        mp.SetCurrentFolder(find_folder(bin, create=True))
    s: dict[str, str] = {}
    if width or height or fps or settings:
        s["useCustomSettings"] = "1"
    if width:
        s["timelineResolutionWidth"] = str(width)
        s["timelineOutputResolutionWidth"] = str(width)
    if height:
        s["timelineResolutionHeight"] = str(height)
        s["timelineOutputResolutionHeight"] = str(height)
    proj_fps = float((project().GetSettings() or {}).get("timelineFrameRate") or 0)
    if fps and abs(float(str(fps).split()[0]) - proj_fps) > 0.001:
        # Changing a timeline's fps by script is fragile in Resolve 21 (can freeze the scripting bridge);
        # it is applied once, right after creation, before the timeline is opened.
        s["timelineFrameRate"] = str(fps)
    for k, v in (settings or {}).items():
        s[k] = str(v)
    t = mp.CreateEmptyTimeline(name)
    if not t:
        fail(f"Could not create timeline '{name}' (name already used?)")
    errors = []
    if s:
        if not t.SetSettings(s):
            for k, v in s.items():
                try:
                    if not t.SetSetting(k, v):
                        errors.append(k)
                except Exception:  # noqa: BLE001
                    errors.append(k)
    project().SetCurrentTimeline(t)
    if start_timecode:
        t.SetStartTimecode(start_timecode)
    placed = []
    if clips:
        from .tools_edit import clips_assemble
        placed = clips_assemble([c if isinstance(c, dict) else {"media": c} for c in clips]).get("placed", [])
    T = TL(t)
    return {"created": t.GetName(), "id": t.GetUniqueId(), "fps": T.fps,
            "resolution": f"{(t.GetSettings() or {}).get('timelineResolutionWidth')}x{(t.GetSettings() or {}).get('timelineResolutionHeight')}",
            "settings_errors": errors or None, "placed": placed or None}


@tool()
def timeline_manage(action: str, name: str | None = None, new_name: str | None = None) -> dict:
    """Timeline housekeeping. action: 'rename' (timeline `name` or current → new_name), 'duplicate' (→ new_name),
    'delete' (timeline `name`; irreversible), 'compound_to_media' (get media pool item of current timeline)."""
    a = action.lower()
    t = timeline(name)
    if a == "rename":
        if not t.SetName(new_name):
            fail("Rename failed (name must be unique)")
        return {"renamed": new_name}
    if a == "duplicate":
        d = t.DuplicateTimeline(new_name) if new_name else t.DuplicateTimeline()
        if not d:
            fail("Duplicate failed")
        return {"duplicate": d.GetName(), "id": d.GetUniqueId()}
    if a == "delete":
        ok = media_pool().DeleteTimelines([t])
        return {"deleted": bool(ok)}
    if a == "compound_to_media":
        m = t.GetMediaPoolItem()
        return {"media_id": m.GetUniqueId() if m else None}
    fail("action must be rename | duplicate | delete")
    return {}


@tool()
def timeline_settings(set: dict | None = None, search: str | None = None, start_timecode: str | None = None,
                      output_blanking: dict | None = None, timeline_name: str | None = None) -> dict:
    """Read/change settings of the current timeline. `set` applies {key: value} (useCustomSettings is turned on
    automatically, e.g. {"timelineResolutionWidth":"1080","timelineResolutionHeight":"1920"}). Also sets the start
    timecode and output blanking ({"Top":px,"Bottom":px,"Left":px,"Right":px})."""
    t = timeline(timeline_name)
    out: dict[str, Any] = {}
    if set:
        s = {"useCustomSettings": "1", **{k: str(v) for k, v in set.items()}}
        if not t.SetSettings(s):
            errs = []
            for k, v in s.items():
                try:
                    if not t.SetSetting(k, v):
                        errs.append(k)
                except Exception:  # noqa: BLE001
                    errs.append(k)
            out["errors"] = errs or None
    if start_timecode:
        out["start_timecode_set"] = bool(t.SetStartTimecode(start_timecode))
    if output_blanking is not None:
        out["blanking_set"] = bool(t.SetOutputBlanking(output_blanking))
    st = t.GetSettings() or {}
    if search:
        st = {k: v for k, v in st.items() if search.lower() in k.lower()}
    elif set:
        st = {k: st.get(k) for k in set}
    out["settings"] = st
    out["start_timecode"] = t.GetStartTimecode()
    out["output_blanking"] = safe(t.GetOutputBlanking)
    return out


@tool(read_only=True)
def timeline_get(tracks: list[str] | None = None, start: Any = None, end: Any = None, detail: bool = False,
                 markers: bool = True, timeline_name: str | None = None) -> dict:
    """The edit of the current timeline: every track (name, enabled, locked) with its clips (ref, id, name, start/end
    seconds + frames, source in/out, media id). Filter by `tracks` (["V1","A1"]) and a time window. detail=true adds
    properties, speed, fades, linked clips, comps, markers per clip."""
    t = timeline(timeline_name)
    T = TL(t)
    f0 = T.frame(start) if start is not None else None
    f1 = T.frame(end) if end is not None else None
    want = {parse_track(x) for x in tracks} if tracks else None
    out_tracks = []
    for tt, ti in all_tracks(t):
        if want and (tt, ti) not in want:
            continue
        items = []
        for it in track_items(t, tt, ti):
            if f0 is not None and it.GetEnd() <= f0:
                continue
            if f1 is not None and it.GetStart() >= f1:
                continue
            items.append(describe_item(t, it, T, detail))
        tr = {"track": track_label(tt, ti), "name": safe(lambda: t.GetTrackName(tt, ti)), "clips": items}
        if not safe(lambda: t.GetIsTrackEnabled(tt, ti), True):
            tr["enabled"] = False
        if safe(lambda: t.GetIsTrackLocked(tt, ti), False):
            tr["locked"] = True
        if tt == "audio":
            tr["format"] = safe(lambda: t.GetTrackSubType(tt, ti))
        out_tracks.append(tr)
    res: dict[str, Any] = {"timeline": t.GetName(), "fps": T.fps, "start_tc": t.GetStartTimecode(),
                           "duration": round((t.GetEndFrame() - T.start) / T.fps, 3), "tracks": out_tracks}
    if markers:
        mk = safe(t.GetMarkers, {}) or {}
        res["markers"] = [{"time": round(float(k) / T.fps, 3), "frame": int(k), **v} for k, v in sorted(mk.items())]
    inout = safe(t.GetMarkInOut, {})
    if inout:
        res["in_out"] = inout
    return res


@tool()
def track_add(type: str = "video", count: int = 1, audio_format: str = "stereo", name: str | None = None) -> dict:
    """Add tracks to the current timeline. type: video | audio | subtitle. audio_format: mono | stereo | 5.1 |
    7.1 | adaptive1..36 …"""
    t = timeline()
    added = []
    for _ in range(max(1, count)):
        ok = t.AddTrack(type, audio_format) if type == "audio" else t.AddTrack(type)
        if not ok:
            fail(f"AddTrack({type}) failed")
        idx = t.GetTrackCount(type)
        if name:
            t.SetTrackName(type, idx, name if count == 1 else f"{name} {len(added) + 1}")
        added.append(track_label(type, idx))
    return {"added": added}


@tool()
def track_set(track: str, name: str | None = None, enabled: bool | None = None, locked: bool | None = None,
              delete: bool = False, voice_isolation: dict | None = None) -> dict:
    """Modify a track ('V2', 'A1', 'S1'): rename, enable/disable, lock/unlock, delete (irreversible),
    voice_isolation for audio tracks ({"isEnabled":true,"amount":70})."""
    t = timeline()
    tt, ti = parse_track(track)
    if ti > t.GetTrackCount(tt):
        fail(f"{track} does not exist")
    r: dict[str, Any] = {"track": track}
    if delete:
        r["deleted"] = bool(t.DeleteTrack(tt, ti))
        return r
    if name is not None:
        r["name"] = bool(t.SetTrackName(tt, ti, name))
    if enabled is not None:
        r["enabled"] = bool(t.SetTrackEnable(tt, ti, enabled))
    if locked is not None:
        r["locked"] = bool(t.SetTrackLock(tt, ti, locked))
    if voice_isolation is not None:
        r["voice_isolation"] = bool(t.SetVoiceIsolationState(ti, voice_isolation))
    return r


@tool()
def in_out(start: Any = None, end: Any = None, clear: bool = False, type: str = "all") -> dict:
    """Get/set/clear the timeline In/Out marks (used e.g. for rendering a range). type: all | video | audio."""
    t = timeline()
    T = TL(t)
    if clear:
        t.ClearMarkInOut(type)
    elif start is not None or end is not None:
        fi = T.rel(T.frame(start)) if start is not None else 0
        fo = T.rel(T.frame(end)) if end is not None else T.rel(t.GetEndFrame()) - 1
        if not t.SetMarkInOut(fi, fo, type):
            fail("SetMarkInOut failed")
    io = safe(t.GetMarkInOut, {}) or {}
    pretty = {k: {kk: round(vv / T.fps, 3) for kk, vv in v.items()} for k, v in io.items() if isinstance(v, dict)}
    return {"frames": io, "seconds": pretty}


@tool()
def timeline_export(path: str, format: str = "fcpxml", timeline_name: str | None = None) -> dict:
    """Export the timeline to a file. format: aaf | drt | edl | edl_cdl | edl_sdl | edl_missing | fcp7xml | fcpxml
    (1.10) | fcpxml_1_9 | fcpxml_1_8 | otio | csv | tab | ale | ale_cdl | hdr10_a | hdr10_b | dolby_vision."""
    t = timeline(timeline_name)
    f = format.lower()
    if f not in EXPORT_TYPES:
        fail(f"format must be one of {list(EXPORT_TYPES)}")
    et, sub = EXPORT_TYPES[f]
    r = resolve()
    ok = t.Export(path, getattr(r, et), getattr(r, sub) if sub else r.EXPORT_NONE)
    if not ok:
        fail("Export failed")
    return {"exported": path}


@tool()
def timeline_import(path: str, name: str | None = None, import_source_clips: bool = True,
                    source_clips_path: str | None = None, into_current: bool = False) -> dict:
    """Import a timeline from AAF/EDL/XML/FCPXML/DRT/ADL/OTIO as a new timeline (or, with into_current=true and an
    AAF, into the current timeline)."""
    if into_current:
        opts = {"sourceClipsPath": source_clips_path} if source_clips_path else {}
        return {"ok": bool(timeline().ImportIntoTimeline(path, opts))}
    opts: dict[str, Any] = {"importSourceClips": import_source_clips}
    if name:
        opts["timelineName"] = name
    if source_clips_path:
        opts["sourceClipsPath"] = source_clips_path
    t = media_pool().ImportTimelineFromFile(path, opts)
    if not t:
        fail("Import failed")
    project().SetCurrentTimeline(t)
    return {"imported": t.GetName(), "id": t.GetUniqueId()}


@tool(timeout=1800)
def scene_cut_detect() -> dict:
    """Detect scene cuts in the current timeline and razor the clips at each cut (useful for flattened masters)."""
    t = timeline()
    ok = t.DetectSceneCuts()
    return {"ok": bool(ok), "video_clips": len(track_items(t, "video", 1))}


@tool(timeout=3600)
def subtitles_from_audio(language: str = "auto", preset: str = "subtitle_default", chars_per_line: int = 42,
                         line_break: str = "single", gap: int = 0) -> dict:
    """Auto-caption: transcribe the timeline audio into a subtitle track (Studio). language: auto | english | french |
    german | spanish | russian | turkish | japanese | korean | mandarin_simplified … ; preset: subtitle_default |
    teletext | netflix; line_break: single | double; gap in frames."""
    t = timeline()
    r = resolve()
    s = {
        r.SUBTITLE_LANGUAGE: pick_const("AUTO_CAPTION_", language),
        r.SUBTITLE_CAPTION_PRESET: pick_const("AUTO_CAPTION_", preset),
        r.SUBTITLE_CHARS_PER_LINE: chars_per_line,
        r.SUBTITLE_LINE_BREAK: pick_const("AUTO_CAPTION_LINE_", line_break),
        r.SUBTITLE_GAP: gap,
    }
    ok = t.CreateSubtitlesFromAudio(s)
    if not ok:
        ok = t.CreateSubtitlesFromAudio({"language": s[r.SUBTITLE_LANGUAGE], "captionPreset": s[r.SUBTITLE_CAPTION_PRESET],
                                         "charsPerLine": chars_per_line, "lineBreak": s[r.SUBTITLE_LINE_BREAK], "gap": gap})
    return {"ok": bool(ok), "subtitle_tracks": t.GetTrackCount("subtitle")}


@tool()
def clips_select_info() -> dict:
    """Return the clips currently selected in the timeline UI (so the user can point at clips by selecting them)."""
    t = timeline()
    T = TL(t)
    return {"selected": [describe_item(t, it, T) for it in t.GetSelectedClips() or []]}
