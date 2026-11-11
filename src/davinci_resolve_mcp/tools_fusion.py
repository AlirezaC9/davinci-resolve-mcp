"""Fusion tools: build and animate node trees inside any clip's Fusion composition."""

from __future__ import annotations

from typing import Any

from .core import TL, all_tracks, fail, find_item, resolve, safe, timeline, tool, track_items


def _comp(clip: Any, comp: Any = None, create: bool = False):
    """Return (comp, item). clip='current' uses the comp open on the Fusion page."""
    if clip in (None, "current"):
        c = resolve().Fusion().GetCurrentComp()
        if not c:
            fail("No current Fusion comp (open the Fusion page on a clip, or pass a clip ref)")
        return c, None
    it = find_item(timeline(), clip)
    n = safe(it.GetFusionCompCount, 0) or 0
    if n == 0:
        # MCP compound titles: dive into the nested timeline
        mpi = safe(it.GetMediaPoolItem)
        inner = safe(mpi.GetTimeline) if mpi else None
        if inner:
            for tt, ti in all_tracks(inner, ("video",)):
                for x in track_items(inner, tt, ti):
                    if (safe(x.GetFusionCompCount, 0) or 0) > 0:
                        return x.GetFusionCompByIndex(1), x
        if not create:
            fail(f"'{it.GetName()}' has no Fusion composition. Use fusion_comps action=add (or create=true).")
        c = it.AddFusionComp()
        return c, it
    if comp is None:
        return it.GetFusionCompByIndex(n), it
    c = it.GetFusionCompByName(comp) if isinstance(comp, str) else it.GetFusionCompByIndex(int(comp))
    if not c:
        fail(f"Comp {comp!r} not found; comps: {it.GetFusionCompNameList()}")
    return c, it


def _tool(comp, name: str):
    t = comp.FindTool(name)
    if not t:
        tools = comp.GetToolList(False) or {}
        t = next((x for x in tools.values() if x.GetAttrs().get("TOOLS_RegID") == name), None)
    if not t:
        fail(f"Tool '{name}' not found. Tools: {[x.GetAttrs()['TOOLS_Name'] for x in (comp.GetToolList(False) or {}).values()]}")
    return t


def _main_input_id(t) -> str | None:
    inp = safe(lambda: t.FindMainInput(1))
    return inp.GetAttrs().get("INPS_ID") if inp else None


def _val(v: Any) -> Any:
    if isinstance(v, dict):
        return {str(k): _val(x) for k, x in v.items()}
    if isinstance(v, (int, float, str, bool)) or v is None:
        return v
    return str(v)


def _conv_in(v: Any) -> Any:
    """[x,y] → Fusion point table."""
    if isinstance(v, list) and 2 <= len(v) <= 4 and all(isinstance(x, (int, float)) for x in v):
        return {i + 1: float(x) for i, x in enumerate(v)}
    if isinstance(v, dict) and all(str(k) in ("x", "y", "z") for k in v):
        return {i + 1: float(v[k]) for i, k in enumerate(("x", "y", "z")) if k in v}
    return v


@tool()
def fusion_comps(clip: Any, action: str = "list", name: str | None = None, new_name: str | None = None,
                 path: str | None = None, index: int = 1) -> dict:
    """Fusion compositions of a timeline clip. action: list | add | load (name) | delete (name) | rename (name→new_name) |
    import (.comp/.setting `path`) | export (comp `index` → `path`)."""
    it = find_item(timeline(), clip)
    a = action.lower()
    if a == "add":
        it.AddFusionComp()
    elif a == "load":
        it.LoadFusionCompByName(name)
    elif a == "delete":
        it.DeleteFusionCompByName(name)
    elif a == "rename":
        it.RenameFusionCompByName(name, new_name)
    elif a == "import":
        if not it.ImportFusionComp(path):
            fail("Import failed")
    elif a == "export":
        if not it.ExportFusionComp(path, index):
            fail("Export failed")
        return {"exported": path}
    return {"clip": it.GetName(), "comps": it.GetFusionCompNameList()}


@tool(read_only=True)
def fusion_comp_info(clip: Any = "current", tool_name: str | None = None, comp: Any = None, search: str | None = None,
                     all_inputs: bool = False) -> dict:
    """Inspect a Fusion comp: tools (name, type, inputs wired from which tool). With tool_name: that tool's inputs
    (id, name, type, current value) — filter with `search`; all_inputs=true includes hidden/advanced ones.
    clip: timeline clip ref, or 'current' (comp open on the Fusion page)."""
    c, it = _comp(clip, comp)
    attrs = c.GetAttrs() or {}
    if tool_name:
        t = _tool(c, tool_name)
        out = []
        for inp in (t.GetInputList() or {}).values():
            a = inp.GetAttrs() or {}
            if not all_inputs and (a.get("INPB_Passive") or a.get("INPID_InputControl") in (None, "LabelControl")):
                if not search:
                    continue
            iid = a.get("INPS_ID")
            if search and search.lower() not in (iid or "").lower() and search.lower() not in (a.get("INPS_Name") or "").lower():
                continue
            dt = a.get("INPS_DataType")
            d = {"id": iid, "name": a.get("INPS_Name"), "type": dt}
            if dt in ("Number", "Point", "Text", "FuID", "StyledText"):
                d["value"] = _val(safe(lambda: t.GetInput(iid)))
            src = safe(inp.GetConnectedOutput)
            if src:
                d["from"] = src.GetTool().GetAttrs()["TOOLS_Name"]
            out.append(d)
        return {"tool": tool_name, "type": t.GetAttrs().get("TOOLS_RegID"), "inputs": out}
    tools = []
    for t in (c.GetToolList(False) or {}).values():
        a = t.GetAttrs()
        conns = {}
        for inp in (t.GetInputList() or {}).values():
            src = safe(inp.GetConnectedOutput)
            if src:
                conns[inp.GetAttrs()["INPS_ID"]] = src.GetTool().GetAttrs()["TOOLS_Name"]
        tools.append({"name": a.get("TOOLS_Name"), "type": a.get("TOOLS_RegID"), "inputs_from": conns or None,
                      "pass_through": a.get("TOOLB_PassThrough") or None})
    return {"comp": attrs.get("COMPS_Name"), "range": [attrs.get("COMPN_RenderStart"), attrs.get("COMPN_RenderEnd")],
            "tools": tools}


@tool()
def fusion_tool_add(tool_type: str, clip: Any = "current", name: str | None = None, insert: bool = True,
                    after: str | None = None, x: float | None = None, y: float | None = None,
                    inputs: dict | None = None, comp: Any = None, create_comp: bool = True) -> dict:
    """Add a Fusion tool (by registry ID) to a clip's comp. Common IDs: TextPlus, Transform, Merge, Background,
    Blur, ColorCorrector, BrightnessContrast, Glow, SoftGlow, FilmGrain, Rectangle, Ellipse, Polygon, BSpline,
    Tracker, PlanarTracker, CornerPositioner, DeltaKeyer, UltraKeyer, Shadow, DirectionalBlur, LensDistort,
    Vignette (via ofx), Text3D, Shape3D, Renderer3D, pRender/pEmitter (particles), sRectangle/sRender (shapes).
    insert=true wires it into the main chain: [after or the tool feeding MediaOut] → new → MediaOut.
    Generators (Background, TextPlus, shapes) are merged over the chain with a new Merge. `inputs` sets values."""
    c, _ = _comp(clip, comp, create=create_comp)
    if True:  # note: comp.Lock() stops Resolve from re-rendering scripted changes
        t = c.AddTool(tool_type, x if x is not None else -32768, y if y is not None else -32768)
        if not t:
            fail(f"Unknown tool type '{tool_type}'")
        if name:
            t.SetAttrs({"TOOLS_Name": name})
        wired = None
        if insert:
            out = c.FindToolByID("MediaOut") or next((x for x in (c.GetToolList(False) or {}).values()
                                                      if x.GetAttrs().get("TOOLS_RegID") == "MediaOut"), None)
            prev = _tool(c, after) if after else None
            if out and not prev:
                src = safe(lambda: out.FindMainInput(1).GetConnectedOutput())
                prev = src.GetTool() if src else None
                if prev and prev.GetAttrs().get("TOOLS_Name") == t.GetAttrs().get("TOOLS_Name"):
                    prev = None
            main_in = _main_input_id(t)
            out_in = _main_input_id(out) if out else None
            generators = ("TextPlus", "Background", "Text3D", "FastNoise", "sRender", "pRender", "Renderer3D")
            if main_in and t.GetAttrs().get("TOOLS_RegID") not in generators:
                if prev:
                    t.ConnectInput(main_in, prev)
                if out:
                    out.ConnectInput(out_in, t)
                wired = "chain"
            else:
                if prev and out:
                    m = c.AddTool("Merge", -32768, -32768)
                    m.ConnectInput("Background", prev)
                    m.ConnectInput("Foreground", t)
                    out.ConnectInput(out_in, m)
                    wired = f"merged over chain via {m.GetAttrs()['TOOLS_Name']}"
                elif out:
                    out.ConnectInput(out_in, t)
                    wired = "direct to MediaOut"
        for k, v in (inputs or {}).items():
            t.SetInput(k, _conv_in(v))
    return {"added": t.GetAttrs()["TOOLS_Name"], "type": tool_type, "wired": wired}


def _comp_frame(c, t: Any, fps: float) -> float:
    start = (c.GetAttrs() or {}).get("COMPN_RenderStart", 0) or 0
    if isinstance(t, str) and t.endswith("f"):
        return start + float(t[:-1])
    return start + float(t) * fps


@tool()
def fusion_set(tool_name: str, inputs: dict, clip: Any = "current", comp: Any = None, ease: str | None = None) -> dict:
    """Set inputs of a Fusion tool. Static: {"Size": 0.1, "Center": [0.5, 0.3], "StyledText": "سلام"}.
    Animated: {"Size": {"keyframes": [[0, 0.05], [1.5, 0.12]]}} — times in seconds from the comp start ('12f' for
    frames); creates a spline (points use a path). ease: linear | smooth (default Fusion bezier).
    Connect: {"Input": {"connect": "Blur1"}}. Expressions: {"Angle": {"expression": "time*2"}}."""
    c, it = _comp(clip, comp)
    tl = timeline(required=False)
    fps = TL(tl).fps if tl else 24
    t = _tool(c, tool_name)
    res = {}
    if True:  # note: comp.Lock() stops Resolve from re-rendering scripted changes
        for k, v in inputs.items():
            if isinstance(v, dict) and "keyframes" in v:
                kfs = v["keyframes"]
                first = _conv_in(kfs[0][1])
                mod = "XYPath" if isinstance(first, dict) else "BezierSpline"
                t.AddModifier(k, mod)
                for tm, val in kfs:
                    t.SetInput(k, _conv_in(val), _comp_frame(c, tm, fps))
                if ease == "linear" and mod == "BezierSpline":
                    try:
                        sp = t.FindInput(k) if hasattr(t, "FindInput") else None
                        sp = (sp or t[k]).GetConnectedOutput().GetTool()
                        kd = sp.GetKeyFrames() or {}
                        sp.SetKeyFrames({f: {1: (val[1] if isinstance(val, dict) else val), "Flags": {"Linear": True}}
                                         for f, val in kd.items()}, True)
                    except Exception:  # noqa: BLE001
                        res[k + "_ease"] = "linear ease not applied"
                res[k] = f"{len(kfs)} keyframes"
            elif isinstance(v, dict) and "connect" in v:
                ok = t.ConnectInput(k, _tool(c, v["connect"]))
                res[k] = f"connected ← {v['connect']}" if ok else "connect failed"
            elif isinstance(v, dict) and "expression" in v:
                t.SetExpression(k, v["expression"]) if hasattr(t, "SetExpression") else t[k].SetExpression(v["expression"])
                res[k] = "expression"
            else:
                inp = safe(lambda: t.FindInput(k)) if hasattr(t, "FindInput") else None
                src = safe(lambda: inp.GetConnectedOutput()) if inp else None
                if src is None:
                    src = next((x.GetConnectedOutput() for x in (t.GetInputList() or {}).values()
                                if x.GetAttrs().get("INPS_ID") == k and safe(x.GetConnectedOutput)), None)
                if src is not None and src.GetTool().GetAttrs().get("TOOLS_RegID") in ("BezierSpline", "XYPath", "PolyPath"):
                    t.ConnectInput(k, None)  # static value replaces existing animation
                ok = t.SetInput(k, _conv_in(v))
                res[k] = "set" if ok is not False else "failed"
    return {"tool": tool_name, "results": res}


@tool()
def fusion_connect(from_tool: str, to_tool: str, input: str = "Input", clip: Any = "current", comp: Any = None,
                   disconnect: bool = False) -> dict:
    """Wire `from_tool`'s main output into `to_tool`.`input` (e.g. 'Input', 'Background', 'Foreground',
    'EffectMask'). disconnect=true clears that input."""
    c, _ = _comp(clip, comp)
    dst = _tool(c, to_tool)
    if disconnect:
        ok = dst.ConnectInput(input, None)
    else:
        ok = dst.ConnectInput(input, _tool(c, from_tool))
    return {"ok": ok is not False}


@tool()
def fusion_tool_delete(tool_name: str, clip: Any = "current", comp: Any = None, bypass: bool | None = None) -> dict:
    """Delete a Fusion tool, or bypass/unbypass it (bypass=true/false keeps it)."""
    c, _ = _comp(clip, comp)
    t = _tool(c, tool_name)
    if bypass is not None:
        t.SetAttrs({"TOOLB_PassThrough": bool(bypass)})
        return {"bypassed": bypass}
    t.Delete()
    return {"deleted": tool_name}


@tool()
def fusion_render_settings(clip: Any = "current", comp: Any = None, start: float | None = None,
                           end: float | None = None) -> dict:
    """Read comp attributes (range, fps, size) and optionally set the comp render range (frames)."""
    c, _ = _comp(clip, comp)
    if start is not None or end is not None:
        c.SetAttrs({k: v for k, v in (("COMPN_RenderStart", start), ("COMPN_RenderEnd", end)) if v is not None})
    a = c.GetAttrs() or {}
    return {k: _val(v) for k, v in a.items() if k.startswith("COMP") and not isinstance(v, dict)}
