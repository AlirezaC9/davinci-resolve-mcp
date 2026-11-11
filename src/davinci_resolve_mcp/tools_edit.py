"""Editing tools. Resolve's scripting API has no razor/trim/move, so those are implemented by rebuilding the
affected clips from their source media (properties, fades, grades, Fusion comps, color and enabled state are
carried over)."""

from __future__ import annotations

import os
import re
import sys
import tempfile
from typing import Any

from .core import (
    TL, WORK_DIR, all_tracks, clip_fps, describe_item, ensure_track, fail, find_folder, find_item, find_items,
    find_mpitem, media_pool, parse_track, pick_const, project, resolve, safe, source_frame, timeline, tool,
    track_items, track_label,
)

# ======================================================================================== placement
def _append(mpi, ttype: str, tidx: int, rec: int, s_in: float, s_out: float):
    """Place [s_in, s_out) source frames of mpi on track at absolute record frame. Returns the item."""
    tl = timeline()
    ensure_track(tl, ttype, tidx)
    info = {"mediaPoolItem": mpi, "startFrame": s_in, "endFrame": s_out, "trackIndex": tidx, "recordFrame": rec,
            "mediaType": 1 if ttype == "video" else 2}
    res = media_pool().AppendToTimeline([info])
    if not res:
        fail(f"Could not place '{mpi.GetName()}' on {track_label(ttype, tidx)} (locked track or bad range?)")
    return res[0]


def timeline_is_empty(tl) -> bool:
    return not any(track_items(tl, tt, ti) for tt, ti in all_tracks(tl))


def _media_has(mpi, kind: str) -> bool:
    typ = (mpi.GetClipProperty("Type") or "").lower()
    if kind == "video":
        return "video" in typ or "still" in typ or "image" in typ or "compound" in typ or "timeline" in typ or "generator" in typ or typ == ""
    return "audio" in typ


def _media_len(mpi) -> int:
    try:
        return int(float(mpi.GetClipProperty("Frames") or 0))
    except ValueError:
        return 0


def _track_end(tl, ttype: str, tidx: int) -> int:
    items = track_items(tl, ttype, tidx) if tidx <= tl.GetTrackCount(ttype) else []
    return max([int(i.GetEnd()) for i in items], default=int(tl.GetStartFrame()))


def _place_segment(seg: dict, default_track: str = "V1", default_at: int | None = None) -> list:
    tl = timeline()
    T = TL(tl)
    mpi = find_mpitem(seg.get("media") or seg.get("item"))
    kind = (seg.get("type") or "both").lower()
    vt = parse_track(seg.get("track", default_track))
    if vt[0] == "audio":
        kind = "audio" if kind == "both" and not seg.get("video_track") else kind
    v_idx = parse_track(seg.get("video_track"))[1] if seg.get("video_track") else (vt[1] if vt[0] == "video" else 1)
    a_idx = parse_track(seg["audio_track"])[1] if seg.get("audio_track") else (vt[1] if vt[0] == "audio" else v_idx)
    fps = clip_fps(mpi) or T.fps
    total = _media_len(mpi)
    s_in = source_frame(mpi, seg["in"], T.fps) if seg.get("in") is not None else 0
    if seg.get("out") is not None:
        s_out = source_frame(mpi, seg["out"], T.fps)
    elif seg.get("duration") is not None:
        s_out = s_in + int(round(float(seg["duration"]) * fps))
    else:
        s_out = total or s_in + int(round(5 * fps))
    if total and s_out > total:
        s_out = total
    if s_out <= s_in:
        fail(f"Empty range for '{mpi.GetName()}' (in={seg.get('in')} out={seg.get('out')})")
    if seg.get("at") is not None:
        rec = T.frame(seg["at"])
    elif default_at is not None:
        rec = default_at
    else:
        rec = max(_track_end(tl, "video", v_idx) if kind in ("both", "video") else 0,
                  _track_end(tl, "audio", a_idx) if kind in ("both", "audio") else 0)
    placed = []
    want_v = kind in ("both", "video") and _media_has(mpi, "video")
    want_a = kind in ("both", "audio") and _media_has(mpi, "audio")
    if not want_v and not want_a:
        want_v = kind != "audio"
        want_a = kind == "audio"
    if want_v:
        placed.append(_append(mpi, "video", v_idx, rec, s_in, s_out))
    if want_a:
        placed.append(_append(mpi, "audio", a_idx, rec, s_in, s_out))
    if len(placed) == 2:
        tl.SetClipsLinked(placed, True)
    return placed


@tool()
def clips_place(segments: list[dict]) -> dict:
    """Place media on the timeline at exact positions (overwrite-free: the target area should be empty — use
    range_remove first otherwise). Each segment: {"media": ref, "in": src_sec, "out": src_sec (or "duration"),
    "at": timeline time (default: end of the target track), "track": "V2" (or "A3" for audio-only),
    "audio_track": "A2", "type": "both"|"video"|"audio"}. Video+audio are placed linked."""
    tl = timeline()
    T = TL(tl)
    out = []
    for seg in segments:
        for it in _place_segment(seg):
            out.append(describe_item(tl, it, T))
    return {"placed": out}


@tool()
def clips_assemble(segments: list[dict], track: str = "V1", start: Any = None, gap: float = 0,
                   audio_track: str | None = None) -> dict:
    """Build a sequence: append segments back-to-back on `track` (video+linked audio), starting at `start`
    (default: end of the track) with optional `gap` seconds between. Segment = {"media": ref, "in": src_sec,
    "out": src_sec} (in/out optional = whole clip), may override "type"/"track". Perfect for transcript-based cuts."""
    tl = timeline()
    T = TL(tl)
    _, v_idx = parse_track(track)
    a_idx = parse_track(audio_track)[1] if audio_track else v_idx
    if start is not None:
        cur = T.frame(start)
    else:
        tt = parse_track(track)[0]
        cur = max(_track_end(tl, tt, v_idx), _track_end(tl, "audio", a_idx) if tt == "video" else 0)
    gap_f = int(round(T.delta(gap))) if gap else 0
    out = []
    for seg in segments:
        s = {"track": track, **seg}
        if audio_track and "audio_track" not in s:
            s["audio_track"] = audio_track
        s.pop("at", None)
        items = _place_segment(s, default_at=cur)
        cur = max(int(i.GetEnd()) for i in items) + gap_f
        out.extend(describe_item(tl, i, T) for i in items)
    return {"placed": out, "sequence_end": T.sec(cur - gap_f)}


@tool()
def clips_delete(clips: Any, ripple: bool = False, with_linked: bool = True) -> dict:
    """Delete timeline clips (refs, ids, 'selected' or a whole track 'V2'). ripple=true closes the gap.
    Linked audio/video is deleted too unless with_linked=false."""
    tl = timeline()
    items = find_items(tl, clips)
    if with_linked:
        seen = {i.GetUniqueId() for i in items}
        for i in list(items):
            for l in safe(i.GetLinkedItems, []) or []:
                if l.GetUniqueId() not in seen:
                    items.append(l)
                    seen.add(l.GetUniqueId())
    names = [i.GetName() for i in items]
    if not tl.DeleteClips(items, ripple):
        fail("DeleteClips failed (locked track?)")
    return {"deleted": len(items), "names": names, "ripple": ripple}


# ======================================================================================== rebuild engine
class _Snap:
    def __init__(self, tl, it, T: TL):
        self.it = it
        self.mpi = it.GetMediaPoolItem()
        if not self.mpi:
            fail(f"'{it.GetName()}' has no source media (title/generator/compound?) — it cannot be cut or moved by script. "
                 "Delete and re-create it instead (title_add / generator_add).")
        tt, ti = it.GetTrackTypeAndIndex()
        self.tt, self.ti = tt, int(ti)
        if safe(lambda: tl.GetIsTrackLocked(tt, self.ti), False):
            fail(f"Track {track_label(tt, self.ti)} is locked")
        self.start, self.end = int(it.GetStart()), int(it.GetEnd())
        self.s_in = float(it.GetSourceStartFrame())
        sp = safe(it.GetSpeed, {}) or {}
        pct = float(sp.get("Percentage", 100) or 100)
        if abs(pct - 100) > 0.01:
            fail(f"'{it.GetName()}' has a speed change ({pct}%) — set it back to 100% (clip_speed) before cutting/moving it.")
        self.ratio = (clip_fps(self.mpi) or T.fps) / T.fps  # source frames per timeline frame
        self.props = safe(it.GetProperties, {}) or {}
        self.fades = safe(it.GetFades, {}) or {}
        self.enabled = safe(it.GetClipEnabled, True)
        self.color = safe(it.GetClipColor, "")
        self.uid = it.GetUniqueId()
        self.linked_ids = {l.GetUniqueId() for l in safe(it.GetLinkedItems, []) or []}
        self.comps: list[str] = []
        if tt == "video" and (safe(it.GetFusionCompCount, 0) or 0) > 0:
            for k in range(1, it.GetFusionCompCount() + 1):
                p = os.path.join(tempfile.mkdtemp(dir=WORK_DIR / "tmp"), f"comp{k}.comp")
                if it.ExportFusionComp(p, k):
                    self.comps.append(p)

    def src(self, abs_frame: float) -> float:
        return self.s_in + (abs_frame - self.start) * self.ratio


def _group(tl, items: list) -> list[list]:
    """Group items with their linked partners."""
    groups: list[list] = []
    seen: set[str] = set()
    for it in items:
        if it.GetUniqueId() in seen:
            continue
        g = [it] + [l for l in safe(it.GetLinkedItems, []) or [] if l]
        g = [x for x in g if x.GetUniqueId() not in seen]
        for x in g:
            seen.add(x.GetUniqueId())
        groups.append(g)
    return groups


def _apply_state(new, snap: _Snap, fade_in: bool, fade_out: bool) -> None:
    if snap.props:
        props = dict(snap.props)
        try:
            if not new.SetProperties(props):
                for k, v in props.items():
                    try:
                        new.SetProperties({k: v})
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            pass
    fd = {}
    if fade_in and snap.fades.get("FadeIn"):
        fd["FadeIn"] = int(snap.fades["FadeIn"])
    if fade_out and snap.fades.get("FadeOut"):
        fd["FadeOut"] = int(snap.fades["FadeOut"])
    if fd:
        safe(lambda: new.SetFades(fd))
    if not snap.enabled:
        safe(lambda: new.SetClipEnabled(False))
    if snap.color:
        safe(lambda: new.SetClipColor(snap.color))
    for p in snap.comps:
        safe(lambda: new.ImportFusionComp(p))


def _rebuild(tl, plans: list[tuple[_Snap, list[dict]]], keep_grades: bool = True) -> list[list]:
    """plans: (snap, [ {rec, from, to, track} ...]) ; from/to are ABSOLUTE frames of the original placement.
    Returns per-snap list of new items."""
    T = TL(tl)
    # 1. hold grades of video items on a temporary track
    holders: dict[str, Any] = {}
    temp_track = None
    if keep_grades and any(s.tt == "video" for s, _ in plans):
        tl.AddTrack("video")
        temp_track = tl.GetTrackCount("video")
        rec = int(tl.GetEndFrame()) + 10
        for s, _ in plans:
            if s.tt != "video":
                continue
            h = _append(s.mpi, "video", temp_track, rec, s.s_in, s.s_in + max(1.0, s.ratio))
            rec = int(h.GetEnd()) + 2
            if s.it.CopyGrades([h]):
                holders[s.uid] = h
    # 2. delete originals
    originals = [s.it for s, _ in plans]
    if not tl.DeleteClips(originals, False):
        if temp_track:
            tl.DeleteClips(track_items(tl, "video", temp_track), False)
            tl.DeleteTrack("video", temp_track)
        fail("Could not remove the original clips (locked?)")
    # 3. place new segments (later segments first so earlier ones can't be trimmed by rounding overlaps)
    results: list[list] = []
    for s, segs in plans:
        new_items: list = [None] * len(segs)
        order = sorted(range(len(segs)), key=lambda k: -segs[k]["rec"])
        for k in order:
            sg = segs[k]
            if sg["to"] <= sg["from"]:
                continue
            a, b = s.src(sg["from"]), s.src(sg["to"])
            if a < 0:
                fail(f"Not enough source media before '{s.mpi.GetName()}' for this edit")
            it = _append(s.mpi, s.tt, sg.get("track", s.ti), sg["rec"], a, b)
            new_items[k] = it
            _apply_state(it, s, fade_in=sg["from"] <= s.start, fade_out=sg["to"] >= s.end)
            h = holders.get(s.uid)
            if h:
                h.CopyGrades([it])
        results.append(new_items)
    # 4. cleanup temp
    if temp_track:
        tl.DeleteClips(track_items(tl, "video", temp_track), False)
        tl.DeleteTrack("video", temp_track)
    # 5. relink pieces that were linked before (same segment index)
    by_uid = {s.uid: (s, res) for (s, _), res in zip(plans, results)}
    done: set[str] = set()
    for s, res in by_uid.values():
        if s.uid in done or not s.linked_ids:
            continue
        partners = [by_uid[u] for u in s.linked_ids if u in by_uid]
        for _, pres in partners:
            done.update(p.uid for p, r in by_uid.values() if r is pres)
        done.add(s.uid)
        for k in range(len(res)):
            grp = [res[k]] + [pres[k] for _, pres in partners if k < len(pres) and pres[k]]
            grp = [g for g in grp if g]
            if len(grp) > 1:
                tl.SetClipsLinked(grp, True)
    return results


def _items_at(tl, frame: int, tracks: list[str] | None):
    want = [parse_track(t) for t in tracks] if tracks else list(all_tracks(tl, ("video", "audio")))
    out = []
    for tt, ti in want:
        if ti > tl.GetTrackCount(tt) or safe(lambda: tl.GetIsTrackLocked(tt, ti), False):
            continue
        for it in track_items(tl, tt, ti):
            if it.GetStart() < frame < it.GetEnd() and safe(it.GetType) != "transition":
                out.append(it)
    return out


def _razor(tl, frame: int, tracks: list[str] | None, keep_grades: bool = True) -> list:
    T = TL(tl)
    items = _items_at(tl, frame, tracks)
    if not items:
        return []
    groups = _group(tl, items)
    plans = []
    for g in groups:
        for it in g:
            s = _Snap(tl, it, T)
            if s.start < frame < s.end:
                plans.append((s, [{"rec": s.start, "from": s.start, "to": frame},
                                  {"rec": frame, "from": frame, "to": s.end}]))
    return _rebuild(tl, plans, keep_grades)


@tool()
def razor(times: list[Any], tracks: list[str] | None = None, keep_grades: bool = True) -> dict:
    """Cut (blade) clips at one or more timeline times. tracks: limit to e.g. ["V1","A1"] (default: all unlocked
    video+audio tracks; linked clips are always cut together). Grades/properties/fades/comps are preserved."""
    tl = timeline()
    T = TL(tl)
    frames = sorted({T.frame(t) for t in times}, reverse=True)
    cuts = 0
    for f in frames:
        cuts += len(_razor(tl, f, tracks, keep_grades))
    return {"cuts": cuts, "at": [T.sec(f) for f in sorted(frames)]}


def _check_free(tl, ttype: str, tidx: int, f0: int, f1: int, ignore: set[str]) -> None:
    if tidx > tl.GetTrackCount(ttype):
        return
    for it in track_items(tl, ttype, tidx):
        if it.GetUniqueId() in ignore or safe(it.GetType) == "transition":
            continue
        if it.GetStart() < f1 and it.GetEnd() > f0:
            fail(f"{track_label(ttype, tidx)} is occupied by '{it.GetName()}' between {TL(tl).sec(max(f0, it.GetStart()))}s and "
                 f"{TL(tl).sec(min(f1, it.GetEnd()))}s — use overwrite=true or range_remove first.")


def _clear_range(tl, ttype: str, tidx: int, f0: int, f1: int, ignore: set[str]) -> None:
    if tidx > tl.GetTrackCount(ttype):
        return
    tr = [track_label(ttype, tidx)]
    for f in (f1, f0):
        for it in _items_at(tl, f, tr):
            if it.GetUniqueId() not in ignore:
                _razor(tl, f, tr)
                break
    kill = [it for it in track_items(tl, ttype, tidx)
            if it.GetUniqueId() not in ignore and it.GetStart() >= f0 and it.GetEnd() <= f1]
    if kill:
        tl.DeleteClips(kill, False)


@tool()
def clip_trim(clip: Any, start: Any = None, end: Any = None, head: Any = None, tail: Any = None,
              linked: bool = True, ripple: bool = False) -> dict:
    """Trim/extend a clip. Either give new timeline edges `start`/`end` (seconds/'120f'/TC), or `head`/`tail`
    deltas in seconds (positive = shorten, negative = extend into available source media).
    linked=true applies the same trim to linked audio/video. ripple=true closes the resulting gap after the clip."""
    tl = timeline()
    T = TL(tl)
    it = find_item(tl, clip)
    s0, e0 = int(it.GetStart()), int(it.GetEnd())
    ns = T.frame(start) if start is not None else s0 + (int(round(T.delta(head))) if head is not None else 0)
    ne = T.frame(end) if end is not None else e0 - (int(round(T.delta(tail))) if tail is not None else 0)
    if ne <= ns:
        fail("Trim would make the clip empty")
    group = _group(tl, [it])[0] if linked else [it]
    ids = {g.GetUniqueId() for g in group}
    plans = []
    for g in group:
        s = _Snap(tl, g, T)
        a = s.start + (ns - s0)
        b = s.end + (ne - e0)
        if ns < s0:
            _check_free(tl, s.tt, s.ti, a, s.start, ids)
        if ne > e0:
            _check_free(tl, s.tt, s.ti, s.end, b, ids)
        plans.append((s, [{"rec": a, "from": a, "to": b}]))
    res = _rebuild(tl, plans)
    new = res[0][0]
    out = {"clip": describe_item(tl, new, T)}
    if ripple and ne < e0:
        out["ripple"] = gaps_close_impl(tl, [track_label(p[0].tt, p[0].ti) for p in plans], ne, e0)
    return out


@tool()
def clip_move(clip: Any, to: Any = None, track: str | None = None, by: Any = None, linked: bool = True,
              overwrite: bool = False) -> dict:
    """Move a clip to a new time (`to`, or relative `by` seconds) and/or another track ('V3'; linked audio moves by
    the same track offset). The destination must be empty unless overwrite=true (then whatever is there is cut out)."""
    tl = timeline()
    T = TL(tl)
    it = find_item(tl, clip)
    s0 = int(it.GetStart())
    dst = T.frame(to) if to is not None else s0 + (int(round(T.delta(by))) if by is not None else 0)
    delta = dst - s0
    tt0, ti0 = it.GetTrackTypeAndIndex()
    dtrack = 0
    if track:
        ttn, tin = parse_track(track)
        if ttn != tt0:
            fail("A clip can only move to a track of the same type")
        dtrack = tin - int(ti0)
    group = _group(tl, [it])[0] if linked else [it]
    ids = {g.GetUniqueId() for g in group}
    plans = []
    for g in group:
        s = _Snap(tl, g, T)
        nt = s.ti + dtrack if (s.tt == tt0) else s.ti
        if nt < 1:
            fail("Target track index < 1")
        ensure_track(tl, s.tt, nt)
        a, b = s.start + delta, s.end + delta
        if overwrite:
            _clear_range(tl, s.tt, nt, a, b, ids)
        else:
            _check_free(tl, s.tt, nt, a, b, ids)
        plans.append((s, [{"rec": a, "from": s.start, "to": s.end, "track": nt}]))
    res = _rebuild(tl, plans)
    return {"moved": [describe_item(tl, r[0], T) for r in res if r and r[0]]}


def gaps_close_impl(tl, tracks: list[str], f0: int, f1: int) -> dict:
    """Close the gap [f0,f1) by ripple-deleting a temporary filler placed in it."""
    filler_src = None
    for _, f in _walk_media():
        if _media_has(f, "video") and _media_len(f) > 0 and "Timeline" not in (f.GetClipProperty("Type") or ""):
            filler_src = f
            break
    if not filler_src:
        fail("Need at least one video clip in the media pool to close gaps")
    tt, ti = parse_track(tracks[0]) if tracks else ("video", 1)
    for t in tracks:
        a, b = parse_track(t)
        for it in track_items(tl, a, b):
            if it.GetStart() < f1 and it.GetEnd() > f0:
                fail(f"Range is not empty on {t}")
    T = TL(tl)
    ratio = (clip_fps(filler_src) or T.fps) / T.fps
    pieces = []
    cur = f0
    while cur < f1:
        n = min(f1 - cur, int(_media_len(filler_src) / ratio) - 1)
        if n <= 0:
            break
        p = _append(filler_src, tt, ti, cur, 0, n * ratio)
        pieces.append(p)
        cur = int(p.GetEnd())
    if not pieces:
        fail("Could not place filler")
    ok = tl.DeleteClips(pieces, True)
    return {"closed": round((f1 - f0) / T.fps, 3), "ok": bool(ok)}


def _walk_media():
    from .core import walk_folder
    for _, folder in walk_folder(media_pool().GetRootFolder()):
        for c in folder.GetClipList() or []:
            yield folder, c


@tool()
def gaps_close(track: str = "V1", start: Any = None, end: Any = None) -> dict:
    """Close an empty gap on a track (ripple — later clips shift left; how other tracks react follows Resolve's
    ripple-delete rules). Give the gap with start/end, or omit them to close the FIRST gap found on the track."""
    tl = timeline()
    T = TL(tl)
    tt, ti = parse_track(track)
    if start is not None and end is not None:
        f0, f1 = T.frame(start), T.frame(end)
    else:
        items = sorted(track_items(tl, tt, ti), key=lambda i: i.GetStart())
        cur = T.start
        f0 = f1 = None
        for i in items:
            if i.GetStart() > cur:
                f0, f1 = cur, int(i.GetStart())
                break
            cur = max(cur, int(i.GetEnd()))
        if f0 is None:
            return {"closed": 0, "note": "no gap found"}
    return gaps_close_impl(tl, [track], f0, f1)


@tool()
def range_remove(start: Any, end: Any, tracks: list[str] | None = None, ripple: bool = True) -> dict:
    """Remove everything between start and end on the given tracks (default: all unlocked video+audio tracks):
    clips are cut at both edges and the middle deleted. ripple=true closes the gap (like a ripple delete)."""
    tl = timeline()
    T = TL(tl)
    f0, f1 = T.frame(start), T.frame(end)
    if f1 <= f0:
        fail("end must be after start")
    trs = tracks or [track_label(tt, ti) for tt, ti in all_tracks(tl, ("video", "audio"))
                     if not safe(lambda: tl.GetIsTrackLocked(tt, ti), False)]
    for f in (f1, f0):
        _razor(tl, f, trs)
    kill = []
    for t in trs:
        tt, ti = parse_track(t)
        for it in track_items(tl, tt, ti):
            if it.GetStart() >= f0 and it.GetEnd() <= f1:
                kill.append(it)
    if kill:
        if not tl.DeleteClips(kill, ripple):
            fail("Delete failed")
    elif ripple:
        return {"removed": 0, "ripple": gaps_close_impl(tl, trs, f0, f1)}
    return {"removed": len(kill), "range": [T.sec(f0), T.sec(f1)], "ripple": ripple}


# ======================================================================================== clip attributes
COMPOSITE_ALIASES = {"difference": "DIFF", "dodge": "COLOR_DODGE", "burn": "COLOR_BURN", "luminosity": "LUM"}
FRIENDLY = {
    "x": "Pan", "y": "Tilt", "pan": "Pan", "tilt": "Tilt", "rotation": "RotationAngle", "opacity": "Opacity",
    "anchor_x": "AnchorPointX", "anchor_y": "AnchorPointY", "pitch": "Pitch", "yaw": "Yaw", "flip_x": "FlipX",
    "flip_y": "FlipY", "crop_left": "CropLeft", "crop_right": "CropRight", "crop_top": "CropTop",
    "crop_bottom": "CropBottom", "crop_softness": "CropSoftness", "crop_retain": "CropRetain",
    "distortion": "Distortion", "dynamic_zoom": "DynamicZoomEnabled", "volume": "AudioVolume",
    "audio_pan": "AudioPan", "pitch_semitones": "AudioPitchSemiTones", "pitch_cents": "AudioPitchCents",
    "voice_isolation": "AudioVoiceIsolationEnabled", "voice_isolation_amount": "AudioVoiceIsolationAmount",
    "dialogue_leveler": "AudioDialogueLevelerEnabled",
}
ENUMS = {"composite": ("CompositeMode", "COMPOSITE_"), "retime": ("RetimeProcess", "RETIME_"),
         "motion_estimation": ("MotionEstimation", "MOTION_EST_"), "scaling": ("Scaling", "SCALE_"),
         "resize_filter": ("ResizeFilter", "RESIZE_FILTER_"), "dynamic_zoom_ease": ("DynamicZoomEase", "DYNAMIC_ZOOM_EASE_"),
         "dialogue_leveler_mode": ("AudioDialogueLevelerMode", "DIALOGUE_LEVELER_MODE_")}


def _props(set_: dict) -> dict:
    out: dict[str, Any] = {}
    for k, v in set_.items():
        lk = k.lower()
        if lk == "zoom":
            out["ZoomX"] = out["ZoomY"] = float(v)
        elif lk in ENUMS:
            key, prefix = ENUMS[lk]
            out[key] = pick_const(prefix, v, COMPOSITE_ALIASES if lk == "composite" else None)
            if lk == "composite":
                out["CompositeEnabled"] = True
        elif lk in FRIENDLY:
            out[FRIENDLY[lk]] = v
            if lk == "volume":
                out["AudioVolumeEnabled"] = True
            if lk == "audio_pan":
                out["AudioPanEnabled"] = True
            if lk.startswith("pitch_"):
                out["AudioPitchEnabled"] = True
        else:
            out[k] = v
    return out


@tool()
def clip_properties(clips: Any, set: dict | None = None) -> dict:
    """Get or set Inspector properties of timeline clips. Friendly keys: x, y (pan/tilt px), zoom (1.0=100%),
    rotation, opacity (0-100), anchor_x/y, flip_x/y, crop_left/right/top/bottom (px), crop_softness, composite
    (normal|screen|multiply|overlay|add|soft_light|…), dynamic_zoom (bool) + dynamic_zoom_ease (linear|in|out|in_and_out),
    retime (nearest|frame_blend|optical_flow), motion_estimation, scaling (crop|fit|fill|stretch), resize_filter,
    volume (dB −100..30), audio_pan (−100..100), pitch_semitones, voice_isolation (bool) + voice_isolation_amount,
    dialogue_leveler … Raw Resolve keys (ZoomX, Pan, …) also work. Values are static (no keyframes via API)."""
    tl = timeline()
    items = find_items(tl, clips)
    out = []
    for it in items:
        r: dict[str, Any] = {"ref": describe_item(tl, it)["ref"], "name": it.GetName()}
        if set:
            p = _props(set)
            ok = it.SetProperties(p)
            if not ok:
                bad = []
                for k, v in p.items():
                    if not it.SetProperties({k: v}):
                        bad.append(k)
                r["failed"] = bad or None
            r["ok"] = True
        r["properties"] = safe(it.GetProperties, {})
        out.append(r)
    return {"clips": out}


@tool()
def clip_speed(clips: Any, percent: float, ripple: bool = False, pitch_correction: bool | None = None,
               stretch_keyframes: bool = False) -> dict:
    """Constant speed change (100 = normal, 50 = half speed slow-mo, 200 = 2x, 0 = freeze frame).
    ripple=true lets the timeline ripple to the new duration. Use clip_properties retime=optical_flow for smooth slow-mo."""
    tl = timeline()
    T = TL(tl)
    out = []
    for it in find_items(tl, clips):
        o: dict[str, Any] = {"Percentage": float(percent), "RippleTimeline": ripple, "StretchKeyframesToFit": stretch_keyframes}
        if pitch_correction is not None:
            o["PitchCorrection"] = pitch_correction
        ok = it.SetSpeed(o)
        out.append({"ok": bool(ok), **describe_item(tl, it, T), "speed": safe(it.GetSpeed)})
    return {"clips": out}


@tool()
def clip_fade(clips: Any, fade_in: float | None = None, fade_out: float | None = None) -> dict:
    """Set video/audio fade-in/out handles on clips (seconds; 0 removes)."""
    tl = timeline()
    T = TL(tl)
    out = []
    for it in find_items(tl, clips):
        cur = safe(it.GetFades, {}) or {}
        f = {"FadeIn": int(round(fade_in * T.fps)) if fade_in is not None else int(cur.get("FadeIn", 0)),
             "FadeOut": int(round(fade_out * T.fps)) if fade_out is not None else int(cur.get("FadeOut", 0))}
        out.append({"name": it.GetName(), "ok": bool(it.SetFades(f)), "fades": safe(it.GetFades)})
    return {"clips": out}


@tool()
def clip_label(clips: Any, name: str | None = None, color: str | None = None, clear_color: bool = False,
               add_flags: list[str] | None = None, clear_flags: str | None = None, enabled: bool | None = None) -> dict:
    """Rename timeline clips, set/clear clip color, add/clear flags, enable/disable clips."""
    tl = timeline()
    out = []
    for it in find_items(tl, clips):
        r: dict[str, Any] = {"id": it.GetUniqueId()}
        if name:
            r["name"] = bool(it.SetName(name))
        if color:
            r["color"] = bool(it.SetClipColor(color))
        if clear_color:
            r["clear_color"] = bool(it.ClearClipColor())
        for f in add_flags or []:
            it.AddFlag(f)
        if clear_flags:
            it.ClearFlags(clear_flags)
        if enabled is not None:
            r["enabled"] = bool(it.SetClipEnabled(enabled))
        out.append(r)
    return {"clips": out}


@tool()
def clips_link(clips: Any, linked: bool = True) -> dict:
    """Link or unlink timeline clips (e.g. to move video without its audio)."""
    tl = timeline()
    items = find_items(tl, clips)
    return {"ok": bool(tl.SetClipsLinked(items, linked)), "count": len(items)}


@tool()
def transition_add(clips: Any, type: str = "Cross Dissolve", position: str = "end", duration: float = 1.0,
                   alignment: str = "center", category: str = "simple") -> dict:
    """Add a transition to clip edges. position: start | end | both; alignment: center | left | right (left = ends at
    the cut, right = starts at the cut); duration in seconds. category: simple | fusion | ofx | audio.
    Common types: 'Cross Dissolve', 'Dip To Color Dissolve', 'Smooth Cut', 'Additive Dissolve', 'Blur Dissolve',
    'Push', 'Slide', 'Wipe', 'Iris', 'Zoom' ; audio: 'Cross Fade +3 dB', 'Cross Fade 0 dB', 'Cross Fade -3 dB'.
    The neighbouring clip needs enough handle media for centered transitions."""
    tl = timeline()
    T = TL(tl)
    out = []
    positions = ["start", "end"] if position == "both" else [position]
    for it in find_items(tl, clips):
        for pos in positions:
            opts = {"type": type, "category": category, "position": pos, "alignment": alignment,
                    "duration": max(1, int(round(duration * T.fps)))}
            tr = it.AddTransition(opts)
            out.append({"clip": it.GetName(), "position": pos, "ok": bool(tr),
                        **({"start": T.sec(tr.GetStart()), "end": T.sec(tr.GetEnd())} if tr else {})})
    return {"transitions": out}


@tool()
def compound_create(clips: Any, name: str | None = None, kind: str = "compound", start_timecode: str | None = None) -> dict:
    """Nest timeline clips into a compound clip (kind='compound') or a Fusion clip (kind='fusion')."""
    tl = timeline()
    T = TL(tl)
    items = find_items(tl, clips)
    if kind == "fusion":
        res = tl.CreateFusionClip(items)
    else:
        info = {}
        if name:
            info["name"] = name
        if start_timecode:
            info["startTimecode"] = start_timecode
        res = tl.CreateCompoundClip(items, info)
    if not res:
        fail("Creation failed")
    return {"created": describe_item(tl, res, T)}


# ======================================================================================== titles & generators
def _insert_at_end(kind: str, name: str):
    """Insert a title/generator at the very end of V1 (no ripple). Returns the new item."""
    tl = timeline()
    T = TL(tl)
    if safe(lambda: tl.GetIsTrackLocked("video", 1), False):
        fail("V1 is locked; titles/generators are inserted through V1. Unlock it.")
    keep = tl.GetCurrentTimecode()
    end = int(tl.GetEndFrame())
    tl.SetCurrentTimecode(T.tc(end))
    fn = {"fusion_title": tl.InsertFusionTitleIntoTimeline, "title": tl.InsertTitleIntoTimeline,
          "generator": tl.InsertGeneratorIntoTimeline, "fusion_generator": tl.InsertFusionGeneratorIntoTimeline,
          "ofx_generator": tl.InsertOFXGeneratorIntoTimeline,
          "fusion_composition": lambda _n: tl.InsertFusionCompositionIntoTimeline()}[kind]
    it = fn(name)
    try:
        tl.SetCurrentTimecode(keep)
    except Exception:  # noqa: BLE001
        pass
    if not it:
        fail(f"Could not insert {kind} '{name}'. Check the exact name in the Effects library (e.g. 'Text+', "
             "'Solid Color', 'Text', 'Scroll'); the current page must be edit/cut.")
    return it


def _text_inputs(text: str | None, font: str | None, style: str | None, size: float | None, color: Any,
                 position: Any, inputs: dict | None) -> dict:
    d: dict[str, Any] = {}
    if text is not None:
        d["StyledText"] = text
        if not font and re.search(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]", text):
            # the default Text+ font has no Persian/Arabic glyphs
            font = os.environ.get("RESOLVE_MCP_RTL_FONT") or ("Geeza Pro" if sys.platform == "darwin" else "Tahoma")
            style = style or "Regular"
    if font:
        d["Font"] = font
    if style:
        d["Style"] = style
    if size is not None:
        d["Size"] = float(size)
    if color is not None:
        rgb = _rgb(color)
        d.update({"Red1": rgb[0], "Green1": rgb[1], "Blue1": rgb[2]})
    if position is not None:
        x, y = (position["x"], position["y"]) if isinstance(position, dict) else position
        d["Center"] = {1: float(x), 2: float(y)}
    d.update(inputs or {})
    return d


def _rgb(c: Any) -> tuple[float, float, float]:
    if isinstance(c, str):
        s = c.lstrip("#")
        return tuple(int(s[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]
    vals = [float(v) for v in (c.values() if isinstance(c, dict) else c)][:3]
    if max(vals) > 1:
        vals = [v / 255 for v in vals]
    return tuple(vals)  # type: ignore[return-value]


def _apply_text(it, inp: dict) -> list[str]:
    bad = []
    for k in range(1, (safe(it.GetFusionCompCount, 0) or 0) + 1):
        comp = it.GetFusionCompByIndex(k)
        tools = comp.GetToolList(False, "TextPlus") or {}
        if not tools:
            tools = {i: t for i, t in (comp.GetToolList(False) or {}).items() if "Text" in t.GetAttrs().get("TOOLS_RegID", "")}
        for t in tools.values():
            for key, v in inp.items():
                t.SetInput(key, v)
        if tools:
            return bad
    # compound: dive into inner timeline
    mpi = safe(it.GetMediaPoolItem)
    inner = safe(mpi.GetTimeline) if mpi else None
    if inner:
        for tt, ti in all_tracks(inner, ("video",)):
            for x in track_items(inner, tt, ti):
                _apply_text(x, inp)
        return bad
    fail("This clip has no Text+ tool")
    return bad


def _place_inserted(items: list, track: str, at_frame: int, duration_f: int, name: str):
    """Wrap inserted end-of-timeline items in a compound clip and place it at track/time."""
    tl = timeline()
    T = TL(tl)
    tt, ti = parse_track(track)
    if tt != "video":
        fail("Titles/generators go on video tracks")
    if ti == 1 and at_frame == int(items[0].GetStart()) and duration_f == int(items[-1].GetEnd() - items[0].GetStart()):
        return items[0]
    mp = media_pool()
    keep_bin = mp.GetCurrentFolder()
    mp.SetCurrentFolder(find_folder("Titles (MCP)", create=True))
    comp = tl.CreateCompoundClip(items, {"name": name})
    mp.SetCurrentFolder(keep_bin)
    if not comp:
        fail("CreateCompoundClip failed")
    mpi = comp.GetMediaPoolItem()
    tl.DeleteClips([comp], False)
    ensure_track(tl, "video", ti)
    _check_free(tl, "video", ti, at_frame, at_frame + duration_f, set())
    return _append(mpi, "video", ti, at_frame, 0, duration_f)


@tool()
def title_add(text: str, at: Any = None, duration: float = 3.0, track: str = "V2", template: str = "Text+",
              font: str | None = None, style: str | None = None, size: float | None = None, color: Any = None,
              position: Any = None, inputs: dict | None = None, name: str | None = None) -> dict:
    """Add a title at `at` (default: playhead) for `duration` seconds on `track`. template: a Fusion title name
    ('Text+', 'Background Reveal', 'Call Out', …) or prefix 'edit:' for Edit-page titles ('edit:Text', 'edit:Scroll').
    Style: font ('Vazirmatn', 'Arial'), style ('Bold'), size (Text+ size ~0.05-0.2), color ('#ffcc00' or [r,g,b]),
    position [x,y] (0-1, center = [0.5,0.5]), plus raw Text+ `inputs`. Persian/Arabic text works in Text+.
    Placed titles not on V1/end are wrapped as compound clips (bin 'Titles (MCP)'); title_update still edits them."""
    tl = timeline()
    T = TL(tl)
    if at is None:
        from .core import tc_to_frames
        at_f = tc_to_frames(tl.GetCurrentTimecode(), T.fps, T.drop)
    else:
        at_f = T.frame(at)
    dur_f = max(1, int(round(T.delta(duration))))
    kind, nm = ("title", template[5:]) if template.startswith("edit:") else ("fusion_title", template)
    inp = _text_inputs(text, font, style, size, color, position, inputs)
    pieces = []
    first = _insert_at_end(kind, nm)
    pieces.append(first)
    per = int(first.GetEnd() - first.GetStart())
    while per * len(pieces) < dur_f:
        pieces.append(_insert_at_end(kind, nm))
    for p in pieces:
        if kind == "fusion_title":
            _apply_text(p, inp)
    if name:
        for p in pieces:
            p.SetName(name)
    it = _place_inserted(pieces, track, at_f, dur_f, name or f"Title - {text[:24]}")
    params = {"text": text, "template": template, "font": font, "style": style, "size": size, "color": color,
              "position": position, "inputs": inputs, "name": name}
    mpi = safe(it.GetMediaPoolItem)
    if mpi:
        import json
        safe(lambda: mpi.SetThirdPartyMetadata({"mcp_title": json.dumps(params, ensure_ascii=False)}))
    return {"title": describe_item(tl, it, T), "segments_used": len(pieces)}


@tool()
def title_update(clip: Any, text: str | None = None, font: str | None = None, style: str | None = None,
                 size: float | None = None, color: Any = None, position: Any = None, inputs: dict | None = None) -> dict:
    """Change text/style of an existing Text+ title (also inside MCP compound titles). See title_add for fields."""
    tl = timeline()
    T = TL(tl)
    it = find_item(tl, clip)
    inp = _text_inputs(text, font, style, size, color, position, inputs)
    if not inp:
        fail("Nothing to change")
    if (safe(it.GetFusionCompCount, 0) or 0) > 0:
        _apply_text(it, inp)
        return {"updated": it.GetName(), "inputs": list(inp)}
    # MCP compound title: rebuild it from its stored parameters
    import json
    mpi = safe(it.GetMediaPoolItem)
    raw = safe(lambda: mpi.GetThirdPartyMetadata("mcp_title")) if mpi else None
    if not raw:
        fail("This clip is not an editable Text+ title")
    params = json.loads(raw)
    for k, v in {"text": text, "font": font, "style": style, "size": size, "color": color, "position": position}.items():
        if v is not None:
            params[k] = v
    if inputs:
        params["inputs"] = {**(params.get("inputs") or {}), **inputs}
    tt, ti = it.GetTrackTypeAndIndex()
    start, end = it.GetStart(), it.GetEnd()
    tl.DeleteClips([it], False)
    res = title_add(at=f"{T.rel(start)}f", duration=f"{int(end - start)}f", track=track_label(tt, int(ti)), **params)
    return {"updated": True, "rebuilt": True, **res}


@tool()
def generator_add(name: str = "Solid Color", at: Any = None, duration: float = 5.0, track: str = "V1",
                  kind: str = "standard", label: str | None = None) -> dict:
    """Add a generator (kind: standard → 'Solid Color', '10 Step', 'Grey Scale', 'SMPTE Color Bar', 'Four Color Gradient',
    'Window'; fusion → Fusion generators e.g. 'Contours', 'Noise Gradient'; ofx → OFX generator names;
    composition → empty Fusion composition to build with fusion_* tools) at time/track for duration seconds."""
    tl = timeline()
    T = TL(tl)
    if at is None:
        from .core import tc_to_frames
        at_f = tc_to_frames(tl.GetCurrentTimecode(), T.fps, T.drop)
    else:
        at_f = T.frame(at)
    dur_f = max(1, int(round(T.delta(duration))))
    k = {"standard": "generator", "fusion": "fusion_generator", "ofx": "ofx_generator", "composition": "fusion_composition"}[kind]
    pieces = [_insert_at_end(k, name)]
    per = int(pieces[0].GetEnd() - pieces[0].GetStart())
    while per * len(pieces) < dur_f:
        pieces.append(_insert_at_end(k, name))
    it = _place_inserted(pieces, track, at_f, dur_f, label or name)
    return {"generator": describe_item(tl, it, T)}


# ======================================================================================== markers
@tool()
def markers(action: str = "list", target: Any = "timeline", time: Any = None, color: str = "Blue",
            name: str = "", note: str = "", duration: float = 0, custom_data: str | None = None) -> dict:
    """Markers on the timeline, a timeline clip, or a media pool clip. target: 'timeline' | clip ref ('V1:0', id) |
    'media:<media ref>'. action: list | add | delete (at `time`, or all of `color` with time omitted; color 'All' = all) |
    delete_custom (by custom_data). For clips, `time` is seconds from the clip's start. duration in seconds."""
    tl = timeline(required=False)
    T = TL(tl) if tl else None
    if target == "timeline":
        obj, base, fps = tl, None, T.fps
    elif isinstance(target, str) and target.startswith("media:"):
        obj = find_mpitem(target[6:])
        base, fps = None, clip_fps(obj) or (T.fps if T else 24)
    else:
        obj = find_item(tl, target)
        base, fps = obj, T.fps

    def to_f(t: Any) -> int:
        if target == "timeline":
            return T.rel(T.frame(t))
        if isinstance(t, str) and t.endswith("f"):
            return int(float(t[:-1]))
        return int(round(float(t) * fps))

    a = action.lower()
    if a == "add":
        if time is None:
            fail("time is required")
        f = to_f(time)
        ok = obj.AddMarker(f, color, name, note, max(1, int(round(duration * fps))), custom_data) if custom_data else \
            obj.AddMarker(f, color, name, note, max(1, int(round(duration * fps))))
        if not ok:
            fail("AddMarker failed (a marker may already exist at that frame)")
    elif a == "delete":
        if time is not None:
            ok = obj.DeleteMarkerAtFrame(to_f(time))
        else:
            ok = obj.DeleteMarkersByColor(color or "All")
        if not ok:
            fail("Nothing deleted")
    elif a == "delete_custom":
        obj.DeleteMarkerByCustomData(custom_data)
    mk = safe(obj.GetMarkers, {}) or {}
    return {"markers": [{"time": round(float(k) / fps, 3), "frame": int(k), **v} for k, v in sorted(mk.items())]}
