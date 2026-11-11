"""Color page tools: CDL, LUTs, generated looks, nodes, versions, groups, stills, AI tools, DCTL."""

from __future__ import annotations

import os
import re
import tempfile
from typing import Any

from .core import (
    LUT_DIR, TL, WORK_DIR, describe_item, fail, find_item, find_items, pick_const, project, resolve, safe, timeline,
    tool,
)

MCP_LUT_DIR = os.path.join(LUT_DIR, "ClaudeMCP")
LUT_EXT = (".cube", ".3dl", ".dat", ".lut", ".olut", ".ilut", ".mga", ".m3d", ".xml", ".vlt", ".dctl")


def _graph(target: Any, layer: int | None = None):
    """target: clip ref | 'timeline' | 'group:<name>:pre' | 'group:<name>:post'."""
    if target == "timeline":
        return timeline().GetNodeGraph()
    if isinstance(target, str) and target.startswith("group:"):
        _, name, *rest = target.split(":")
        g = _find_group(name)
        return g.GetPostClipNodeGraph() if rest and rest[0] == "post" else g.GetPreClipNodeGraph()
    it = find_item(timeline(), target)
    g = it.GetNodeGraph(layer) if layer else it.GetNodeGraph()
    if not g:
        fail("Could not get node graph (is this a video clip?)")
    return g


def _find_group(name: str):
    for g in project().GetColorGroupsList() or []:
        if g.GetName() == name:
            return g
    fail(f"Color group '{name}' not found")


def _video_items(clips: Any) -> list:
    tl = timeline()
    items = [i for i in find_items(tl, clips) if safe(lambda: i.GetTrackTypeAndIndex()[0]) == "video"]
    if not items:
        fail("No video clips matched")
    return items


@tool()
def grade_cdl(clips: Any, node: int = 1, slope: Any = None, offset: Any = None, power: Any = None,
              saturation: float | None = None) -> dict:
    """Apply an ASC CDL to a node of timeline clips (cumulative primary: slope≈gain, offset≈lift/offset,
    power≈gamma inverse). Values: number (all channels) or [r,g,b]. Defaults: slope 1, offset 0, power 1, sat 1."""
    def trip(v: Any, d: float) -> str:
        if v is None:
            v = d
        if isinstance(v, (int, float)):
            v = [v, v, v]
        return " ".join(f"{float(x):.5f}" for x in v)

    cdl = {"NodeIndex": node, "Slope": trip(slope, 1), "Offset": trip(offset, 0), "Power": trip(power, 1),
           "Saturation": float(saturation if saturation is not None else 1)}
    out = []
    for it in _video_items(clips):
        g = it.GetNodeGraph()
        if g and g.GetNumNodes() < node:
            fail(f"Clip has only {g.GetNumNodes()} node(s); add nodes in the Color page or use node 1")
        out.append({"clip": it.GetName(), "ok": bool(it.SetCDL(cdl))})
    return {"cdl": cdl, "clips": out}


def _lut_roots() -> list[str]:
    roots = [LUT_DIR]
    user = os.path.expanduser("~/Library/Application Support/Blackmagic Design/DaVinci Resolve/LUT")  # macOS per-user
    if os.path.isdir(user):
        roots.append(user)
    return roots


@tool(read_only=True)
def lut_list(search: str | None = None, limit: int = 200) -> dict:
    """List LUT files Resolve knows (relative paths usable in grade_lut). `search` filters by substring."""
    out = []
    for root in _lut_roots():
        for dp, _, fns in os.walk(root):
            for f in sorted(fns):
                if f.lower().endswith(LUT_EXT):
                    rel = os.path.relpath(os.path.join(dp, f), root)
                    if search and search.lower() not in rel.lower():
                        continue
                    out.append(rel)
    return {"root": LUT_DIR, "luts": out[:limit], "total": len(out)}


@tool()
def grade_lut(clips: Any, lut: str, node: int = 1, layer: int | None = None) -> dict:
    """Apply a LUT to a node (path relative to the LUT folder, e.g. 'Film Looks/Rec709 Kodak 2383 D65.cube', or an
    absolute path inside a LUT folder). Use lut='' to remove. Targets: clip refs, 'timeline', 'group:<name>:pre|post'."""
    p = project()
    p.RefreshLUTList()
    targets = [clips] if clips == "timeline" or (isinstance(clips, str) and clips.startswith("group:")) else _video_items(clips)
    out = []
    for t in targets:
        g = _graph(t, layer) if isinstance(t, str) else (t.GetNodeGraph(layer) if layer else t.GetNodeGraph())
        ok = g.SetLUT(node, lut)
        out.append({"target": t if isinstance(t, str) else t.GetName(), "ok": bool(ok), "lut": g.GetLUT(node)})
    if not any(o["ok"] for o in out):
        fail(f"SetLUT failed. Is '{lut}' a valid path under {LUT_DIR}? (see lut_list)")
    return {"results": out}


def _cube(path: str, fn, size: int = 33, title: str = "Claude MCP look") -> None:
    with open(path, "w") as f:
        f.write(f'TITLE "{title}"\nLUT_3D_SIZE {size}\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 1\n')
        n = size - 1
        for b in range(size):
            for g in range(size):
                for r in range(size):
                    o = fn(r / n, g / n, b / n)
                    f.write("%.6f %.6f %.6f\n" % tuple(min(1.0, max(0.0, x)) for x in o))


def _v3(v: Any, d: float) -> list[float]:
    if v is None:
        return [d, d, d]
    if isinstance(v, (int, float)):
        return [float(v)] * 3
    return [float(x) for x in v]


@tool()
def grade_look(clips: Any, name: str | None = None, exposure: float = 0, contrast: float = 1, pivot: float = 0.435,
               saturation: float = 1, temperature: float = 0, tint: float = 0, lift: Any = None, gamma: Any = None,
               gain: Any = None, offset: Any = None, shadows: float = 0, highlights: float = 0,
               vibrance: float = 0, fade: float = 0, tint_shadows: Any = None, tint_highlights: Any = None,
               node: int = 1, size: int = 33, apply: bool = True) -> dict:
    """Build a creative grade as a 3D LUT and apply it to a node of clips (or 'timeline'). Works on display-referred
    (Rec.709) images. Parameters (neutral defaults):
    exposure (stops), contrast (1 = none; 1.2 = +20%) around pivot, saturation (1), vibrance (0..1),
    temperature (−1 cool … +1 warm), tint (−1 green … +1 magenta), lift/gamma/gain/offset (number or [r,g,b];
    lift/offset default 0, gamma/gain 1), shadows/highlights (−1..1 tone lift/recover), fade (0..0.3 milky blacks),
    tint_shadows / tint_highlights ([r,g,b] colors like [0.0,0.05,0.1] for teal shadows) — e.g. teal & orange:
    tint_shadows=[0,0.04,0.08], tint_highlights=[0.08,0.04,0]. Returns the LUT path (reusable with grade_lut)."""
    L, Gm, Gn, O = _v3(lift, 0), _v3(gamma, 1), _v3(gain, 1), _v3(offset, 0)
    ts, th = _v3(tint_shadows, 0), _v3(tint_highlights, 0)
    expo = 2 ** exposure
    wb = [1 + 0.12 * temperature + 0.04 * tint, 1 - 0.06 * tint, 1 - 0.12 * temperature + 0.04 * tint]

    def fn(r: float, g: float, b: float):
        c = [r, g, b]
        c = [c[i] * expo * wb[i] for i in range(3)]
        c = [Gn[i] * (c[i] + L[i] * (1 - c[i])) + O[i] for i in range(3)]
        c = [max(0.0, c[i]) ** (1 / Gm[i]) if Gm[i] > 0 else c[i] for i in range(3)]
        if shadows or highlights:
            out = []
            for v in c:
                v = v + shadows * 0.5 * v * (1 - v) ** 2 * 2
                v = v + highlights * 0.5 * v ** 2 * (1 - v) * 2
                out.append(v)
            c = out
        c = [(v - pivot) * contrast + pivot for v in c]
        y = 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
        sat = saturation
        if vibrance:
            mx, mn = max(c), min(c)
            cur = (mx - mn)
            sat *= 1 + vibrance * (1 - min(1.0, cur * 2))
        c = [y + (v - y) * sat for v in c]
        if any(ts) or any(th):
            ws = max(0.0, 1 - y * 2) ** 1.5
            wh = max(0.0, y * 2 - 1) ** 1.5
            c = [c[i] + ts[i] * ws + th[i] * wh for i in range(3)]
        if fade:
            c = [fade + v * (1 - fade) for v in c]
        return c

    os.makedirs(MCP_LUT_DIR, exist_ok=True)
    import hashlib
    key = repr((exposure, contrast, pivot, saturation, temperature, tint, L, Gm, Gn, O, shadows, highlights, vibrance, fade, ts, th))
    nm = re.sub(r"[^\w\-. ]", "_", name or "look_" + hashlib.md5(key.encode()).hexdigest()[:8])
    path = os.path.join(MCP_LUT_DIR, nm + ".cube")
    _cube(path, fn, size, nm)
    res: dict[str, Any] = {"lut": os.path.relpath(path, LUT_DIR), "path": path}
    if apply:
        res.update(grade_lut(clips, os.path.relpath(path, LUT_DIR), node))
    return res


@tool()
def nodes(clip: Any, node: int | None = None, enabled: bool | None = None, cache: str | None = None,
          layer: int | None = None, reset_node_colors: bool = False) -> dict:
    """Inspect a clip's node graph (count, labels, tools used, LUTs, cache mode) or change one node: enabled,
    cache (auto | enabled | disabled). Target: clip ref, 'timeline' or 'group:<name>:pre|post'."""
    g = _graph(clip, layer)
    if node is not None:
        if enabled is not None:
            g.SetNodeEnabled(node, enabled)
        if cache:
            g.SetNodeCacheMode(node, pick_const("CACHE_", {"auto": "AUTO_ENABLED"}.get(cache.lower(), cache)))
    if reset_node_colors and clip not in ("timeline",):
        find_item(timeline(), clip).ResetAllNodeColors()
    out = []
    for i in range(1, (g.GetNumNodes() or 0) + 1):
        out.append({"node": i, "label": safe(lambda: g.GetNodeLabel(i)), "tools": safe(lambda: g.GetToolsInNode(i)),
                    "lut": safe(lambda: g.GetLUT(i)) or None, "cache": safe(lambda: g.GetNodeCacheMode(i))})
    return {"nodes": out}


@tool()
def grade_reset(clips: Any) -> dict:
    """Reset all grades on the clips' node graphs (or 'timeline' / 'group:<name>:pre|post')."""
    targets = [clips] if clips == "timeline" or (isinstance(clips, str) and clips.startswith("group:")) else _video_items(clips)
    out = []
    for t in targets:
        g = _graph(t) if isinstance(t, str) else t.GetNodeGraph()
        out.append(bool(g.ResetAllGrades()))
    return {"reset": out}


@tool()
def grade_copy(source: Any, targets: Any) -> dict:
    """Copy the grade (current node stack layer) from one clip to other clips (refs, 'V1' for a whole track,
    'selected')."""
    tl = timeline()
    src = find_item(tl, source)
    dst = [i for i in find_items(tl, targets) if i.GetUniqueId() != src.GetUniqueId()]
    return {"ok": bool(src.CopyGrades(dst)), "count": len(dst)}


@tool()
def grade_drx(clips: Any, path: str, mode: str = "none") -> dict:
    """Apply a saved grade (.drx still file) to clips. mode: none (no keyframes) | source_tc | start_frames."""
    m = {"none": 0, "source_tc": 1, "start_frames": 2}[mode]
    targets = [clips] if clips == "timeline" or (isinstance(clips, str) and clips.startswith("group:")) else _video_items(clips)
    out = []
    for t in targets:
        g = _graph(t) if isinstance(t, str) else t.GetNodeGraph()
        out.append(bool(g.ApplyGradeFromDRX(path, m)))
    return {"applied": out}


@tool()
def color_versions(clip: Any, action: str = "list", name: str | None = None, new_name: str | None = None,
                   remote: bool = False) -> dict:
    """Color versions of a clip. action: list | add | load | delete | rename. remote=true uses remote versions."""
    it = find_item(timeline(), clip)
    vt = 1 if remote else 0
    a = action.lower()
    ok = True
    if a == "add":
        ok = it.AddVersion(name, vt)
    elif a == "load":
        ok = it.LoadVersionByName(name, vt)
    elif a == "delete":
        ok = it.DeleteVersionByName(name, vt)
    elif a == "rename":
        ok = it.RenameVersionByName(name, new_name, vt)
    if not ok:
        fail(f"{action} failed")
    return {"versions": it.GetVersionNameList(vt), "current": it.GetCurrentVersion()}


@tool()
def color_groups(action: str = "list", name: str | None = None, clips: Any = None, new_name: str | None = None) -> dict:
    """Color groups. action: list | create | delete | rename | assign (clips → group `name`) | remove (clips from group) |
    clips (list clips of group)."""
    p = project()
    a = action.lower()
    if a == "create":
        g = p.AddColorGroup(name)
        if not g:
            fail("Could not create group")
    elif a == "delete":
        p.DeleteColorGroup(_find_group(name))
    elif a == "rename":
        _find_group(name).SetName(new_name)
    elif a == "assign":
        g = _find_group(name)
        return {"assigned": [bool(i.AssignToColorGroup(g)) for i in _video_items(clips)]}
    elif a == "remove":
        return {"removed": [bool(i.RemoveFromColorGroup()) for i in _video_items(clips)]}
    elif a == "clips":
        tl = timeline()
        return {"group": name, "clips": [describe_item(tl, i) for i in _find_group(name).GetClipsInTimeline(tl) or []]}
    return {"groups": [g.GetName() for g in p.GetColorGroupsList() or []]}


@tool()
def stills(action: str = "list", album: str | None = None, folder: str | None = None, prefix: str = "still",
           format: str = "png", indices: list[int] | None = None, files: list[str] | None = None,
           clips: Any = None, frame_source: str = "middle", powergrade: bool = False) -> dict:
    """Gallery stills. action:
    list (albums + still labels) | grab (still of the current clip at the playhead; Color page) |
    grab_all (one still per clip; frame_source first|middle) | export (stills `indices` of `album` to `folder`;
    format png|jpg|tif|dpx|cin|tga|bmp|exr|drx) | import (files) | delete (indices) |
    apply (grade of still `indices[0]` → `clips`, via DRX) | new_album | set_album (album name or index).
    powergrade=true works with PowerGrade albums."""
    gal = project().GetGallery()
    albums = (gal.GetGalleryPowerGradeAlbums() if powergrade else gal.GetGalleryStillAlbums()) or []

    def get_album():
        if album is None:
            return gal.GetCurrentStillAlbum()
        for k, al in enumerate(albums):
            if gal.GetAlbumName(al) == album or str(k) == str(album):
                return al
        fail(f"Album '{album}' not found")

    a = action.lower()
    tl = timeline(required=False)
    if a == "new_album":
        al = gal.CreateGalleryPowerGradeAlbum() if powergrade else gal.CreateGalleryStillAlbum()
        if album:
            gal.SetAlbumName(al, album)
        return {"created": gal.GetAlbumName(al)}
    if a == "set_album":
        return {"ok": bool(gal.SetCurrentStillAlbum(get_album()))}
    if a == "grab":
        resolve().OpenPage("color")
        s = tl.GrabStill()
        return {"ok": bool(s)}
    if a == "grab_all":
        res = tl.GrabAllStills(1 if frame_source == "first" else 2)
        return {"grabbed": len(res or [])}
    al = get_album()
    st = al.GetStills() or []
    sel = [st[i] for i in indices] if indices else st
    if a == "export":
        os.makedirs(folder or str(WORK_DIR / "stills"), exist_ok=True)
        ok = al.ExportStills(sel, folder or str(WORK_DIR / "stills"), prefix, format)
        return {"ok": bool(ok), "folder": folder or str(WORK_DIR / "stills")}
    if a == "import":
        return {"ok": bool(al.ImportStills(files or []))}
    if a == "delete":
        return {"ok": bool(al.DeleteStills(sel))}
    if a == "apply":
        tmp = tempfile.mkdtemp(dir=WORK_DIR / "tmp")
        if not al.ExportStills(sel[:1], tmp, "grade", "drx"):
            fail("Could not export still grade")
        drx = next((os.path.join(tmp, f) for f in os.listdir(tmp) if f.endswith(".drx")), None)
        if not drx:
            fail("No .drx produced")
        return grade_drx(clips, drx)
    return {"albums": [gal.GetAlbumName(x) for x in albums], "current": gal.GetAlbumName(gal.GetCurrentStillAlbum()),
            "stills": [{"index": k, "label": al.GetLabel(s)} for k, s in enumerate(st)]}


@tool()
def lut_export(clip: Any, path: str, size: str = "33") -> dict:
    """Export a clip's grade as a LUT file. size: 17 | 33 | 65 | panasonic."""
    it = find_item(timeline(), clip)
    t = {"17": "EXPORT_LUT_17PTCUBE", "33": "EXPORT_LUT_33PTCUBE", "65": "EXPORT_LUT_65PTCUBE",
         "panasonic": "EXPORT_LUT_PANASONICVLUT"}[str(size)]
    ok = it.ExportLUT(getattr(resolve(), t), path)
    if not ok:
        fail("ExportLUT failed")
    return {"exported": path}


@tool(timeout=3600)
def clip_ai(clips: Any, action: str, mode: str = "BI") -> dict:
    """Studio AI/tracking ops on timeline clips. action: magic_mask (mode F|B|BI; needs a Magic Mask in the node — creates
    one on the current node), magic_mask_regenerate, stabilize, smart_reframe (uses timeline aspect, e.g. vertical),
    color_cache_on/off, update_sidecar."""
    out = []
    for it in _video_items(clips):
        a = action.lower()
        if a == "magic_mask":
            ok = it.CreateMagicMask(mode)
        elif a == "magic_mask_regenerate":
            ok = it.RegenerateMagicMask()
        elif a == "stabilize":
            ok = it.Stabilize()
        elif a == "smart_reframe":
            ok = it.SmartReframe()
        elif a in ("color_cache_on", "color_cache_off"):
            ok = it.SetColorOutputCache(a.endswith("on"))
        elif a == "update_sidecar":
            ok = it.UpdateSidecar()
        else:
            fail("action must be magic_mask | magic_mask_regenerate | stabilize | smart_reframe | color_cache_on | color_cache_off | update_sidecar")
        out.append({"clip": it.GetName(), "ok": bool(ok)})
    return {"results": out}


@tool()
def dctl(action: str, name: str | None = None, source: str | None = None, path: str | None = None) -> dict:
    """DCTL shaders. action: validate (source) | write (name + source → LUT/ClaudeMCP/<name>.dctl, validated first;
    then use grade_lut with 'ClaudeMCP/<name>.dctl') | read (name) | list | encrypt (path)."""
    r = resolve()
    a = action.lower()
    os.makedirs(MCP_LUT_DIR, exist_ok=True)
    if a == "validate":
        err = r.ValidateDCTL(source)
        return {"valid": not err, "error": err}
    if a == "write":
        err = r.ValidateDCTL(source)
        if err:
            fail(f"DCTL error: {err}")
        p = os.path.join(MCP_LUT_DIR, f"{name}.dctl")
        with open(p, "w") as f:
            f.write(source or "")
        project().RefreshLUTList()
        return {"written": p, "lut": os.path.relpath(p, LUT_DIR)}
    if a == "read":
        with open(os.path.join(MCP_LUT_DIR, f"{name}.dctl")) as f:
            return {"source": f.read()}
    if a == "list":
        return lut_list(".dctl")
    if a == "encrypt":
        return {"ok": bool(r.EncryptDCTL(path))}
    fail("action must be validate | write | read | list | encrypt")
    return {}
