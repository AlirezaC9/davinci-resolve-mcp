"""Escape hatches: arbitrary Python against the API, API search, and batching."""

from __future__ import annotations

import ast
import contextlib
import io
import os
import re
import traceback
from typing import Any

from . import core
from .core import REGISTRY, SCRIPT_API, TL, fail, tool


class _Helpers:
    """Helpers exposed as `H` inside run_python."""

    item = staticmethod(lambda ref: core.find_item(core.timeline(), ref))
    items = staticmethod(lambda refs: core.find_items(core.timeline(), refs))
    media = staticmethod(core.find_mpitem)
    folder = staticmethod(core.find_folder)
    timeline = staticmethod(core.timeline)
    describe = staticmethod(lambda it: core.describe_item(core.timeline(), it))
    describe_media = staticmethod(core.describe_mpitem)
    const = staticmethod(core.const)

    @staticmethod
    def frame(t: Any) -> int:
        """time spec → absolute timeline frame"""
        return TL(core.timeline()).frame(t)

    @staticmethod
    def sec(abs_frame: float) -> float:
        return TL(core.timeline()).sec(abs_frame)

    @staticmethod
    def tc(abs_frame: float) -> str:
        return TL(core.timeline()).tc(abs_frame)

    @staticmethod
    def call(tool_name: str, **kwargs: Any) -> Any:
        """Call another MCP tool by name."""
        return REGISTRY[tool_name](**kwargs)


@tool(timeout=600)
def run_python(code: str) -> dict:
    """Run Python against the live Resolve API for anything without a dedicated tool. Pre-defined: resolve, pm
    (ProjectManager), project, mp (MediaPool), tl (current timeline), fusion, ms (MediaStorage), H (helpers:
    H.item('V1:0'), H.items(...), H.media(ref), H.folder(path), H.frame(seconds|'tc'|'Nf'), H.sec(f), H.tc(f),
    H.describe(item), H.const('EXPORT_EDL'), H.call('tool_name', **args)), os, json, time.
    The value of the last expression (or a variable named `result`) is returned, plus printed output.
    Use api_search to look up functions."""
    import json
    import time

    r = core.resolve()
    pmx = r.GetProjectManager()
    proj = pmx.GetCurrentProject()
    ns: dict[str, Any] = {
        "resolve": r, "pm": pmx, "project": proj, "mp": proj.GetMediaPool() if proj else None,
        "tl": proj.GetCurrentTimeline() if proj else None, "fusion": r.Fusion(), "ms": r.GetMediaStorage(),
        "H": _Helpers, "os": os, "json": json, "time": time, "bmd": None,
    }
    buf = io.StringIO()
    result = None
    try:
        tree = ast.parse(code, mode="exec")
        last = None
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            last = ast.Expression(tree.body.pop().value)
        with contextlib.redirect_stdout(buf):
            exec(compile(tree, "<run_python>", "exec"), ns)
            if last is not None:
                result = eval(compile(last, "<run_python>", "eval"), ns)
        if result is None:
            result = ns.get("result")
    except Exception:  # noqa: BLE001
        tb = traceback.format_exc(limit=4)
        return {"error": tb.splitlines()[-1], "traceback": tb, "stdout": buf.getvalue()[-4000:] or None}
    return {"result": core.jsonable(result), "stdout": buf.getvalue()[-8000:] or None}


_API_CACHE: list[str] = []


@tool(read_only=True)
def api_search(pattern: str, readme: bool = False, max_results: int = 60) -> dict:
    """Search Resolve's scripting API reference (regex, case-insensitive) for classes, methods, constants and settings
    keys — returns matching signatures with docs and owning class. readme=true searches the README notes instead."""
    if readme:
        with open(os.path.join(SCRIPT_API, "README.md")) as f:
            txt = f.read()
        secs = re.split(r"\n(?=#+ )", txt)
        hits = [s.strip()[:2500] for s in secs if re.search(pattern, s, re.I)]
        return {"sections": hits[:8]}
    if not _API_CACHE:
        with open(os.path.join(SCRIPT_API, "DaVinciResolveScript.pyi")) as f:
            _API_CACHE.extend(f.read().splitlines())
    lines = _API_CACHE
    rx = re.compile(pattern, re.I)
    out = []
    cls = None
    for i, ln in enumerate(lines):
        m = re.match(r"(?:class |)(\w+)\s*(?:\(|=\s*TypedDict)", ln)
        if ln.startswith("class ") or (m and "TypedDict(" in ln):
            cls = (m.group(1) if m else ln)
        s = ln.strip()
        if not s or s == "..." or s.startswith('"""'):
            continue
        if rx.search(s):
            doc = ""
            if i + 1 < len(lines) and lines[i + 1].strip().startswith('"""'):
                doc = lines[i + 1].strip().strip('"')
            out.append({"in": cls, "line": s[:220], **({"doc": doc[:300]} if doc else {})})
            if len(out) >= max_results:
                break
    return {"matches": out, "truncated": len(out) >= max_results or None}


@tool(timeout=1800)
def batch(steps: list[dict], stop_on_error: bool = True) -> dict:
    """Run several tools in one round trip: steps = [{"tool": "razor", "args": {"times": [3.2]}}, …].
    Images from preview tools are not returned here (call them directly)."""
    out = []
    for k, st in enumerate(steps):
        name = st.get("tool")
        fn = REGISTRY.get(name or "")
        if not fn:
            out.append({"step": k, "tool": name, "error": "unknown tool"})
            if stop_on_error:
                break
            continue
        try:
            res = fn(**(st.get("args") or {}))
            if isinstance(res, list):
                res = [x for x in res if isinstance(x, (dict, str, int, float, list))]
            out.append({"step": k, "tool": name, "result": res})
        except Exception as e:  # noqa: BLE001
            out.append({"step": k, "tool": name, "error": str(e)})
            if stop_on_error:
                break
    return {"steps": out}
