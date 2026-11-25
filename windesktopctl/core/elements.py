"""Element model, filtering, dedup, compact text formatting, token estimate.

Pure logic - no desktop interaction, unit-testable.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

MAX_TEXT = 80  # name/value truncation length

# Categories stripped from element text: control (Cc), format (Cf) and
# private-use (Co) chars - icon-font glyphs are meaningless to an agent and
# can crash cp1252 consoles.
_STRIP_CATEGORIES = frozenset({"Cc", "Cf", "Co"})

# UIA control types (ControlTypeName minus the "Control" suffix) kept always.
# "Scroll" is windesktopctl's own role for vertically/horizontally scrollable
# containers that are not otherwise interactive.
INTERACTIVE_TYPES = frozenset(
    {
        "Button",
        "Edit",
        "CheckBox",
        "RadioButton",
        "ComboBox",
        "MenuItem",
        "ListItem",
        "TabItem",
        "Hyperlink",
        "TreeItem",
        "Slider",
        "SplitButton",
        "DataItem",
        "Spinner",
        "Document",
        "HeaderItem",
        "Scroll",
    }
)

# Element state flags, in display order. Scroll containers carry "v=NN%" /
# "h=NN%" flags instead (appended after these).
FLAG_ORDER = (
    "focused",
    "disabled",
    "checked",
    "expanded",
    "collapsed",
    "selected",
    "password",
)

_WS_RE = re.compile(r"\s+")


def clean_text(s: str | None, limit: int = MAX_TEXT) -> str:
    """Collapse whitespace, strip, drop control/format/private-use chars,
    truncate to ~limit chars with an ellipsis."""
    if s is None:
        return ""
    s = _WS_RE.sub(" ", str(s))
    s = "".join(ch for ch in s if unicodedata.category(ch) not in _STRIP_CATEGORIES)
    s = s.strip()
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


def escape_text(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def should_keep(role: str, name: str) -> bool:
    """Keep interactive types always; keep Text only if it has a name."""
    if role in INTERACTIVE_TYPES:
        return True
    if role == "Text":
        return bool((name or "").strip())
    return False


@dataclass
class Element:
    role: str
    name: str = ""
    value: str = ""
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)  # x, y, w, h (screen px)
    source: str = "uia"  # "uia" | "ocr"
    id: int = 0
    flags: frozenset = frozenset()  # see FLAG_ORDER + "v=NN%"/"h=NN%"

    @property
    def cx(self) -> int:
        return self.rect[0] + self.rect[2] // 2

    @property
    def cy(self) -> int:
        return self.rect[1] + self.rect[3] // 2

    def center(self) -> tuple[int, int]:
        return self.cx, self.cy

    @property
    def is_password(self) -> bool:
        return "password" in self.flags

    def effective_value(self) -> str:
        """Value safe to display: always empty for password fields."""
        return "" if self.is_password else self.value

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "role": self.role,
            "name": self.name,
            "value": self.effective_value(),
            "rect": list(self.rect),
            "source": self.source,
            "flags": sorted(self.flags),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Element":
        return cls(
            role=d.get("role", ""),
            name=d.get("name", ""),
            value=d.get("value", ""),
            rect=tuple(d.get("rect", [0, 0, 0, 0])),
            source=d.get("source", "uia"),
            id=int(d.get("id", 0)),
            flags=frozenset(d.get("flags") or ()),
        )


def dedup_key(e: Element) -> tuple:
    return (e.role, e.name, e.rect)


def dedup(elements: list[Element]) -> list[Element]:
    """Remove duplicates with identical (role, name, rect), keeping order."""
    seen: set[tuple] = set()
    out: list[Element] = []
    for e in elements:
        k = dedup_key(e)
        if k in seen:
            continue
        seen.add(k)
        out.append(e)
    return out


def from_uia_records(records: list[dict]) -> list[Element]:
    """Convert raw UIA records ({role,name,value,rect,flags}) to Elements,
    cleaning text and de-duplicating. Password values are never kept."""
    elems = []
    for r in records:
        role = r.get("role", "")
        name = clean_text(r.get("name", ""))
        flags = frozenset(r.get("flags") or ())
        value = "" if "password" in flags else clean_text(r.get("value", ""))
        rect = tuple(int(v) for v in r.get("rect", (0, 0, 0, 0)))
        elems.append(
            Element(role=role, name=name, value=value, rect=rect, source="uia", flags=flags)
        )
    return dedup(elems)


def cap(elements: list[Element], max_elements: int) -> tuple[list[Element], int]:
    """Keep the first max_elements, assign ids 1..N. Returns (kept, dropped)."""
    kept = elements[:max_elements]
    for i, e in enumerate(kept, 1):
        e.id = i
    return kept, max(0, len(elements) - len(kept))


def format_flags(flags) -> str:
    """Canonical flag ordering: FLAG_ORDER first, then scroll percents."""
    fs = set(flags or ())
    ordered = [f for f in FLAG_ORDER if f in fs]
    ordered += sorted(f for f in fs if f not in FLAG_ORDER)
    return " ".join(ordered)


def format_element_line(e: Element) -> str:
    line = f'[{e.id}] {e.role} "{escape_text(e.name)}"'
    value = e.effective_value()
    if value:
        line += f' ="{escape_text(value)}"'
    flags = format_flags(e.flags)
    if flags:
        line += f" {flags}"
    line += f" @{e.cx},{e.cy}"
    return line


def format_observe(
    *,
    title: str,
    proc: str,
    hwnd: int,
    rect: tuple[int, int, int, int],
    source: str,
    changed: bool,
    elements: list[Element],
    truncated_count: int = 0,
    walk_truncated: bool = False,
) -> str:
    """Compact observe output, e.g.:

    window: "Untitled - Notepad" notepad.exe hwnd=1234 rect=0,0,1920,1040 source=uia changed=true ~tokens=310
    [1] Button "File" @45,20
    [2] Edit "Text editor" ="hello…" @960,500
    """
    lines = [format_element_line(e) for e in elements]
    if not lines:
        lines = ["(no elements found)"]
    if truncated_count > 0:
        lines.append(f"(truncated: {truncated_count} more)")
    elif walk_truncated:
        lines.append("(truncated: enumeration stopped early)")
    body = "\n".join(lines)

    head = (
        f'window: "{escape_text(title)}" {proc or "-"} hwnd={hwnd} '
        f"rect={rect[0]},{rect[1]},{rect[2]},{rect[3]} "
        f"source={source} changed={str(changed).lower()}"
    )
    # ~tokens estimate: len(text)/4 over the whole returned string.
    est = (len(head) + 1 + len(body)) // 4
    return head + f" ~tokens={est}\n" + body


def estimate_tokens(text: str) -> int:
    return len(text) // 4
