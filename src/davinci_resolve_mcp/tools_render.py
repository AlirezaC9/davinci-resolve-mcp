"""Deliver page / render tools, visual previews and audio tools."""

from __future__ import annotations

import io
import os
import time
from datetime import datetime
from typing import Any

from mcp.server.mcpserver.utilities.types import Image

from .core import (
    TL, WORK_DIR, clip_fps, describe_item, fail, find_items, find_mpitem, media_pool, parse_track, pick_const, project,
    resolve, safe, source_frame, tc_to_frames, timeline, tool, track_items,
)


# ======================================================================================== render
@tool(read_only=True)
def render_options(format: str | None = None, codec: str | None = None) -> dict:
    """What can be rendered: formats (name → extension), codecs for `format` (extension like 'mp4', 'mov'),
    resolutions for format+codec, render presets, quick-export presets, current format/codec and render mode."""
    p = project()
    out: dict[str, Any] = {
        "current": safe(p.GetCurrentRenderFormatAndCodec),
        "mode": "single clip" if safe(p.GetCurrentRenderMode) == 1 else "individual clips",
        "presets": safe(p.GetRenderPresetList),
        "quick_export_presets": safe(p.GetQuickExportRenderPresets),
    }
    if format:
        out["codecs"] = safe(lambda: p.GetRenderCodecs(format))
        if codec:
            out["resolutions"] = safe(lambda: p.GetRenderResolutions(format, codec))
        out["audio_codecs"] = safe(lambda: p.GetAudioRenderCodecs(format))
    else:
        out["formats"] = safe(p.GetRenderFormats)
        out["audio_formats"] = safe(p.GetAudioRenderFormats)
    return out


def _job_add(target_dir: str | None, name: str | None, preset: str | None, format: str | None, codec: str | None,
             start: Any, end: Any, settings: dict | None, single_clip: bool, timeline_name: str | None) -> tuple[str, dict]:
    p = project()
    if timeline_name:
        from .core import timeline as _tl
        p.SetCurrentTimeline(_tl(timeline_name))
    tl = timeline()
    T = TL(tl)
    if preset and not p.LoadRenderPreset(preset):
        fail(f"Unknown render preset '{preset}'. Presets: {p.GetRenderPresetList()}")
    if format or codec:
        cur = p.GetCurrentRenderFormatAndCodec() or {}
        f = format or cur.get("format")
        c = codec or cur.get("codec")
        if not p.SetCurrentRenderFormatAndCodec(f, c):
            fail(f"Bad format/codec {f}/{c}. Check render_options(format='{f}').")
    p.SetCurrentRenderMode(1 if single_clip else 0)
    s: dict[str, Any] = {}
    d = os.path.expanduser(target_dir) if target_dir else str(WORK_DIR / "renders")
    os.makedirs(d, exist_ok=True)
    s["TargetDir"] = d
    if name:
        s["CustomName"] = name
    if start is not None or end is not None:
        s["SelectAllFrames"] = False
        s["MarkIn"] = T.frame(start) if start is not None else T.start
        s["MarkOut"] = (T.frame(end) - 1) if end is not None else int(tl.GetEndFrame()) - 1
    elif not (settings or {}).get("MarkIn"):
        s["SelectAllFrames"] = True
    s.update(settings or {})
    if not p.SetRenderSettings(s):
        bad = [k for k, v in s.items() if not p.SetRenderSettings({k: v})]
        fail(f"Render settings rejected: {bad}")
    jid = p.AddRenderJob()
    if not jid:
        fail("AddRenderJob failed (Deliver settings invalid?)")
    return jid, s


@tool()
def render_job_add(target_dir: str | None = None, name: str | None = None, preset: str | None = None,
                   format: str | None = None, codec: str | None = None, start: Any = None, end: Any = None,
                   settings: dict | None = None, single_clip: bool = True, timeline_name: str | None = None) -> dict:
    """Queue a render job for the current timeline. preset: a render preset ('H.264 Master', 'YouTube - 1080p',
    'TikTok - 1080p', 'ProRes 422 HQ' …); or format ('mp4','mov','mxf'…) + codec ('H264','H265','ProRes422HQ'…).
    start/end = range (default whole timeline). settings = extra RenderSettings (FormatWidth, FormatHeight, FrameRate,
    VideoQuality, ExportAudio, ExportVideo, AudioCodec, AudioBitDepth, ExportAlpha, EncodingProfile, ExportSubtitle,
    SubtitleFormat 'BurnIn', UseFullExtents, AddFrameHandles …). Output defaults to ~/Documents/ResolveMCP/renders."""
    jid, s = _job_add(target_dir, name, preset, format, codec, start, end, settings, single_clip, timeline_name)
    return {"job_id": jid, "settings": s}


def _job_info(p, jid: str) -> dict:
    info = next((j for j in p.GetRenderJobList() or [] if j.get("JobId") == jid), {})
    st = p.GetRenderJobStatus(jid) or {}
    out = {"job_id": jid, "status": st.get("JobStatus"), "percent": st.get("CompletionPercentage")}
    if info:
        out["timeline"] = info.get("TimelineName")
        out["output"] = os.path.join(info.get("TargetDir", ""), info.get("OutputFilename", ""))
    if st.get("Error"):
        out["error"] = st["Error"]
    if st.get("TimeTakenToRenderInMs"):
        out["seconds"] = round(st["TimeTakenToRenderInMs"] / 1000, 1)
    return out


@tool(timeout=3700)
def render_start(job_ids: list[str] | None = None, wait: bool = True, timeout: float = 3600) -> dict:
    """Start rendering queued jobs (all if job_ids omitted). wait=true blocks until done and returns output files;
    wait=false returns immediately (poll with render_status)."""
    p = project()
    ids = job_ids or [j["JobId"] for j in p.GetRenderJobList() or []]
    if not ids:
        fail("No render jobs queued")
    resolve().OpenPage("deliver")
    if not p.StartRendering(ids):
        fail("StartRendering failed")
    if not wait:
        return {"started": ids}
    t0 = time.time()
    time.sleep(1)
    while p.IsRenderingInProgress() and time.time() - t0 < timeout:
        time.sleep(1.5)
    return {"jobs": [_job_info(p, j) for j in ids], "elapsed": round(time.time() - t0, 1),
            "timed_out": p.IsRenderingInProgress() or None}


@tool(read_only=True)
def render_status(job_id: str | None = None) -> dict:
    """Status of one render job or of the whole queue (status, percent, output path, errors)."""
    p = project()
    ids = [job_id] if job_id else [j["JobId"] for j in p.GetRenderJobList() or []]
    return {"rendering": p.IsRenderingInProgress(), "jobs": [_job_info(p, j) for j in ids]}


@tool()
def render_jobs(action: str = "list", job_id: str | None = None) -> dict:
    """Render queue management. action: list | delete (job_id) | clear (all) | stop (current render)."""
    p = project()
    a = action.lower()
    if a == "delete":
        p.DeleteRenderJob(job_id)
    elif a == "clear":
        p.DeleteAllRenderJobs()
    elif a == "stop":
        p.StopRendering()
    return {"jobs": p.GetRenderJobList()}


@tool(timeout=3700)
def render_timeline(target_dir: str | None = None, name: str | None = None, preset: str | None = "H.264 Master",
                    format: str | None = None, codec: str | None = None, start: Any = None, end: Any = None,
                    width: int | None = None, height: int | None = None, settings: dict | None = None,
                    wait: bool = True, timeout: float = 3600) -> dict:
    """One-shot export of the current timeline (queue + render + wait). E.g. preset 'YouTube - 1080p', or
    format='mp4', codec='H265'; width/height to override resolution; start/end for a range."""
    s = dict(settings or {})
    if width:
        s["FormatWidth"] = int(width)
    if height:
        s["FormatHeight"] = int(height)
    nm = name or f"{timeline().GetName()}_{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    jid, _ = _job_add(target_dir, nm, preset, format, codec, start, end, s, True, None)
    return render_start([jid], wait, timeout)


@tool(timeout=3700)
def quick_export(preset: str = "H.264", target_dir: str | None = None, name: str | None = None) -> dict:
    """Render the current timeline with a Quick Export preset (see render_options quick_export_presets)."""
    p = project()
    s: dict[str, Any] = {"TargetDir": os.path.expanduser(target_dir) if target_dir else str(WORK_DIR / "renders")}
    if name:
        s["CustomName"] = name
    return {"result": p.RenderWithQuickExport(preset, s)}


# ======================================================================================== previews
def _export_frame() -> str:
    r = resolve()
    p = project()
    page = r.GetCurrentPage()
    switched = None
    if page not in ("edit", "color", "cut"):
        switched = page
        r.OpenPage("edit")
        time.sleep(0.4)
    path = str(WORK_DIR / "previews" / f"frame_{int(time.time() * 1000)}.png")
    ok = False
    for attempt in range(5):  # right after a timeline switch the viewer needs a moment
        ok = p.ExportCurrentFrameAsStill(path) and os.path.exists(path)
        if ok:
            break
        time.sleep(0.4 * (attempt + 1))
    if switched:
        r.OpenPage(switched)
    if not ok or not os.path.exists(path):
        fail("Could not export the current frame (is a timeline open with a clip under the playhead?)")
    return path


def _to_jpeg(path: str, width: int) -> bytes:
    from PIL import Image as PI

    im = PI.open(path).convert("RGB")
    if im.width > width:
        im = im.resize((width, int(im.height * width / im.width)), PI.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue()


@tool(read_only=True)
def frame_preview(time: Any = None, width: int = 960, keep_playhead: bool = False) -> list:
    """Render the timeline frame at `time` (default: playhead) as an image you can see — use it to verify edits,
    titles, grades and composites. Full-resolution PNG is kept in ~/Documents/ResolveMCP/previews."""
    tl = timeline()
    T = TL(tl)
    keep = tl.GetCurrentTimecode()
    if time is not None:
        tl.SetCurrentTimecode(T.tc(T.frame(time)))
    cur = tl.GetCurrentTimecode()
    path = _export_frame()
    if keep_playhead:
        tl.SetCurrentTimecode(keep)
    f = tc_to_frames(cur, T.fps, T.drop)
    item = safe(tl.GetCurrentVideoItem)
    return [Image(data=_to_jpeg(path, width), format="jpeg"),
            {"time": T.sec(f), "tc": cur, "clip": item.GetName() if item else None, "file": path}]


@tool(read_only=True)
def frames_contact(times: list[Any] | None = None, count: int = 6, columns: int = 3, width: int = 1500,
                   start: Any = None, end: Any = None) -> list:
    """Contact sheet of several timeline frames in ONE image (labelled with their times). Give explicit `times`, or
    `count` frames spread evenly between start/end (default whole timeline). Great for reviewing a whole edit."""
    from PIL import Image as PI, ImageDraw

    tl = timeline()
    T = TL(tl)
    keep = tl.GetCurrentTimecode()
    if not times:
        f0 = T.frame(start) if start is not None else T.start
        f1 = T.frame(end) if end is not None else int(tl.GetEndFrame()) - 1
        n = max(1, count)
        times = [T.sec(f0 + (f1 - f0) * (i + 0.5) / n) for i in range(n)]
    tiles = []
    for t in times:
        tl.SetCurrentTimecode(T.tc(T.frame(t)))
        tiles.append((t, PI.open(_export_frame()).convert("RGB")))
    tl.SetCurrentTimecode(keep)
    cols = max(1, min(columns, len(tiles)))
    rows = (len(tiles) + cols - 1) // cols
    tw = width // cols
    th = int(tw * tiles[0][1].height / tiles[0][1].width)
    sheet = PI.new("RGB", (tw * cols, (th + 22) * rows), (20, 20, 20))
    dr = ImageDraw.Draw(sheet)
    for k, (t, im) in enumerate(tiles):
        x, y = (k % cols) * tw, (k // cols) * (th + 22)
        sheet.paste(im.resize((tw - 4, th), PI.LANCZOS), (x + 2, y + 20))
        dr.text((x + 6, y + 4), f"{t}s" if isinstance(t, (int, float)) else str(t), fill=(255, 220, 0))
    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=82)
    return [Image(data=buf.getvalue(), format="jpeg"), {"times": times}]


@tool(read_only=True)
def media_frames(item: str, times: list[Any] | None = None, count: int = 6, columns: int = 3, width: int = 1500) -> list:
    """Look at frames of a SOURCE media clip (before it is edited in): contact sheet at source `times` (seconds) or
    `count` evenly spread frames. Uses a temporary timeline that is deleted afterwards."""
    from PIL import Image as PI, ImageDraw

    p = project()
    mp = media_pool()
    c = find_mpitem(item)
    cur_tl = p.GetCurrentTimeline()
    keep_bin = mp.GetCurrentFolder()
    stale = [p.GetTimelineByIndex(i) for i in range(1, p.GetTimelineCount() + 1)]
    stale = [t for t in stale if t.GetName().startswith("__mcp_preview_")]
    if cur_tl and cur_tl.GetName().startswith("__mcp_preview_"):
        cur_tl = next((p.GetTimelineByIndex(i) for i in range(1, p.GetTimelineCount() + 1)
                       if not p.GetTimelineByIndex(i).GetName().startswith("__")), None)
    if stale:
        mp.DeleteTimelines(stale)
    tmp = mp.CreateEmptyTimeline(f"__mcp_preview_{int(time.time())}")
    if not tmp:
        fail("Could not create preview timeline")
    try:
        p.SetCurrentTimeline(tmp)
        # NOTE: never change fps/resolution here — SetSettings(timelineFrameRate) can deadlock Resolve's scripting
        res = mp.AppendToTimeline([{"mediaPoolItem": c, "mediaType": 1}]) or mp.AppendToTimeline([{"mediaPoolItem": c}])
        if not res:
            fail("Could not place the clip (audio-only?)")
        T = TL(tmp)
        fps = clip_fps(c) or T.fps
        dur = (res[0].GetEnd() - res[0].GetStart()) / T.fps
        if not times:
            n = max(1, count)
            times = [round(dur * (i + 0.5) / n, 2) for i in range(n)]
        tiles = []
        for t in times:
            sf = source_frame(c, t, T.fps) / fps
            tmp.SetCurrentTimecode(T.tc(T.start + int(round(sf * T.fps))))
            tiles.append((t, PI.open(_export_frame()).convert("RGB")))
    finally:
        if cur_tl:
            p.SetCurrentTimeline(cur_tl)
        mp.DeleteTimelines([tmp])
        mp.SetCurrentFolder(keep_bin)
    cols = max(1, min(columns, len(tiles)))
    rows = (len(tiles) + cols - 1) // cols
    tw = width // cols
    th = int(tw * tiles[0][1].height / tiles[0][1].width)
    sheet = PI.new("RGB", (tw * cols, (th + 22) * rows), (20, 20, 20))
    dr = ImageDraw.Draw(sheet)
    for k, (t, im) in enumerate(tiles):
        x, y = (k % cols) * tw, (k // cols) * (th + 22)
        sheet.paste(im.resize((tw - 4, th), PI.LANCZOS), (x + 2, y + 20))
        dr.text((x + 6, y + 4), f"src {t}s", fill=(0, 220, 255))
    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=82)
    return [Image(data=buf.getvalue(), format="jpeg"), {"item": c.GetName(), "times": times, "duration": round(dur, 3)}]


# ======================================================================================== audio
@tool()
def audio_normalize(clips: Any, mode: str = "Sample Peak Program", target_level: float | None = -1.0,
                    target_loudness: float | None = None, independent: bool = False) -> dict:
    """Normalize audio clip levels. mode: one of the timeline's normalize modes (e.g. 'Sample Peak Program',
    'True Peak Program', 'ITU-R BS.1770-4', 'EBU R128' — see result.modes). target_level dBFS for peak modes,
    target_loudness LKFS/LUFS for loudness modes (e.g. -14 for YouTube, -23 broadcast). independent=true sets each clip
    separately instead of relative."""
    tl = timeline()
    items = find_items(tl, clips)
    opts: dict[str, Any] = {"normalizationMode": mode,
                            "setLevelMode": pick_const("NORMALIZE_AUDIO_SET_LEVEL_", "independent" if independent else "relative")}
    if target_level is not None:
        opts["targetLevel"] = float(target_level)
    if target_loudness is not None:
        opts["targetLoudness"] = float(target_loudness)
    ok = tl.NormalizeAudioLevel(items, opts)
    return {"ok": bool(ok), "count": len(items), "modes": safe(tl.GetNormalizeAudioModes)}


@tool()
def voice_isolation(target: Any, enabled: bool = True, amount: int = 80) -> dict:
    """AI Voice Isolation (Studio) on an audio track ('A1') or on timeline clips (refs)."""
    tl = timeline()
    state = {"isEnabled": bool(enabled), "amount": int(amount)}
    if isinstance(target, str) and target.upper().startswith("A") and ":" not in target:
        _, ti = parse_track(target)
        return {"track": target, "ok": bool(tl.SetVoiceIsolationState(ti, state)), "state": safe(lambda: tl.GetVoiceIsolationState(ti))}
    out = []
    for it in find_items(tl, target):
        out.append({"clip": it.GetName(), "ok": bool(it.SetVoiceIsolationState(state))})
    return {"clips": out}


@tool(timeout=600)
def tts_generate(text: str, voice: str = "Female 1", speed: float = 0, pitch: float = 0, variation: float = 0.5,
                 add_to_timeline: bool = True, audio_track: int = 0, filename: str | None = None,
                 custom_voice_file: str | None = None, at: Any = None) -> dict:
    """AI Speech Generator (Studio + downloaded extra): synthesize speech (max ~350 chars per call) into the media pool
    and optionally onto the timeline at `at` (default playhead) on audio_track (0 = new track).
    voice: 'Female 1', 'Male 1', … or 'Custom Voice' with custom_voice_file."""
    p = project()
    tl = timeline(required=False)
    if at is not None and tl:
        T = TL(tl)
        tl.SetCurrentTimecode(T.tc(T.frame(at)))
    s: dict[str, Any] = {"TextInput": text, "VoiceModel": voice, "Speed": float(speed), "Pitch": float(pitch),
                         "Variation": float(variation), "AddToTimeline": add_to_timeline, "AudioTrack": int(audio_track)}
    if filename:
        s["Filename"] = filename
    if custom_voice_file:
        s["CustomVoiceFile"] = custom_voice_file
    res = None
    try:
        res = p.GenerateSpeech(s)
    except Exception:  # noqa: BLE001
        res = None
    if not res and tl:
        try:
            res = p.GenerateSpeech(s, tl.GetCurrentTimecode())
        except Exception:  # noqa: BLE001
            res = None
    if not res:
        fail("GenerateSpeech failed — requires Resolve Studio and the 'AI Speech Generator' extra "
             "(DaVinci Resolve > Extras Download Manager).")
    return {"created": res.GetName() if hasattr(res, "GetName") else str(res)}


@tool()
def audio_insert(path: str, at: Any = None, start_offset: float = 0, duration: float | None = None,
                 sample_rate: int = 48000) -> dict:
    """Insert an audio file into the current audio track at `at` (default playhead) — Fairlight-style insert.
    start_offset/duration in seconds of the file."""
    p = project()
    tl = timeline()
    T = TL(tl)
    if at is not None:
        tl.SetCurrentTimecode(T.tc(T.frame(at)))
    dur = int((duration or 36000) * sample_rate)
    ok = p.InsertAudioToCurrentTrackAtPlayhead(path, int(start_offset * sample_rate), dur)
    if not ok:
        fail("Insert failed (open the Fairlight page and select a target track, or use media_import + clips_place)")
    return {"ok": True}


@tool()
def audio_mapping(target: str, mapping: dict | None = None, timeline_clip: bool = False) -> dict:
    """Get or set audio channel mapping (JSON as in Resolve's Clip Attributes) for a media pool clip, or for a timeline
    clip with timeline_clip=true. Set only needs {"track_mapping": {...}}."""
    import json
    if timeline_clip:
        from .core import find_item
        obj = find_item(timeline(), target)
        if mapping:
            if not obj.SetSourceAudioChannelMapping(json.dumps(mapping)):
                fail("SetSourceAudioChannelMapping failed")
        raw = obj.GetSourceAudioChannelMapping()
    else:
        obj = find_mpitem(target)
        if mapping:
            if not obj.SetAudioMapping(json.dumps(mapping)):
                fail("SetAudioMapping failed")
        raw = obj.GetAudioMapping()
    try:
        return {"mapping": json.loads(raw)}
    except Exception:  # noqa: BLE001
        return {"mapping": raw}
