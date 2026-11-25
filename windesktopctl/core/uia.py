"""UI Automation tree extraction.

Two paths, both producing the same record dicts consumed by
elements.from_uia_records:

- ``walk_cached`` (default): one ``FindAllBuildCache`` COM call returns every
  control-view descendant with all needed properties cached; we filter and
  format locally. Much faster than per-element COM walks on big trees.
- ``_walk_tree``: the original uiautomation BFS walker, kept as fallback and
  selectable via ``WINDESKTOPCTL_UIA=walk|cached``.

Runs on the core worker thread (COM initialized there).
"""

from __future__ import annotations

import os
import time
from collections import deque

from windesktopctl.core import WindesktopctlError
from windesktopctl.core import elements as el

MAX_DEPTH = 25
TIME_BUDGET = 3.0  # seconds, wall clock (walker path only)
MAX_VISITED = 6000
MAX_CACHED = 40000  # flat cached enumeration safety cap

INTERACTIVE = el.INTERACTIVE_TYPES

DEFAULT_MODE = "cached"

# Non-interactive container roles worth a ScrollPattern probe in the walker.
_SCROLL_ROLES = frozenset(
    {
        "Pane",
        "Group",
        "Custom",
        "Window",
        "Tab",
        "Menu",
        "ToolBar",
        "StatusBar",
        "TitleBar",
        "Table",
        "DataGrid",
        "Header",
    }
)

_CACHED_PROP_NAMES = (
    "NameProperty",
    "ControlTypeProperty",
    "BoundingRectangleProperty",
    "IsOffscreenProperty",
    "IsEnabledProperty",
    "HasKeyboardFocusProperty",
    "IsPasswordProperty",
    "AutomationIdProperty",
    "ValueValueProperty",
    "ToggleToggleStateProperty",
    "ExpandCollapseExpandCollapseStateProperty",
    "SelectionItemIsSelectedProperty",
    "ScrollVerticallyScrollableProperty",
    "ScrollVerticalScrollPercentProperty",
    "ScrollHorizontallyScrollableProperty",
    "ScrollHorizontalScrollPercentProperty",
)

_CT_NAMES: dict[int, str] | None = None


def _mode() -> tuple[str, bool]:
    """(mode, forced). WINDESKTOPCTL_UIA=walk|cached forces; otherwise default."""
    raw = os.environ.get("WINDESKTOPCTL_UIA", "").strip().lower()
    if raw in ("walk", "cached"):
        return raw, True
    return DEFAULT_MODE, False


# ------------------------------------------------------------ shared helpers --

def _role_name(ctrl) -> str:
    try:
        name = ctrl.ControlTypeName  # e.g. "ButtonControl"
    except Exception:
        return ""
    if name.endswith("Control"):
        name = name[: -len("Control")]
    return name


def _rect_of(ctrl) -> tuple[int, int, int, int] | None:
    try:
        r = ctrl.BoundingRectangle
        x, y = int(r.left), int(r.top)
        w, h = int(r.right - r.left), int(r.bottom - r.top)
        return (x, y, w, h)
    except Exception:
        return None


def _intersects(rect, win_rect) -> bool:
    x, y, w, h = rect
    wx, wy, ww, wh = win_rect
    return x < wx + ww and x + w > wx and y < wy + wh and y + h > wy


def _name_of(ctrl) -> str:
    try:
        return ctrl.Name or ""
    except Exception:
        return ""


def _value_of(ctrl, is_password: bool) -> str:
    if is_password:
        return ""
    try:
        pattern = ctrl.GetPattern(10002)  # UIA_ValuePatternId
        if pattern is not None:
            v = pattern.Value
            return v if isinstance(v, str) else str(v)
    except Exception:
        pass
    return ""


def _silence_uiautomation(auto) -> None:
    """Keep uiautomation quiet: no @AutomationLog.txt, no stdout prints
    (critical when stdout is the MCP stdio channel; also keeps CLI output
    clean)."""
    try:
        auto.Logger.SetLogFile("")

        def _noop(*_a, **_k):
            return None

        auto.Logger.Write = staticmethod(_noop)
    except Exception:
        pass


def _scroll_flags(pct_v, pct_h, scroll_v: bool, scroll_h: bool) -> set[str]:
    flags: set[str] = set()
    if scroll_v:
        flags.add("v=" + (str(int(round(pct_v))) if pct_v is not None and pct_v >= 0 else "?") + "%")
    if scroll_h:
        flags.add("h=" + (str(int(round(pct_h))) if pct_h is not None and pct_h >= 0 else "?") + "%")
    return flags


def _scroll_name(name: str, automation_id: str, role: str) -> str:
    n = (name or "").strip()
    if n:
        return n
    if automation_id:
        return automation_id
    return f"scrollable {role.lower()}" if role else "scrollable area"


# ------------------------------------------------------- cached COM path --

def _core_uia():
    """(UIAutomationCore typelib module, IUIAutomation object)."""
    from uiautomation.uiautomation import _AutomationClient

    inst = _AutomationClient.instance()
    return inst.UIAutomationCore, inst.IUIAutomation


def _ct_names(core) -> dict[int, str]:
    """UIA control-type id -> short role name ('Button', 'Edit', ...)."""
    global _CT_NAMES
    if _CT_NAMES is None:
        m: dict[int, str] = {}
        for n in dir(core):
            if n.startswith("UIA_") and n.endswith("ControlTypeId"):
                m[int(getattr(core, n))] = n[4 : -len("ControlTypeId")]
        _CT_NAMES = m
    return _CT_NAMES


def _make_cache_request(uia, core):
    import uiautomation as auto

    req = uia.CreateCacheRequest()
    P = auto.PropertyId
    for pname in _CACHED_PROP_NAMES:
        req.AddProperty(getattr(P, pname))
    # Cache ONLY each returned element - TreeScope_Descendants here would make
    # every element cache its whole subtree (huge, and cached reads then fail).
    req.TreeScope = core.TreeScope_Element
    return req


def _dom_root_element(uia, core, root):
    """Find the web-content subtree root: AutomationId=='RootWebArea'
    (Chromium family) else the first Document control (Firefox)."""
    import uiautomation as auto

    P = auto.PropertyId
    for prop, val in (
        (P.AutomationIdProperty, "RootWebArea"),
        (P.ControlTypeProperty, int(getattr(core, "UIA_DocumentControlTypeId", 50030))),
    ):
        try:
            cond = uia.CreatePropertyCondition(prop, val)
            el = root.FindFirst(core.TreeScope_Descendants, cond)
            if el is not None:
                return el
        except Exception:
            continue
    return None


def _record_from_cached(e, P, ct_names, window_rect) -> dict | None:
    """Build one record dict from a cached IUIAutomationElement, or None."""
    try:
        ct = e.GetCachedPropertyValue(P.ControlTypeProperty)
        role = ct_names.get(int(ct), "")
        if e.GetCachedPropertyValue(P.IsOffscreenProperty):
            return None
        br = e.GetCachedPropertyValue(P.BoundingRectangleProperty)
        x, y, w, h = int(br[0]), int(br[1]), int(br[2]), int(br[3])
        rect = (x, y, w, h)
        if w <= 0 or h <= 0 or not _intersects(rect, window_rect):
            return None

        scroll_v = bool(e.GetCachedPropertyValue(P.ScrollVerticallyScrollableProperty))
        scroll_h = bool(e.GetCachedPropertyValue(P.ScrollHorizontallyScrollableProperty))
        name = e.GetCachedPropertyValue(P.NameProperty) or ""
        aid = ""

        kept_role = role in INTERACTIVE or role == "Text"
        if kept_role and el.should_keep(role, name):
            flags: set[str] = set()
            if e.GetCachedPropertyValue(P.HasKeyboardFocusProperty):
                flags.add("focused")
            if not e.GetCachedPropertyValue(P.IsEnabledProperty):
                flags.add("disabled")
            is_pw = bool(e.GetCachedPropertyValue(P.IsPasswordProperty))
            if is_pw:
                flags.add("password")
            if e.GetCachedPropertyValue(P.ToggleToggleStateProperty) == 1:
                flags.add("checked")
            ec = e.GetCachedPropertyValue(P.ExpandCollapseExpandCollapseStateProperty)
            if ec == 1:
                flags.add("expanded")
            elif ec == 0:
                flags.add("collapsed")
            if e.GetCachedPropertyValue(P.SelectionItemIsSelectedProperty):
                flags.add("selected")
            value = ""
            if not is_pw:
                try:
                    value = e.GetCachedPropertyValue(P.ValueValueProperty) or ""
                except Exception:
                    value = ""
            return {"role": role, "name": name, "value": value, "rect": rect, "flags": flags}

        # Scrollable non-interactive container -> synthetic Scroll element.
        if (scroll_v or scroll_h) and not kept_role:
            try:
                aid = e.GetCachedPropertyValue(P.AutomationIdProperty) or ""
            except Exception:
                aid = ""
            pct_v = pct_h = None
            try:
                if scroll_v:
                    pct_v = e.GetCachedPropertyValue(P.ScrollVerticalScrollPercentProperty)
                if scroll_h:
                    pct_h = e.GetCachedPropertyValue(P.ScrollHorizontalScrollPercentProperty)
            except Exception:
                pass
            return {
                "role": "Scroll",
                "name": _scroll_name(name, aid, role),
                "value": "",
                "rect": rect,
                "flags": _scroll_flags(pct_v, pct_h, scroll_v, scroll_h),
            }
    except Exception:
        return None
    return None


def walk_cached(
    hwnd: int, window_rect: tuple[int, int, int, int], dom: bool = False
) -> tuple[list[dict], bool]:
    """Flat cached enumeration of a window's control-view descendants.

    Returns (records, truncated). ``truncated`` is True only when the tree
    exceeded MAX_CACHED (the COM call itself is atomic).
    """
    import uiautomation as auto

    _silence_uiautomation(auto)
    core, uia = _core_uia()
    P = auto.PropertyId
    ct_names = _ct_names(core)

    root = uia.ElementFromHandle(int(hwnd))
    if root is None:
        raise WindesktopctlError(f"UIA: cannot get element for hwnd {hwnd}")

    scope_root = root
    if dom:
        scope_root = _dom_root_element(uia, core, root)
        if scope_root is None:
            raise WindesktopctlError("dom mode: web content (RootWebArea/Document) not found")

    req = _make_cache_request(uia, core)
    arr = scope_root.FindAllBuildCache(core.TreeScope_Descendants, uia.ControlViewCondition, req)
    total = int(arr.Length)
    truncated = total > MAX_CACHED

    records: list[dict] = []
    for i in range(min(total, MAX_CACHED)):
        try:
            e = arr.GetElement(i)
        except Exception:
            continue
        rec = _record_from_cached(e, P, ct_names, window_rect)
        if rec is not None:
            records.append(rec)
    return records, truncated


# ---------------------------------------------------------- walker fallback --

def _control_flags(ctrl) -> set[str]:
    flags: set[str] = set()
    try:
        if ctrl.HasKeyboardFocus:
            flags.add("focused")
    except Exception:
        pass
    try:
        if not ctrl.IsEnabled:
            flags.add("disabled")
    except Exception:
        pass
    try:
        if ctrl.IsPassword:
            flags.add("password")
    except Exception:
        pass
    try:
        tp = ctrl.GetPattern(10015)  # UIA_TogglePatternId
        if tp is not None and tp.ToggleState == 1:  # ToggleState.On
            flags.add("checked")
    except Exception:
        pass
    try:
        ep = ctrl.GetPattern(10005)  # UIA_ExpandCollapsePatternId
        if ep is not None:
            s = ep.ExpandCollapseState
            if s == 1:  # Expanded
                flags.add("expanded")
            elif s == 0:  # Collapsed
                flags.add("collapsed")
    except Exception:
        pass
    try:
        sp = ctrl.GetPattern(10010)  # UIA_SelectionItemPatternId
        if sp is not None and sp.IsSelected:
            flags.add("selected")
    except Exception:
        pass
    return flags


def _scroll_record(ctrl, role: str, name: str, rect) -> dict | None:
    try:
        sp = ctrl.GetPattern(10004)  # UIA_ScrollPatternId
    except Exception:
        return None
    if sp is None:
        return None
    try:
        vv = bool(sp.VerticallyScrollable)
        vh = bool(sp.HorizontallyScrollable)
    except Exception:
        return None
    if not (vv or vh):
        return None
    try:
        aid = ctrl.AutomationId or ""
    except Exception:
        aid = ""
    flags = _scroll_flags(
        _safe_float(getattr(sp, "VerticalScrollPercent", None)),
        _safe_float(getattr(sp, "HorizontalScrollPercent", None)),
        vv,
        vh,
    )
    return {
        "role": "Scroll",
        "name": _scroll_name(name, aid, role),
        "value": "",
        "rect": rect,
        "flags": flags,
    }


def _safe_float(v):
    try:
        return float(v)
    except Exception:
        return None


def _dom_control_root(hwnd: int):
    """Locate the DOM subtree root as a uiautomation Control (for _walk_tree)."""
    import uiautomation as auto

    _silence_uiautomation(auto)
    core, uia = _core_uia()
    raw_root = uia.ElementFromHandle(int(hwnd))
    if raw_root is None:
        return None
    for prop, val in (
        (auto.PropertyId.AutomationIdProperty, "RootWebArea"),
        (auto.PropertyId.ControlTypeProperty, int(getattr(core, "UIA_DocumentControlTypeId", 50030))),
    ):
        try:
            cond = uia.CreatePropertyCondition(prop, val)
            el = raw_root.FindFirst(core.TreeScope_Descendants, cond)
        except Exception:
            el = None
        if el is not None:
            return auto.Control.CreateControlFromElement(el)
    return None


def _walk_tree(
    hwnd: int, window_rect: tuple[int, int, int, int], dom: bool = False
) -> tuple[list[dict], bool]:
    """BFS the UIA tree via the uiautomation package (fallback path).

    Skips offscreen elements (and their subtrees), empty/off-window rects,
    and non-kept roles. Stops early on the time/node budget -> truncated.
    """
    import uiautomation as auto

    _silence_uiautomation(auto)
    root = auto.ControlFromHandle(int(hwnd))
    if root is None:
        raise WindesktopctlError(f"UIA: cannot get control for hwnd {hwnd}")

    if dom:
        dom_root = _dom_control_root(hwnd)
        if dom_root is None:
            raise WindesktopctlError("dom mode: web content (RootWebArea/Document) not found")
        root = dom_root

    records: list[dict] = []
    visited = 0
    truncated = False
    deadline = time.monotonic() + TIME_BUDGET

    queue: deque = deque([(root, 0)])
    while queue:
        if (visited & 31) == 0 and time.monotonic() > deadline:
            truncated = True
            break
        if visited >= MAX_VISITED:
            truncated = True
            break

        ctrl, depth = queue.popleft()
        visited += 1

        try:
            offscreen = bool(ctrl.IsOffscreen)
        except Exception:
            offscreen = False

        # Offscreen subtrees are clipped out of view; skipping them keeps the
        # budget for visible content.
        if not offscreen:
            rect = _rect_of(ctrl)
            if rect is not None and rect[2] > 0 and rect[3] > 0 and _intersects(rect, window_rect):
                role = _role_name(ctrl)
                if role in INTERACTIVE or role == "Text":
                    name = _name_of(ctrl)
                    if el.should_keep(role, name):
                        try:
                            is_pw = bool(ctrl.IsPassword)
                        except Exception:
                            is_pw = False
                        records.append(
                            {
                                "role": role,
                                "name": name,
                                "value": _value_of(ctrl, is_pw),
                                "rect": rect,
                                "flags": _control_flags(ctrl),
                            }
                        )
                elif role in _SCROLL_ROLES:
                    name = _name_of(ctrl)
                    srec = _scroll_record(ctrl, role, name, rect)
                    if srec is not None:
                        records.append(srec)

            if depth < MAX_DEPTH:
                try:
                    children = ctrl.GetChildren()
                except Exception:
                    children = []
                for child in children or []:
                    queue.append((child, depth + 1))
        # offscreen: do not descend

    return records, truncated


def walk(
    hwnd: int, window_rect: tuple[int, int, int, int], dom: bool = False
) -> tuple[list[dict], bool]:
    """Dispatch to the configured UIA path (WINDESKTOPCTL_UIA=walk|cached)."""
    mode, forced = _mode()
    if mode == "walk":
        return _walk_tree(hwnd, window_rect, dom=dom)
    try:
        return walk_cached(hwnd, window_rect, dom=dom)
    except Exception as ex:
        if forced:
            if isinstance(ex, WindesktopctlError):
                raise
            raise WindesktopctlError(f"cached UIA failed: {ex}") from ex
        # auto mode: fall back to the walker
        return _walk_tree(hwnd, window_rect, dom=dom)


# ------------------------------------------------------- browser helpers --

_DOM_TEXT_PROP_NAMES = (
    "NameProperty",
    "ControlTypeProperty",
    "LocalizedControlTypeProperty",
    "AriaRoleProperty",
    "ValueValueProperty",
    "IsPasswordProperty",
)


def dom_text_records(hwnd: int) -> list[dict]:
    """Every descendant of the DOM root (RootWebArea/Document) in document
    order, INCLUDING offscreen elements - feeds `browser text`.

    Records: {"role", "name", "value", "localized", "aria"}. Password
    values are never read. No rect/offscreen filtering: the point is to
    read the whole page without scrolling.
    """
    import uiautomation as auto

    _silence_uiautomation(auto)
    core, uia = _core_uia()
    P = auto.PropertyId
    ct_names = _ct_names(core)

    root = uia.ElementFromHandle(int(hwnd))
    if root is None:
        raise WindesktopctlError(f"UIA: cannot get element for hwnd {hwnd}")
    scope_root = _dom_root_element(uia, core, root)
    if scope_root is None:
        raise WindesktopctlError("dom mode: web content (RootWebArea/Document) not found")

    req = uia.CreateCacheRequest()
    for pname in _DOM_TEXT_PROP_NAMES:
        req.AddProperty(getattr(P, pname))
    req.TreeScope = core.TreeScope_Element

    try:
        condition = uia.RawViewCondition
    except Exception:
        condition = uia.CreateTrueCondition()
    arr = scope_root.FindAllBuildCache(core.TreeScope_Descendants, condition, req)
    total = int(arr.Length)

    records: list[dict] = []
    for i in range(min(total, MAX_CACHED)):
        try:
            e = arr.GetElement(i)
        except Exception:
            continue
        try:
            ct = e.GetCachedPropertyValue(P.ControlTypeProperty)
            role = ct_names.get(int(ct), "")
            name = e.GetCachedPropertyValue(P.NameProperty) or ""
            try:
                localized = e.GetCachedPropertyValue(P.LocalizedControlTypeProperty) or ""
            except Exception:
                localized = ""
            try:
                aria = e.GetCachedPropertyValue(P.AriaRoleProperty) or ""
            except Exception:
                aria = ""
            is_pw = bool(e.GetCachedPropertyValue(P.IsPasswordProperty))
            value = ""
            if not is_pw:
                try:
                    value = e.GetCachedPropertyValue(P.ValueValueProperty) or ""
                except Exception:
                    value = ""
            records.append(
                {
                    "role": role,
                    "name": name,
                    "value": value,
                    "localized": localized,
                    "aria": aria,
                }
            )
        except Exception:
            continue
    return records


def dom_root_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """Bounding rect of the DOM root (RootWebArea/Document), or None."""
    core, uia = _core_uia()
    root = uia.ElementFromHandle(int(hwnd))
    if root is None:
        return None
    el = _dom_root_element(uia, core, root)
    if el is None:
        return None
    try:
        br = el.CurrentBoundingRectangle
        x, y = int(br.left), int(br.top)
        w, h = int(br.right - br.left), int(br.bottom - br.top)
        if w <= 0 or h <= 0:
            return None
        return (x, y, w, h)
    except Exception:
        return None


TAB_STRIP_DEPTH = 140  # px below window top that counts as the tab strip


def tab_controls(hwnd: int, window_rect: tuple[int, int, int, int]) -> list[dict]:
    """TabItem controls in the browser tab strip, in document order.

    Returns [{"name", "rect", "selected", "control"}] - ``control`` is the
    live uiautomation Control so callers can try SelectionItemPattern.Select
    or fall back to clicking its center. Tab strip = TabItems whose top
    edge sits within TAB_STRIP_DEPTH px of the window top (in-page tab
    lists sit lower and are ignored).
    """
    import uiautomation as auto

    _silence_uiautomation(auto)
    root = auto.ControlFromHandle(int(hwnd))
    if root is None:
        raise WindesktopctlError(f"UIA: cannot get control for hwnd {hwnd}")

    strip_bottom = window_rect[1] + TAB_STRIP_DEPTH
    out: list[dict] = []
    visited = 0
    deadline = time.monotonic() + TIME_BUDGET
    queue: deque = deque([(root, 0)])
    while queue:
        if (visited & 31) == 0 and time.monotonic() > deadline:
            break
        if visited >= MAX_VISITED:
            break
        ctrl, depth = queue.popleft()
        visited += 1
        try:
            is_tab = ctrl.ControlTypeName == "TabItemControl"
        except Exception:
            is_tab = False
        if is_tab:
            r = _rect_of(ctrl)
            if r is not None and r[2] > 0 and r[3] > 0 and r[1] < strip_bottom:
                selected = False
                try:
                    sp = ctrl.GetPattern(10010)  # UIA_SelectionItemPatternId
                    if sp is not None and sp.IsSelected:
                        selected = True
                except Exception:
                    pass
                out.append(
                    {
                        "name": _name_of(ctrl),
                        "rect": r,
                        "selected": selected,
                        "control": ctrl,
                    }
                )
        if depth < MAX_DEPTH:
            try:
                children = ctrl.GetChildren()
            except Exception:
                children = []
            for child in children or []:
                queue.append((child, depth + 1))
    return out
