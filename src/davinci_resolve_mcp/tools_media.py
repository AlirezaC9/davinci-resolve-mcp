"""Media pool and media storage tools."""

from __future__ import annotations

import glob
import os
import re
from typing import Any

from .core import (
    TL, describe_mpitem, fail, find_folder, find_mpitem, media_pool, pick_const, project, resolve, safe,
    source_frame, tc_to_frames, timeline, tool, walk_folder, clip_fps,
)

MEDIA_EXT = {".mov", ".mp4", ".m4v", ".mxf", ".avi", ".mkv", ".braw", ".r3d", ".ari", ".arx", ".dng", ".wav", ".mp3",
             ".aif", ".aiff", ".m4a", ".flac", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".exr", ".dpx", ".psd", ".gif",
             ".webm", ".mts", ".m2ts", ".crm", ".heic", ".webp", ".srt", ".ogg", ".aac", ".bmp", ".svg"}


def _expand(paths: list[str]) -> list[str]:
    out: list[str] = []
    for p in paths:
        p = os.path.expanduser(p)
        if any(ch in p for ch in "*?["):
            out.extend(sorted(glob.glob(p)))
        else:
            out.append(p)
    return out


@tool(timeout=600)
def media_import(paths: list[str], bin: str | None = None, sequence: bool = False, recursive: bool = False) -> dict:
    """Import files or folders into the media pool (into `bin`, e.g. 'Footage/Day1'; created if missing).
    Paths may contain globs ('~/Footage/*.mov'). Folders import their media (recursive=true walks sub-folders).
    sequence=true imports numbered images as one image sequence (pass one frame or a %04d pattern)."""
    mp = media_pool()
    folder = find_folder(bin, create=True)
    mp.SetCurrentFolder(folder)
    files: list[str] = []
    for p in _expand(paths):
        if os.path.isdir(p):
            if recursive:
                for dp, _, fns in os.walk(p):
                    files += [os.path.join(dp, f) for f in sorted(fns) if os.path.splitext(f)[1].lower() in MEDIA_EXT]
            else:
                files.append(p)
        elif os.path.exists(p) or "%" in p:
            files.append(p)
        else:
            fail(f"Path not found: {p}")
    if not files:
        fail("Nothing to import")
    items = None
    if sequence:
        infos = []
        for f in files:
            base = os.path.basename(f)
            m = re.search(r"(\d+)(\.\w+)$", base)
            if m and "%" not in f:
                d = os.path.dirname(f)
                prefix, digits, ext = base[:m.start(1)], m.group(1), m.group(2)
                pat = f"{prefix}%0{len(digits)}d{ext}"
                rx = re.compile("^" + re.escape(prefix) + r"(\d{%d})" % len(digits) + re.escape(ext) + "$")
                nums = sorted(int(mm.group(1)) for fn in os.listdir(d) if (mm := rx.match(fn)))
                infos.append({"FilePath": os.path.join(d, pat), "StartIndex": nums[0], "EndIndex": nums[-1]})
            else:
                infos.append({"FilePath": f})
        items = mp.ImportMedia(infos)
    else:
        items = mp.ImportMedia(files)
        if not items:
            items = mp.ImportMedia([{"FilePath": f} for f in files])
        if not items:
            items = resolve().GetMediaStorage().AddItemListToMediaPool([{"media": f} for f in files])
    if not items:
        fail("Import failed (unsupported files, or Resolve has no permission to read that location — "
             "macOS: System Settings > Privacy & Security > Files and Folders / Full Disk Access for DaVinci Resolve).")
    return {"bin": folder.GetName(), "imported": [describe_mpitem(c) for c in items]}


@tool(name="media_pool", read_only=True)
def media_pool_list(bin: str | None = None, recursive: bool = True, search: str | None = None, detail: bool = False,
               limit: int = 300) -> dict:
    """List bins and clips in the media pool (from `bin`, default root). `search` filters clip names
    (case-insensitive substring); detail=true adds all clip properties, metadata and markers."""
    root = find_folder(bin)
    out_bins = []
    n = 0
    walker = walk_folder(root) if recursive else [("", root)]
    for path, f in walker:
        clips = []
        for c in f.GetClipList() or []:
            if search and search.lower() not in c.GetName().lower():
                continue
            if n >= limit:
                break
            clips.append(describe_mpitem(c, detail))
            n += 1
        if clips or not search:
            out_bins.append({"bin": path or "/", "id": f.GetUniqueId(), "clips": clips,
                             "subbins": [s.GetName() for s in f.GetSubFolderList() or []]})
    cur = media_pool().GetCurrentFolder()
    return {"current_bin": cur.GetName() if cur else None, "bins": out_bins, "truncated": n >= limit or None}


@tool()
def bin_manage(action: str, path: str | None = None, items: list[str] | None = None, target: str | None = None,
               file: str | None = None) -> dict:
    """Bins/clips housekeeping. action:
    'create' (path, nested ok) | 'select' (make `path` the current bin) | 'move' (items → target bin; items may be clip refs
    or bin paths) | 'delete' (items = clips/timeline clips, or bin paths prefixed 'bin:') — irreversible |
    'export' (bin `path` → .drb `file`) | 'import' (.drb `file` into current bin) | 'refresh'."""
    mp = media_pool()
    a = action.lower()
    if a == "create":
        f = find_folder(path, create=True)
        return {"created": path, "id": f.GetUniqueId()}
    if a == "select":
        f = find_folder(path)
        return {"selected": bool(mp.SetCurrentFolder(f))}
    if a in ("move", "delete"):
        clips, bins = [], []
        for ref in items or []:
            if isinstance(ref, str) and ref.startswith("bin:"):
                bins.append(find_folder(ref[4:]))
            else:
                try:
                    clips.append(find_mpitem(ref))
                except Exception:  # noqa: BLE001
                    bins.append(find_folder(ref))
        if a == "move":
            dst = find_folder(target, create=True)
            ok = (not clips or mp.MoveClips(clips, dst)) and (not bins or mp.MoveFolders(bins, dst))
            return {"moved": len(clips) + len(bins), "ok": bool(ok)}
        ok = (not clips or mp.DeleteClips(clips)) and (not bins or mp.DeleteFolders(bins))
        return {"deleted": len(clips) + len(bins), "ok": bool(ok)}
    if a == "export":
        return {"ok": bool(find_folder(path).Export(file))}
    if a == "import":
        return {"ok": bool(mp.ImportFolderFromFile(file))}
    if a == "refresh":
        return {"ok": bool(mp.RefreshFolders())}
    fail("action must be create | select | move | delete | export | import | refresh")
    return {}


@tool(read_only=True)
def clip_info(item: str) -> dict:
    """Everything about one media pool clip: properties (codec, res, fps, TC, path…), metadata, markers, flags,
    in/out marks, audio mapping and transcription availability."""
    c = find_mpitem(item)
    d = describe_mpitem(c, detail=True)
    d["audio_mapping"] = safe(c.GetAudioMapping)
    tr = safe(c.GetTranscription)
    d["has_transcription"] = bool(tr and tr.get("segments"))
    return d


@tool()
def clip_update(items: list[str], name: str | None = None, color: str | None = None, clear_color: bool = False,
                add_flags: list[str] | None = None, clear_flags: str | None = None, metadata: dict | None = None,
                third_party_metadata: dict | None = None, properties: dict | None = None,
                mark_in: Any = None, mark_out: Any = None, clear_marks: bool = False) -> dict:
    """Modify media pool clips: rename (single item), clip color, flags, metadata ({"Scene":"12","Take":"3",
    "Description":..., "Keywords":...}), clip attributes/properties ({"Input Color Space":"Rec.709", "Super Scale":2,
    "Clip Name":...}), source in/out marks (seconds of source). clear_flags: a color or 'All'."""
    clips = [find_mpitem(i) for i in items]
    results = []
    tlfps = 24.0
    for c in clips:
        r: dict[str, Any] = {"id": c.GetUniqueId()}
        if name:
            r["name"] = bool(c.SetName(name))
        if color:
            r["color"] = bool(c.SetClipColor(color))
        if clear_color:
            r["clear_color"] = bool(c.ClearClipColor())
        for fl in add_flags or []:
            r.setdefault("flags", []).append(bool(c.AddFlag(fl)))
        if clear_flags:
            r["clear_flags"] = bool(c.ClearFlags(clear_flags))
        if metadata:
            r["metadata"] = bool(c.SetMetadata({k: str(v) for k, v in metadata.items()}))
        if third_party_metadata:
            r["third_party_metadata"] = bool(c.SetThirdPartyMetadata({k: str(v) for k, v in third_party_metadata.items()}))
        for k, v in (properties or {}).items():
            r.setdefault("properties", {})[k] = bool(c.SetClipProperty(k, v if isinstance(v, (int, float)) else str(v)))
        if clear_marks:
            r["clear_marks"] = bool(c.ClearMarkInOut())
        if mark_in is not None or mark_out is not None:
            fi = source_frame(c, mark_in, tlfps) if mark_in is not None else 0
            fo = source_frame(c, mark_out, tlfps) if mark_out is not None else int(c.GetClipProperty("Frames") or 0) - 1
            r["marks"] = bool(c.SetMarkInOut(fi, fo))
        results.append(r)
    return {"results": results}


@tool()
def media_link(items: list[str], action: str, path: str | None = None) -> dict:
    """Relink / replace / proxy operations. action: 'relink' (items → media in folder `path`), 'unlink',
    'replace' (single item → file `path`), 'replace_keep_subclip', 'proxy_link' (proxy file `path`),
    'proxy_unlink', 'full_res_link' (full-res file `path`), 'monitor_growing'."""
    mp = media_pool()
    clips = [find_mpitem(i) for i in items]
    a = action.lower()
    if a == "relink":
        return {"ok": bool(mp.RelinkClips(clips, path))}
    if a == "unlink":
        return {"ok": bool(mp.UnlinkClips(clips))}
    fns = {
        "replace": lambda c: c.ReplaceClip(path), "replace_keep_subclip": lambda c: c.ReplaceClipPreserveSubClip(path),
        "proxy_link": lambda c: c.LinkProxyMedia(path), "proxy_unlink": lambda c: c.UnlinkProxyMedia(),
        "full_res_link": lambda c: c.LinkFullResolutionMedia(path), "monitor_growing": lambda c: c.MonitorGrowingFile(),
    }
    if a not in fns:
        fail(f"action must be relink | unlink | {' | '.join(fns)}")
    return {"results": [{"id": c.GetUniqueId(), "ok": bool(fns[a](c))} for c in clips]}


@tool(read_only=True)
def media_storage(path: str | None = None) -> dict:
    """Browse disks as Resolve sees them: no path → mounted volumes; a folder → its sub-folders and media files
    (image sequences are consolidated)."""
    ms = resolve().GetMediaStorage()
    if not path:
        return {"volumes": ms.GetMountedVolumeList()}
    p = os.path.expanduser(path)
    return {"path": p, "folders": ms.GetSubFolderList(p), "files": ms.GetFileList(p)}


# ------------------------------------------------------------------------------ AI / analysis
@tool(timeout=3600)
def transcribe(items: list[str] | None = None, bin: str | None = None, speaker_detection: bool = False,
               clear: bool = False) -> dict:
    """Run Resolve's AI audio transcription on clips (or a whole bin) — needed for `transcript` and text-based editing.
    clear=true removes existing transcriptions instead. Can take a while for long media."""
    targets: list = []
    if bin is not None:
        f = find_folder(bin)
        ok = f.ClearTranscription() if clear else f.TranscribeAudio(speaker_detection)
        return {"bin": f.GetName(), "ok": bool(ok)}
    for i in items or []:
        c = find_mpitem(i)
        ok = c.ClearTranscription() if clear else c.TranscribeAudio(speaker_detection)
        targets.append({"id": c.GetUniqueId(), "name": c.GetName(), "ok": bool(ok)})
    if not targets:
        fail("Give items or bin")
    return {"results": targets}


def _tc_sec(tc: str, fps: float, start_frames: int) -> float:
    return round((tc_to_frames(tc, fps) - start_frames) / fps, 3)


@tool(read_only=True, timeout=3600)
def transcript(item: str, words: bool = False, transcribe_if_missing: bool = True) -> dict:
    """Get the transcription of a media clip as segments with start/end in SECONDS OF SOURCE (ready for
    clips_assemble in/out). words=true includes word-level timings. Transcribes first if needed."""
    c = find_mpitem(item)
    tr = safe(c.GetTranscription)
    if (not tr or not tr.get("segments")) and transcribe_if_missing:
        c.TranscribeAudio()
        tr = safe(c.GetTranscription)
    if not tr or not tr.get("segments"):
        fail("No transcription available (transcription may still be running, or the clip has no speech).")
    fps = clip_fps(c) or 24.0
    start = tc_to_frames(c.GetClipProperty("Start TC") or "00:00:00:00", fps)
    segs = []
    for s in tr["segments"]:
        d = {"start": _tc_sec(s["start"], fps, start), "end": _tc_sec(s["end"], fps, start), "text": s.get("text", "")}
        if s.get("speaker"):
            d["speaker"] = s["speaker"]
        if words:
            d["words"] = [{"t": w.get("text"), "s": _tc_sec(w["start"], fps, start), "e": _tc_sec(w["end"], fps, start)}
                          for w in s.get("words", [])]
        segs.append(d)
    return {"item": c.GetName(), "language": tr.get("language"), "fps": fps, "segments": segs}


@tool(timeout=3600)
def media_ai(task: str, items: list[str] | None = None, bin: str | None = None, options: dict | None = None) -> dict:
    """Studio AI analysis on clips or a bin. task: 'intellisearch' (options: faces bool, better bool),
    'slate' (options: marker_color), 'audio_classify', 'audio_classify_clear', 'deblur' (options: DeblurOptions dict,
    creates new clips)."""
    o = options or {}
    targets = [find_folder(bin)] if bin is not None else [find_mpitem(i) for i in items or []]
    if not targets:
        fail("Give items or bin")
    t = task.lower()
    res = []
    for x in targets:
        if t == "intellisearch":
            ok = x.AnalyzeForIntellisearch(bool(o.get("faces", False)), bool(o.get("better", False)))
        elif t == "slate":
            ok = x.AnalyzeForSlate(pick_const("MARKER_", o.get("marker_color", "Blue")))
        elif t == "audio_classify":
            ok = x.PerformAudioClassification()
        elif t == "audio_classify_clear":
            ok = x.ClearAudioClassification()
        elif t == "deblur":
            r = x.RemoveMotionBlur(o)
            ok = bool(r)
        else:
            fail("task must be intellisearch | slate | audio_classify | audio_classify_clear | deblur")
        res.append({"name": x.GetName(), "ok": bool(ok)})
    return {"results": res}


@tool()
def audio_sync(items: list[str], mode: str = "timecode", channel: Any = 1, retain_embedded_audio: bool = False,
               retain_video_metadata: bool = False) -> dict:
    """Auto-sync external audio to video clips (needs ≥1 video + ≥1 audio clip).
    mode: timecode | waveform | in | out | marker."""
    clips = [find_mpitem(i) for i in items]
    r = resolve()
    s = {
        r.AUDIO_SYNC_MODE: pick_const("AUDIO_SYNC_", mode),
        r.AUDIO_SYNC_CHANNEL_NUMBER: channel if isinstance(channel, int) else pick_const("AUDIO_SYNC_CHANNEL_", channel),
        r.AUDIO_SYNC_RETAIN_EMBEDDED_AUDIO: retain_embedded_audio,
        r.AUDIO_SYNC_RETAIN_VIDEO_METADATA: retain_video_metadata,
    }
    ok = media_pool().AutoSyncAudio(clips, s)
    if not ok:
        ok = media_pool().AutoSyncAudio(clips, {"syncMode": s[r.AUDIO_SYNC_MODE], "channelNumber": s[r.AUDIO_SYNC_CHANNEL_NUMBER],
                                                "retainEmbeddedAudio": retain_embedded_audio, "retainVideoMetadata": retain_video_metadata})
    return {"ok": bool(ok)}


@tool()
def multicam_create(items: list[str], name: str | None = None, sync: str = "timecode", audio_mode: str = "source",
                    angle_names: str = "sequential", start_timecode: str | None = None) -> dict:
    """Create a multicam clip from media pool clips. sync: in | out | timecode | audio | marker.
    audio_mode: adaptive | source | reference | all. angle_names: sequential | angle | camera | clip | file."""
    clips = [find_mpitem(i) for i in items]
    opts: dict[str, Any] = {
        "angleSyncMode": pick_const("MULTICAM_ANGLE_SYNC_", sync),
        "multicamAudioMode": pick_const("MULTICAM_AUDIO_", audio_mode),
        "angleNameMode": pick_const("MULTICAM_ANGLE_NAME_", angle_names),
    }
    if name:
        opts["name"] = name
    if start_timecode:
        opts["startTimecode"] = start_timecode
    res = media_pool().CreateMulticamClip(clips, opts)
    if not res:
        fail("CreateMulticamClip failed")
    return {"created": [describe_mpitem(c) for c in res]}


@tool()
def media_select(item: str | None = None) -> dict:
    """Get the media pool selection, or select one clip (useful before UI actions)."""
    mp = media_pool()
    if item:
        mp.SetSelectedClip(find_mpitem(item))
    return {"selected": [describe_mpitem(c) for c in mp.GetSelectedClips() or []]}


@tool()
def metadata_export(path: str, items: list[str] | None = None) -> dict:
    """Export clip metadata to CSV (all media pool clips if items not given)."""
    clips = [find_mpitem(i) for i in items] if items else None
    ok = media_pool().ExportMetadata(path, clips) if clips else media_pool().ExportMetadata(path)
    return {"ok": bool(ok), "path": path}
