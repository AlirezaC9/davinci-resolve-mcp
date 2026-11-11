"""Application, project and database tools."""

from __future__ import annotations

import subprocess
import time
from datetime import datetime
from typing import Any

from .core import (
    APP_PATH, TL, WORK_DIR, fail, is_running, launch_app, pm, project, resolve, safe, timeline, tool,
)

PAGES = ["media", "cut", "edit", "fusion", "color", "fairlight", "deliver", "photo"]


@tool(read_only=True)
def status() -> dict:
    """Overview: Resolve version/edition, current page, project, timeline, playhead and media pool size.
    Call first in every session."""
    if not is_running():
        return {"running": False, "hint": "Call launch to start DaVinci Resolve."}
    r = resolve()
    out: dict[str, Any] = {
        "running": True,
        "product": r.GetProductName(),
        "version": r.GetVersionString(),
        "studio": r.IsStudio(),
        "page": r.GetCurrentPage(),
    }
    p = pm().GetCurrentProject()
    if p:
        s = p.GetSettings() or {}
        out["project"] = {
            "name": p.GetName(),
            "timelines": p.GetTimelineCount(),
            "resolution": f"{s.get('timelineResolutionWidth')}x{s.get('timelineResolutionHeight')}",
            "fps": s.get("timelineFrameRate"),
            "color_science": s.get("colorScienceMode"),
        }
        tl = p.GetCurrentTimeline()
        if tl:
            T = TL(tl)
            cur = tl.GetCurrentTimecode()
            ts = tl.GetSettings() or {}
            out["timeline"] = {
                "name": tl.GetName(),
                "fps": T.fps,
                "resolution": f"{ts.get('timelineResolutionWidth')}x{ts.get('timelineResolutionHeight')}",
                "start_tc": tl.GetStartTimecode(),
                "duration": round((tl.GetEndFrame() - tl.GetStartFrame()) / T.fps, 3),
                "tracks": {t: tl.GetTrackCount(t) for t in ("video", "audio", "subtitle")},
                "playhead_tc": cur,
            }
            try:
                from .core import tc_to_frames
                out["timeline"]["playhead"] = T.sec(tc_to_frames(cur, T.fps, T.drop))
            except Exception:  # noqa: BLE001
                pass
        root = p.GetMediaPool().GetRootFolder()
        out["media_pool_root_items"] = len(root.GetClipList() or [])
        out["render_in_progress"] = p.IsRenderingInProgress()
    else:
        out["project"] = None
    out["work_dir"] = str(WORK_DIR)
    return out


@tool(timeout=200)
def launch(wait: float = 90) -> dict:
    """Start DaVinci Resolve (if not running) and wait until the scripting API answers."""
    already = is_running()
    launch_app(wait)
    return {"launched": not already, **status()}


@tool()
def page(name: str | None = None) -> dict:
    """Get the current page, or switch to one of: media, cut, edit, fusion, color, fairlight, deliver, photo."""
    r = resolve()
    if name:
        if name.lower() not in PAGES:
            fail(f"page must be one of {PAGES}")
        if not r.OpenPage(name.lower()):
            fail(f"Could not open page {name}")
        time.sleep(0.3)
    return {"page": r.GetCurrentPage()}


@tool()
def save() -> dict:
    """Save the current project."""
    ok = pm().SaveProject()
    return {"saved": bool(ok), "project": project().GetName()}


def _keystroke(key: str, modifiers: str = "command down", times: int = 1) -> None:
    import sys
    if sys.platform != "darwin":
        fail("undo/redo via keystrokes is only implemented on macOS. Use Ctrl+Z in Resolve, or restore a project_snapshot.")
    script = f'''
    tell application "DaVinci Resolve" to activate
    delay 0.3
    tell application "System Events"
        repeat {times} times
            keystroke "{key}" using {{{modifiers}}}
            delay 0.15
        end repeat
    end tell'''
    res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if res.returncode != 0:
        fail("Could not send keystrokes to Resolve (" + res.stderr.strip() + "). Grant Accessibility permission to the "
             "app that runs this MCP server (System Settings > Privacy & Security > Accessibility). "
             "Alternative: restore a project_snapshot.")


@tool()
def undo(steps: int = 1) -> dict:
    """Undo the last N actions by sending Cmd+Z to Resolve (the scripting API has no undo).
    Needs macOS Accessibility permission. Scripted edits may count as several undo steps — verify with timeline_get."""
    _keystroke("z", "command down", max(1, steps))
    return {"undone": steps}


@tool()
def redo(steps: int = 1) -> dict:
    """Redo N actions (Cmd+Shift+Z) in Resolve."""
    _keystroke("z", "command down, shift down", max(1, steps))
    return {"redone": steps}


# ------------------------------------------------------------------------------ projects
def _goto_folder(path: str | None) -> None:
    m = pm()
    m.GotoRootFolder()
    if path:
        for part in [p for p in path.strip("/").split("/") if p]:
            if not m.OpenFolder(part):
                fail(f"Project folder '{part}' not found")


@tool(read_only=True)
def projects_list(folder: str | None = None) -> dict:
    """List projects and sub-folders in the project manager (folder = path like 'Clients/2026'; root by default)."""
    m = pm()
    _goto_folder(folder)
    attrs = safe(m.GetProjectAttributesInCurrentFolder, {}) or {}
    projects = []
    for name in m.GetProjectListInCurrentFolder() or []:
        a = attrs.get(name, {}) or {}
        projects.append({"name": name, "modified": a.get("lastModifiedDate"), "notes": a.get("notes") or None})
    cur = m.GetCurrentProject()
    return {
        "database": safe(m.GetCurrentDatabase),
        "folder": folder or "/",
        "folders": m.GetFolderListInCurrentFolder(),
        "projects": projects,
        "current_project": cur.GetName() if cur else None,
    }


@tool()
def project_open(name: str, folder: str | None = None, save_current: bool = True) -> dict:
    """Open (load) a project by name. The current project is saved first unless save_current=false."""
    m = pm()
    if save_current and m.GetCurrentProject():
        m.SaveProject()
    _goto_folder(folder)
    p = m.LoadProject(name)
    if not p:
        fail(f"Could not load project '{name}'. Available: {m.GetProjectListInCurrentFolder()}")
    return status()


@tool()
def project_create(name: str, width: int | None = None, height: int | None = None, fps: str | None = None,
                   folder: str | None = None, settings: dict | None = None, media_location: str | None = None) -> dict:
    """Create and open a new project. Optionally set timeline resolution (width/height), frame rate
    (fps as string, e.g. '25', '29.97', '29.97 DF', '23.976'), and any other project settings.
    Set resolution/fps BEFORE creating timelines; Resolve locks fps once a timeline exists."""
    m = pm()
    if m.GetCurrentProject():
        m.SaveProject()
    _goto_folder(folder)
    p = m.CreateProject(name, media_location) if media_location else m.CreateProject(name)
    if not p:
        fail(f"Could not create project '{name}' (does it already exist?)")
    s = dict(settings or {})
    if width:
        s["timelineResolutionWidth"] = str(width)
    if height:
        s["timelineResolutionHeight"] = str(height)
    if fps:
        s["timelineFrameRate"] = str(fps)
        s["timelinePlaybackFrameRate"] = str(fps).replace(" DF", "")
    errors = _apply_settings(p, s)
    return {"created": name, "settings_errors": errors or None, **status()}


def _apply_settings(obj, settings: dict) -> list[str]:
    errors = []
    if not settings:
        return errors
    try:
        if obj.SetSettings({k: str(v) if not isinstance(v, str) else v for k, v in settings.items()}):
            return errors
    except Exception:  # noqa: BLE001
        pass
    for k, v in settings.items():
        if k == "timelinePlaybackFrameRate":
            continue
        try:
            ok = obj.SetSetting(k, str(v))
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            errors.append(k)
    return errors


@tool()
def project_settings(set: dict | None = None, keys: list[str] | None = None, search: str | None = None) -> dict:
    """Read or change project settings (Project Settings dialog). With `set`, applies {key: value}
    (e.g. {"timelineResolutionWidth":"1080","timelineResolutionHeight":"1920","colorScienceMode":"davinciYRGBColorManagedv2"}).
    Read: `keys` returns those keys, `search` filters keys by substring; neither returns everything."""
    p = project()
    out: dict[str, Any] = {}
    if set:
        out["errors"] = _apply_settings(p, set) or None
    s = p.GetSettings() or {}
    if keys:
        out["settings"] = {k: s.get(k) for k in keys}
    elif search:
        out["settings"] = {k: v for k, v in s.items() if search.lower() in k.lower()}
    elif set:
        out["settings"] = {k: s.get(k) for k in set}
    else:
        out["settings"] = s
    return out


@tool()
def project_snapshot(name: str | None = None, with_stills_and_luts: bool = True) -> dict:
    """Save and export the current project to a .drp file (backup before risky edits).
    Restore later with project_import. Files go to ~/Documents/ResolveMCP/snapshots."""
    m = pm()
    p = project()
    m.SaveProject()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    fname = f"{p.GetName()}_{name + '_' if name else ''}{stamp}.drp".replace("/", "-")
    path = WORK_DIR / "snapshots" / fname
    if not m.ExportProject(p.GetName(), str(path), with_stills_and_luts):
        fail("ExportProject failed")
    return {"snapshot": str(path)}


@tool()
def project_import(path: str, name: str | None = None, open: bool = True, archive: bool = False) -> dict:
    """Import a .drp project file (or restore a .dra archive with archive=true), optionally opening it.
    Use a new `name` to restore a snapshot next to the original."""
    m = pm()
    ok = (m.RestoreProject(path, name) if name else m.RestoreProject(path)) if archive else \
        (m.ImportProject(path, name) if name else m.ImportProject(path))
    if not ok:
        fail(f"Could not import {path} (a project with that name may already exist — pass a new name)")
    if open:
        import os
        nm = name or os.path.splitext(os.path.basename(path))[0]
        if m.GetCurrentProject():
            m.SaveProject()
        if not m.LoadProject(nm):
            return {"imported": True, "opened": False, "note": f"Imported; open it with project_open (name may differ from '{nm}')"}
    return {"imported": True, **status()}


@tool()
def project_export(path: str, name: str | None = None, archive: bool = False, archive_media: bool = True,
                   with_stills_and_luts: bool = True) -> dict:
    """Export a project to a .drp file, or archive it (.dra folder, with source media) when archive=true."""
    m = pm()
    nm = name or project().GetName()
    m.SaveProject()
    ok = m.ArchiveProject(nm, path, archive_media, True, False) if archive else m.ExportProject(nm, path, with_stills_and_luts)
    if not ok:
        fail("Export failed")
    return {"exported": nm, "path": path}


@tool()
def project_manage(action: str, name: str | None = None, new_name: str | None = None, folder: str | None = None,
                   notes: str | None = None) -> dict:
    """Project manager actions: 'rename' (current project → new_name), 'create_folder' (name, inside `folder`),
    'delete' (project `name`; must not be open — irreversible), 'close' (close current project WITHOUT saving),
    'databases' (list databases)."""
    m = pm()
    a = action.lower()
    if a == "rename":
        if not project().SetName(new_name or ""):
            fail("Rename failed (name must be unique)")
        return {"renamed": new_name}
    if a == "create_folder":
        _goto_folder(folder)
        return {"created": bool(m.CreateFolder(name)), "folder": name}
    if a == "delete":
        _goto_folder(folder)
        cur = m.GetCurrentProject()
        if cur and cur.GetName() == name:
            fail("Cannot delete the open project; open another project first.")
        return {"deleted": bool(m.DeleteProject(name)), "project": name}
    if a == "close":
        return {"closed": bool(m.CloseProject(project()))}
    if a == "databases":
        return {"current": m.GetCurrentDatabase(), "databases": m.GetDatabaseList()}
    fail("action must be rename | create_folder | delete | close | databases")
    return {}


@tool()
def presets(kind: str, action: str = "list", name: str | None = None, path: str | None = None) -> dict:
    """Manage application presets. kind: layout | keyboard | burn_in | project_settings | render | user_prefs | fairlight.
    action: list | load | save | update | delete | import | export (not every kind supports every action).
    'fairlight' + 'load' applies a Fairlight preset to the current timeline."""
    r = resolve()
    k, a = kind.lower(), action.lower()
    p = pm().GetCurrentProject()
    table = {
        "layout": dict(list=r.GetLayoutPresetList, load=r.LoadLayoutPreset, save=r.SaveLayoutPreset, update=r.UpdateLayoutPreset,
                       delete=r.DeleteLayoutPreset, export=lambda n: r.ExportLayoutPreset(n, path),
                       **{"import": lambda n: r.ImportLayoutPreset(path, n) if n else r.ImportLayoutPreset(path)}),
        "keyboard": dict(list=lambda: {"current": r.GetCurrentKeyboardPreset(), "presets": r.GetKeyboardPresetList()},
                         load=r.LoadKeyboardPreset, delete=r.DeleteKeyboardPreset, export=lambda n: r.ExportKeyboardPreset(n, path),
                         **{"import": lambda n: r.ImportKeyboardPreset(path, n) if n else r.ImportKeyboardPreset(path)}),
        "burn_in": dict(list=r.GetBurnInPresetList, load=lambda n: p.LoadBurnInPreset(n), delete=r.DeleteBurnInPreset,
                        export=lambda n: r.ExportBurnInPreset(n, path), **{"import": lambda n: r.ImportBurnInPreset(path)}),
        "user_prefs": dict(list=r.GetUserPreferencesPresetList, load=r.LoadUserPreferencesPreset, save=r.SaveUserPreferencesPreset,
                           delete=r.DeleteUserPreferencesPreset, export=lambda n: r.ExportUserPreferencesPreset(n, path),
                           **{"import": lambda n: r.ImportUserPreferencesPreset(path, n) if n else r.ImportUserPreferencesPreset(path)}),
        "fairlight": dict(list=r.GetFairlightPresets, load=lambda n: p.ApplyFairlightPresetToCurrentTimeline(n)),
    }
    if p:
        table["project_settings"] = dict(
            list=p.GetProjectSettingsPresetList, load=p.SetProjectSettingsPreset, save=p.SaveCurrentProjectSettingsAsNewPreset,
            update=p.UpdateProjectSettingsPreset, delete=p.DeleteProjectSettingsPreset,
            export=lambda n: p.ExportProjectSettingsPreset(n, path),
            **{"import": lambda n: p.ImportProjectSettingsPreset(path, n) if n else p.ImportProjectSettingsPreset(path)})
        table["render"] = dict(list=p.GetRenderPresetList, load=p.LoadRenderPreset, save=p.SaveAsNewRenderPreset,
                               update=p.UpdateRenderPreset, delete=p.DeleteRenderPreset,
                               export=lambda n: r.ExportRenderPreset(n, path), **{"import": lambda n: r.ImportRenderPreset(path)})
    if k not in table:
        fail(f"kind must be one of {list(table)}")
    fn = table[k].get(a)
    if not fn:
        fail(f"{k} supports actions {list(table[k])}")
    if a == "list":
        return {"kind": k, "presets": fn()}
    if a in ("export", "import") and not path:
        fail("path is required")
    res = fn(name)
    if res is False or res is None:
        fail(f"{k} {a} failed")
    return {"kind": k, "action": a, "name": name, "ok": True}


@tool()
def playhead(time: Any = None) -> dict:
    """Get the playhead, or move it to `time` (seconds from timeline start, '120f' or timecode)."""
    tl = timeline()
    T = TL(tl)
    if time is not None:
        f = T.frame(time)
        if not tl.SetCurrentTimecode(T.tc(f)):
            fail("Could not move playhead (the current page may not support it; try page('edit'))")
    from .core import tc_to_frames
    tc = tl.GetCurrentTimecode()
    f = tc_to_frames(tc, T.fps, T.drop)
    item = safe(tl.GetCurrentVideoItem)
    return {"tc": tc, "time": T.sec(f), "frame": T.rel(f), "video_clip": item.GetName() if item else None}
