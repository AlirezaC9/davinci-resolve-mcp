"""Core plumbing: Resolve connection, locking, time conversion and object references.

Every tool goes through `tool()` which serialises access to Resolve (the scripting bridge
is not thread safe) and turns exceptions into readable MCP errors.
"""

from __future__ import annotations

import functools
import inspect
import math
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

# ----------------------------------------------------------------------------------------
# Paths / constants
# ----------------------------------------------------------------------------------------
IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform.startswith("win") or sys.platform == "cygwin"

if IS_MAC:
    APP_PATH = "/Applications/DaVinci Resolve/DaVinci Resolve.app"
    _SCRIPT_API = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
    _SCRIPT_LIB = APP_PATH + "/Contents/Libraries/Fusion/fusionscript.so"
    _LUT_DIR = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/LUT"
    PROCESS_NAME = "Resolve"
elif IS_WIN:
    _pd = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    APP_PATH = r"C:\Program Files\Blackmagic Design\DaVinci Resolve\Resolve.exe"
    _SCRIPT_API = os.path.join(_pd, "Blackmagic Design", "DaVinci Resolve", "Support", "Developer", "Scripting")
    _SCRIPT_LIB = r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll"
    _LUT_DIR = os.path.join(_pd, "Blackmagic Design", "DaVinci Resolve", "Support", "LUT")
    PROCESS_NAME = "Resolve.exe"
else:  # Linux
    APP_PATH = "/opt/resolve/bin/resolve"
    _SCRIPT_API = "/opt/resolve/Developer/Scripting"
    _SCRIPT_LIB = "/opt/resolve/libs/Fusion/fusionscript.so"
    _LUT_DIR = "/opt/resolve/LUT"
    PROCESS_NAME = "resolve"

# Every path can be overridden for non-standard installs.
APP_PATH = os.environ.get("RESOLVE_APP_PATH", APP_PATH)
SCRIPT_API = os.environ.get("RESOLVE_SCRIPT_API", _SCRIPT_API)
SCRIPT_LIB = os.environ.get("RESOLVE_SCRIPT_LIB", _SCRIPT_LIB)
LUT_DIR = os.environ.get("RESOLVE_LUT_DIR", _LUT_DIR)
WORK_DIR = Path(os.environ.get("RESOLVE_MCP_HOME", os.path.join(os.path.expanduser("~"), "Documents", "ResolveMCP")))
for sub in ("previews", "snapshots", "luts", "tmp", "renders"):
    (WORK_DIR / sub).mkdir(parents=True, exist_ok=True)

os.environ.setdefault("RESOLVE_SCRIPT_API", SCRIPT_API)
os.environ.setdefault("RESOLVE_SCRIPT_LIB", SCRIPT_LIB)
_MODULES = os.path.join(SCRIPT_API, "Modules")
if _MODULES not in sys.path:
    sys.path.append(_MODULES)

LOCK = threading.RLock()
REGISTRY: dict[str, Callable[..., Any]] = {}


class ResolveError(Exception):
    """A user-facing error."""


def fail(msg: str) -> None:
    raise ResolveError(msg)


# ----------------------------------------------------------------------------------------
# MCP server + tool decorator
# ----------------------------------------------------------------------------------------
INSTRUCTIONS = """DaVinci Resolve control (live, through Resolve's scripting API; DaVinci Resolve Studio must be running —
call `launch` if `status` says it is not).

Conventions
- TIME: numbers are SECONDS from the timeline start (0 = first frame of the timeline). Strings may be a
  timecode "01:00:10:05" (absolute timeline TC), frames "120f" (relative to timeline start) or "+2.5"
  (relative, where noted). Results report seconds plus frame numbers ("*_f", relative to timeline start).
- SOURCE times (in/out of a media clip) are seconds from the media's first frame (or "48f", or a source TC).
- Tracks are written like the UI: "V1", "V2", "A1", "S1" (subtitle). 1-based.
- A timeline clip is referenced as "V1:0" (first clip on V1, 0-based, transitions not counted; -1 = last), {"track":"A1","time":12.5}
  (clip under that time), its unique id (from timeline_get), or "selected". Lists accept arrays.
  Indexes shift after edits — re-read with timeline_get or use ids.
- Media pool items: unique id, bin path "Footage/Interview/A001.mov", exact clip name, or absolute file path.
- Colors (markers/flags): Blue Cyan Green Yellow Red Pink Purple Fuchsia Rose Lavender Sky Mint Lemon Sand Cocoa Cream.
  Clip colors: Orange Apricot Yellow Lime Olive Green Teal Navy Blue Purple Violet Pink Tan Beige Brown Chocolate.

Recommended workflow
1. status → media_pool / timelines / timeline_get to see what exists.
2. project_snapshot before big or destructive changes (the scripting API has no undo; `undo` sends Cmd+Z to the UI).
3. Edit: media_import → timeline_create / clips_place / clips_assemble → razor, clip_trim, clip_move, range_remove,
   clips_delete(ripple) → transition_add, clip_fade, clip_properties (transform/crop/opacity/composite/volume/pan),
   clip_speed, title_add / title_update, generator_add, markers.
4. Color: grade_look (exposure/contrast/sat/temp/tint/lift/gamma/gain via generated LUT), grade_cdl, grade_lut,
   grade_copy, nodes, color_versions, color_groups, stills, clip_ai (magic mask/stabilize/smart reframe).
5. Fusion: fusion_comp_info / fusion_tool_add / fusion_set (supports keyframes) / fusion_connect on any clip's comp.
6. Audio: clip_properties volume/pan, audio_normalize, voice_isolation, transcribe + transcript (word timings
   for text-based editing), subtitles_from_audio, tts_generate.
7. ALWAYS verify visually with frame_preview (or frames_contact for several times) after meaningful changes.
8. Deliver: render_options → render_timeline (one-shot) or render_job_add + render_start + render_status.
run_python exposes the full API (resolve, project, mp, tl, H helpers) for anything without a dedicated tool;
api_search finds API functions. batch runs several tool calls in one round trip.
Report results to the user in their language (they often write in Persian).
"""

mcp = MCPServer(name="davinci-resolve", instructions=INSTRUCTIONS, version="1.0.0")


DEFAULT_TIMEOUT = float(os.environ.get("RESOLVE_MCP_TIMEOUT", "120"))
_QUEUE = None
_WORKER_IDENT: int | None = None


def _worker_loop(q) -> None:
    global _WORKER_IDENT
    _WORKER_IDENT = threading.get_ident()
    while True:
        fn, box, done = q.get()
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001
            box["error"] = e
        done.set()


def _queue():
    global _QUEUE
    if _QUEUE is None:
        import queue

        _QUEUE = queue.Queue()
        # daemon: a call stuck behind a Resolve dialog must not keep the process alive
        threading.Thread(target=_worker_loop, args=(_QUEUE,), name="resolve-worker", daemon=True).start()
    return _QUEUE


BLOCKED_MSG = ("DaVinci Resolve did not answer within {t:.0f}s. It is most likely showing a modal dialog "
               "(e.g. 'frame rate mismatch' or 'save changes?') or is busy. Ask the user to look at Resolve and close "
               "the dialog. If it still does not respond afterwards, Resolve must be restarted.")


def run_in_worker(fn: Callable[[], Any], timeout: float) -> Any:
    """Run on the single Resolve worker thread with a timeout (nested calls run inline)."""
    if threading.get_ident() == _WORKER_IDENT:
        return fn()
    box: dict[str, Any] = {}
    done = threading.Event()
    _queue().put((fn, box, done))
    if not done.wait(timeout):
        raise ResolveError(BLOCKED_MSG.format(t=timeout))
    if "error" in box:
        raise box["error"]
    return box.get("value")


def tool(name: str | None = None, read_only: bool = False, timeout: float | None = None):
    """Register a function as an MCP tool: runs on the Resolve worker thread with a timeout and friendly errors.
    Tools with a `wait`/`timeout` argument get that budget (+60s)."""

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            budget = timeout or DEFAULT_TIMEOUT
            if isinstance(kwargs.get("timeout"), (int, float)):
                budget = max(budget, float(kwargs["timeout"]) + 60)
            try:
                return run_in_worker(lambda: jsonable(fn(*args, **kwargs)), budget)
            except ResolveError as e:
                raise ToolError(str(e)) from None
            except ToolError:
                raise
            except Exception as e:  # noqa: BLE001
                raise ToolError(f"{type(e).__name__}: {e}") from None

        tname = name or fn.__name__
        mcp.add_tool(wrapper, name=tname, description=inspect.getdoc(fn), structured_output=False)
        REGISTRY[tname] = wrapper
        return fn

    return deco


# ----------------------------------------------------------------------------------------
# JSON helpers
# ----------------------------------------------------------------------------------------
def jsonable(v: Any) -> Any:
    from mcp.server.mcpserver.utilities.types import Image

    if isinstance(v, Image):
        return v
    if isinstance(v, dict):
        return {(str(int(k)) if isinstance(k, float) and k.is_integer() else str(k)): jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
        return round(v, 4)
    if v is None or isinstance(v, (str, int, bool)):
        return v
    if hasattr(v, "to_image_content"):
        return v
    return str(v)


# ----------------------------------------------------------------------------------------
# Connection
# ----------------------------------------------------------------------------------------
_resolve = None


def _connect():
    global _resolve
    import DaVinciResolveScript as dvr  # type: ignore

    _resolve = dvr.scriptapp("Resolve")
    return _resolve


def is_running() -> bool:
    if IS_WIN:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {PROCESS_NAME}"], capture_output=True, text=True).stdout
        return PROCESS_NAME.lower() in out.lower()
    return subprocess.run(["pgrep", "-x", PROCESS_NAME], capture_output=True).returncode == 0


def resolve():
    """Return a live Resolve object, reconnecting if needed."""
    global _resolve
    r = _resolve
    if r is not None:
        try:
            if r.GetProductName():
                return r
        except Exception:  # noqa: BLE001
            pass
    if not is_running():
        fail("DaVinci Resolve is not running. Call `launch` to start it.")
    r = _connect()
    if not r:
        fail("Could not connect to DaVinci Resolve. Make sure it finished starting and that "
             "Preferences > System > General > External scripting using = Local (DaVinci Resolve Studio is required for external scripting).")
    return r


def launch_app(wait: float = 90) -> Any:
    if not is_running():
        if IS_MAC:
            subprocess.run(["open", "-a", APP_PATH])
        else:
            subprocess.Popen([APP_PATH], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.time()
    while time.time() - t0 < wait:
        if is_running():
            try:
                r = _connect()
                if r and r.GetProjectManager() and r.GetProjectManager().GetCurrentProject():
                    return r
            except Exception:  # noqa: BLE001
                pass
        time.sleep(2)
    fail(f"Resolve did not become ready within {wait}s")


def pm():
    return resolve().GetProjectManager()


def project():
    p = pm().GetCurrentProject()
    if not p:
        fail("No project is open.")
    return p


def media_pool():
    return project().GetMediaPool()


def timeline(name: str | None = None, required: bool = True):
    p = project()
    if name:
        for i in range(1, p.GetTimelineCount() + 1):
            t = p.GetTimelineByIndex(i)
            if t.GetName() == name or t.GetUniqueId() == name:
                return t
        fail(f"Timeline '{name}' not found. Timelines: {[p.GetTimelineByIndex(i).GetName() for i in range(1, p.GetTimelineCount() + 1)]}")
    t = p.GetCurrentTimeline()
    if not t and required:
        fail("No current timeline. Create one with timeline_create or open one with timeline_open.")
    return t


# ----------------------------------------------------------------------------------------
# Time conversion
# ----------------------------------------------------------------------------------------
class TL:
    """Timing info of a timeline."""

    def __init__(self, tl):
        self.tl = tl
        s = tl.GetSettings() or {}
        self.fps = float(s.get("timelineFrameRate") or 24)
        self.drop = str(s.get("timelineDropFrameTimecode", "0")) == "1"
        self.nominal = int(round(self.fps))
        self.start = int(tl.GetStartFrame())

    # absolute frame <-> seconds (relative to timeline start)
    def sec(self, abs_frame: float) -> float:
        return round((abs_frame - self.start) / self.fps, 4)

    def rel(self, abs_frame: float) -> int:
        return int(round(abs_frame - self.start))

    def tc(self, abs_frame: float) -> str:
        return frames_to_tc(int(round(abs_frame)), self.fps, self.drop)

    def frame(self, t: Any, relative_to: float | None = None) -> int:
        """Convert a time spec to an ABSOLUTE timeline frame."""
        if t is None:
            fail("time is required")
        if isinstance(t, bool):
            fail(f"bad time {t!r}")
        if isinstance(t, (int, float)):
            return int(round(self.start + float(t) * self.fps))
        s = str(t).strip()
        if s.startswith("+") or (s.startswith("-") and relative_to is not None):
            if relative_to is None:
                fail(f"relative time {s!r} not allowed here")
            return int(round(relative_to + self.delta(s)))
        if s.endswith("f") and re.fullmatch(r"-?\d+(\.\d+)?f", s):
            return int(round(self.start + float(s[:-1])))
        if re.fullmatch(r"\d{1,2}[:;]\d{2}[:;]\d{2}[:;.]\d{2,3}", s):
            return tc_to_frames(s, self.fps, self.drop)
        try:
            return int(round(self.start + float(s) * self.fps))
        except ValueError:
            fail(f"Cannot parse time {t!r}. Use seconds (12.5), frames ('300f') or timecode ('01:00:12:12').")
        return 0

    def delta(self, t: Any) -> float:
        """Duration spec (seconds / 'Nf') -> frames."""
        if isinstance(t, (int, float)):
            return float(t) * self.fps
        s = str(t).strip().lstrip("+")
        neg = s.startswith("-")
        s = s.lstrip("-")
        v = float(s[:-1]) if s.endswith("f") else float(s) * self.fps
        return -v if neg else v

    def info(self, abs_frame: float, key: str) -> dict:
        return {key: self.sec(abs_frame), key + "_f": self.rel(abs_frame)}


def tc_to_frames(tc: str, fps: float, drop: bool = False) -> int:
    parts = re.split(r"[:;.]", tc)
    h, m, s, f = (int(x) for x in parts)
    nominal = int(round(fps))
    is_drop = drop or ";" in tc
    if is_drop and nominal in (30, 60):
        d = 2 if nominal == 30 else 4
        total_min = 60 * h + m
        return (h * 3600 + m * 60 + s) * nominal + f - d * (total_min - total_min // 10)
    return (h * 3600 + m * 60 + s) * nominal + f


def frames_to_tc(frames: int, fps: float, drop: bool = False) -> str:
    nominal = int(round(fps)) or 24
    sep = ":"
    if drop and nominal in (30, 60):
        d = 2 if nominal == 30 else 4
        per10 = nominal * 600 - d * 9
        per1 = nominal * 60 - d
        tens, rem = divmod(frames, per10)
        if rem > d:
            frames += d * 9 * tens + d * ((rem - d) // per1)
        else:
            frames += d * 9 * tens
        sep = ";"
    f = frames % nominal
    s = (frames // nominal) % 60
    m = (frames // (nominal * 60)) % 60
    h = frames // (nominal * 3600)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{f:02d}"


def clip_fps(mpi) -> float:
    try:
        v = float(mpi.GetClipProperty("FPS") or 0)
    except Exception:  # noqa: BLE001
        v = 0
    return v


def source_frame(mpi, t: Any, tlfps: float) -> int:
    """Source time spec -> source frame index (0-based from the media's first frame)."""
    fps = clip_fps(mpi) or tlfps
    if isinstance(t, (int, float)):
        return int(round(float(t) * fps))
    s = str(t).strip()
    if s.endswith("f"):
        return int(round(float(s[:-1])))
    if re.fullmatch(r"\d{1,2}[:;]\d{2}[:;]\d{2}[:;.]\d{2,3}", s):
        start_tc = mpi.GetClipProperty("Start TC") or "00:00:00:00"
        return tc_to_frames(s, fps) - tc_to_frames(start_tc, fps)
    return int(round(float(s) * fps))


# ----------------------------------------------------------------------------------------
# Tracks
# ----------------------------------------------------------------------------------------
TRACK_TYPES = {"V": "video", "A": "audio", "S": "subtitle"}


def parse_track(t: Any, default_type: str = "video") -> tuple[str, int]:
    if isinstance(t, int):
        return default_type, t
    s = str(t).strip().upper()
    m = re.fullmatch(r"([VAS])?(\d+)", s)
    if not m:
        fail(f"Bad track {t!r}. Use 'V1', 'A2', 'S1'.")
    return (TRACK_TYPES[m.group(1)] if m.group(1) else default_type), int(m.group(2))


def track_label(ttype: str, idx: int) -> str:
    return {"video": "V", "audio": "A", "subtitle": "S"}[ttype] + str(idx)


def ensure_track(tl, ttype: str, idx: int) -> None:
    while tl.GetTrackCount(ttype) < idx:
        if not tl.AddTrack(ttype, "stereo" if ttype == "audio" else None) and not tl.AddTrack(ttype):
            fail(f"Could not add {ttype} track")


def track_items(tl, ttype: str, idx: int) -> list:
    return [i for i in (tl.GetItemListInTrack(ttype, idx) or []) if i]


def clip_items(tl, ttype: str, idx: int) -> list:
    """Track items without transitions (what 'V1:n' indexes count)."""
    return [i for i in track_items(tl, ttype, idx) if safe(i.GetType) != "transition"]


def all_tracks(tl, types=("video", "audio", "subtitle")):
    for tt in types:
        for i in range(1, (tl.GetTrackCount(tt) or 0) + 1):
            yield tt, i


# ----------------------------------------------------------------------------------------
# Timeline item references
# ----------------------------------------------------------------------------------------
def find_item(tl, ref: Any):
    if ref is None:
        fail("clip reference is required")
    if isinstance(ref, dict):
        if "id" in ref:
            return find_item(tl, ref["id"])
        tt, ti = parse_track(ref.get("track", "V1"))
        T = TL(tl)
        f = T.frame(ref.get("time", 0))
        for it in clip_items(tl, tt, ti):
            if it.GetStart() <= f < it.GetEnd():
                return it
        fail(f"No clip on {track_label(tt, ti)} at {ref.get('time')}")
    s = str(ref).strip()
    if s.lower() == "selected":
        sel = tl.GetSelectedClips() or []
        if not sel:
            fail("No clip is selected in the timeline")
        return sel[0]
    m = re.fullmatch(r"([VAS]\d+):(-?\d+)", s.upper())
    if m:
        tt, ti = parse_track(m.group(1))
        items = clip_items(tl, tt, ti)
        k = int(m.group(2))
        if not items or k >= len(items) or k < -len(items):
            fail(f"{m.group(1)} has {len(items)} clips; index {k} out of range")
        return items[k]
    named = []
    for tt, ti in all_tracks(tl):
        for it in track_items(tl, tt, ti):
            if it.GetUniqueId() == s:
                return it
            if it.GetName() == s:
                named.append(it)
    if named:
        return named[0]
    fail(f"Timeline clip {ref!r} not found. Use 'V1:0', {{track,time}} or an id from timeline_get.")


def find_items(tl, refs: Any) -> list:
    if refs is None:
        fail("clips are required")
    if isinstance(refs, str) and refs.lower() == "selected":
        sel = tl.GetSelectedClips() or []
        if not sel:
            fail("No clips are selected")
        return list(sel)
    if isinstance(refs, str) and re.fullmatch(r"[VAS]\d+", refs.strip().upper()):
        tt, ti = parse_track(refs)
        return clip_items(tl, tt, ti)
    if not isinstance(refs, list):
        refs = [refs]
    return [find_item(tl, r) for r in refs]


def item_ref(tl, it) -> str:
    try:
        if safe(it.GetType) == "transition":
            return "transition"
        tt, ti = it.GetTrackTypeAndIndex()
        items = clip_items(tl, tt, int(ti))
        uid = it.GetUniqueId()
        for k, x in enumerate(items):
            if x.GetUniqueId() == uid:
                return f"{track_label(tt, int(ti))}:{k}"
    except Exception:  # noqa: BLE001
        pass
    return "?"


def describe_item(tl, it, T: TL | None = None, detail: bool = False) -> dict:
    T = T or TL(tl)
    st, en = it.GetStart(), it.GetEnd()
    d: dict[str, Any] = {
        "ref": item_ref(tl, it),
        "id": it.GetUniqueId(),
        "name": it.GetName(),
        "type": safe(it.GetType),
        "start": T.sec(st), "end": T.sec(en), "dur": round((en - st) / T.fps, 4),
        "start_f": T.rel(st), "end_f": T.rel(en),
    }
    mpi = safe(it.GetMediaPoolItem)
    if mpi:
        fps = clip_fps(mpi) or T.fps
        try:
            s_in, s_out = it.GetSourceStartFrame(), it.GetSourceEndFrame()
            d.update({"src_in": round(s_in / fps, 4), "src_out": round(s_out / fps, 4), "src_in_f": s_in, "src_out_f": s_out})
        except Exception:  # noqa: BLE001
            pass
        d["media_id"] = mpi.GetUniqueId()
    if not safe(it.GetClipEnabled, True):
        d["enabled"] = False
    col = safe(it.GetClipColor)
    if col:
        d["color"] = col
    if detail:
        d["properties"] = safe(it.GetProperties, {})
        d["speed"] = safe(it.GetSpeed)
        d["fades"] = safe(it.GetFades)
        d["flags"] = safe(it.GetFlagList)
        d["linked"] = [item_ref(tl, x) for x in (safe(it.GetLinkedItems, []) or [])]
        d["fusion_comps"] = safe(it.GetFusionCompNameList)
        mk = safe(it.GetMarkers, {}) or {}
        if mk:
            d["markers"] = {str(int(k)): v for k, v in mk.items()}
        if mpi:
            d["file"] = mpi.GetClipProperty("File Path")
        try:
            g = it.GetNodeGraph()
            if g:
                d["nodes"] = g.GetNumNodes()
        except Exception:  # noqa: BLE001
            pass
        d["version"] = safe(it.GetCurrentVersion)
    return d


def safe(fn, default=None):
    try:
        v = fn()
        return default if v is None else v
    except Exception:  # noqa: BLE001
        return default


# ----------------------------------------------------------------------------------------
# Media pool references
# ----------------------------------------------------------------------------------------
def walk_folder(folder, path=""):
    yield path, folder
    for sub in folder.GetSubFolderList() or []:
        yield from walk_folder(sub, f"{path}/{sub.GetName()}" if path else sub.GetName())


def find_folder(ref: Any = None, create: bool = False):
    mp = media_pool()
    root = mp.GetRootFolder()
    if ref in (None, "", "/", "Master", root.GetName()):
        return root
    if not isinstance(ref, str):
        return ref
    for path, f in walk_folder(root):
        if f.GetUniqueId() == ref:
            return f
    parts = [p for p in ref.strip("/").split("/") if p]
    if parts and parts[0] == root.GetName():
        parts = parts[1:]
    cur = root
    for part in parts:
        nxt = next((s for s in cur.GetSubFolderList() or [] if s.GetName() == part), None)
        if not nxt:
            if not create:
                fail(f"Bin '{ref}' not found")
            nxt = mp.AddSubFolder(cur, part)
            if not nxt:
                fail(f"Could not create bin '{part}'")
        cur = nxt
    return cur


def find_mpitem(ref: Any):
    if ref is None:
        fail("media reference is required")
    if not isinstance(ref, str):
        return ref
    s = ref.strip()
    root = media_pool().GetRootFolder()
    # bin path
    if "/" in s and not s.startswith("/"):
        folder_path, _, name = s.rpartition("/")
        try:
            f = find_folder(folder_path)
            for c in f.GetClipList() or []:
                if c.GetName() == name:
                    return c
        except ResolveError:
            pass
    by_name = None
    for _, f in walk_folder(root):
        for c in f.GetClipList() or []:
            if c.GetUniqueId() == s or safe(c.GetMediaId) == s:
                return c
            if by_name is None and c.GetName() == s:
                by_name = c
            if s.startswith("/") and c.GetClipProperty("File Path") == s:
                return c
    if by_name:
        return by_name
    if s.startswith("/") and os.path.exists(s):
        fail(f"'{s}' is not in the media pool yet — import it with media_import first.")
    fail(f"Media pool item {ref!r} not found. Use media_pool to list items.")


def describe_mpitem(c, detail: bool = False) -> dict:
    props = safe(c.GetClipProperty, {}) or {}
    d = {
        "id": c.GetUniqueId(),
        "name": c.GetName(),
        "type": props.get("Type"),
        "duration": props.get("Duration"),
        "fps": props.get("FPS"),
        "res": props.get("Resolution"),
    }
    if props.get("File Path"):
        d["file"] = props.get("File Path")
    if props.get("Clip Color"):
        d["color"] = props.get("Clip Color")
    if detail:
        d["properties"] = props
        d["metadata"] = safe(c.GetMetadata, {})
        d["markers"] = safe(c.GetMarkers, {})
        d["flags"] = safe(c.GetFlagList)
        d["mark_in_out"] = safe(c.GetMarkInOut)
        d["third_party_metadata"] = safe(c.GetThirdPartyMetadata, {})
    return {k: v for k, v in d.items() if v not in (None, "")}


def const(name: str):
    """Resolve enum constant by name (e.g. 'EXPORT_EDL')."""
    v = getattr(resolve(), name, None)
    if v is None:
        fail(f"Unknown Resolve constant {name}")
    return v


def pick_const(prefix: str, value: Any, aliases: dict | None = None):
    """Map a friendly string ('optical_flow') to resolve.<PREFIX>OPTICAL_FLOW; ints pass through."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    key = str(value).strip().upper().replace(" ", "_").replace("-", "_").replace(".", "_")
    if aliases and str(value).lower() in aliases:
        key = aliases[str(value).lower()]
    return const(prefix + key)
