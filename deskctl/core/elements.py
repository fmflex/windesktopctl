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
    }
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

    @property
    def cx(self) -> int:
        return self.rect[0] + self.rect[2] // 2

    @property
    def cy(self) -> int:
        return self.rect[1] + self.rect[3] // 2

    def center(self) -> tuple[int, int]:
        return self.cx, self.cy

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "role": self.role,
            "name": self.name,
            "value": self.value,
            "rect": list(self.rect),
            "source": self.source,
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
    """Convert raw UIA walk records ({role,name,value,rect}) to Elements,
    cleaning text and de-duplicating."""
    elems = []
    for r in records:
        role = r.get("role", "")
        name = clean_text(r.get("name", ""))
        value = clean_text(r.get("value", ""))
        rect = tuple(int(v) for v in r.get("rect", (0, 0, 0, 0)))
        elems.append(Element(role=role, name=name, value=value, rect=rect, source="uia"))
    return dedup(elems)


def cap(elements: list[Element], max_elements: int) -> tuple[list[Element], int]:
    """Keep the first max_elements, assign ids 1..N. Returns (kept, dropped)."""
    kept = elements[:max_elements]
    for i, e in enumerate(kept, 1):
        e.id = i
    return kept, max(0, len(elements) - len(kept))


def format_element_line(e: Element) -> str:
    line = f'[{e.id}] {e.role} "{escape_text(e.name)}"'
    if e.value:
        line += f' ="{escape_text(e.value)}"'
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
