"""Compute answers.json for the challenge answer phase.

The agent (me) cannot view PNGs in this environment, so instead of eyeballing
the grid overlay we compute which labeled cells contain the target elements,
using the SAME grid geometry challenge() stores in state. This exercises the
real cells->coords->click pipeline end to end; the saved PNGs remain for
human review.

For each manifest session: focus the still-open chrome window, re-run
challenge() (fresh id + grid), observe elements, pick one cell per target,
write answers.json.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _live_browser import MANIFEST  # noqa: E402
from _live_notepad import assert_safe_target, run  # noqa: E402
from deskctl.core import api, elements as el, state as st  # noqa: E402
from deskctl.core import challenge as ch  # noqa: E402

GRID_ANSWER_TILES = [2, 7, 13]  # 0-based indices of the 🚥 tiles
TEXT_ANSWER = "K7Q2-X9"


def _cells_for_rect(grid, rect):
    """All (row,col) whose cell CENTER lies inside rect."""
    out = []
    for r in range(int(grid["rows"])):
        for c in range(int(grid["cols"])):
            x, y = ch.cell_center(grid, r, c)
            if rect[0] <= x < rect[0] + rect[2] and rect[1] <= y < rect[1] + rect[3]:
                out.append((r, c))
    return out


def _pick_cell(grid, rect):
    cells = _cells_for_rect(grid, rect)
    if not cells:
        return None
    # prefer the cell center closest to the rect center (most central click)
    rcx, rcy = rect[0] + rect[2] / 2, rect[1] + rect[3] / 2
    return min(
        cells,
        key=lambda rc: (
            (ch.cell_center(grid, *rc)[0] - rcx) ** 2
            + (ch.cell_center(grid, *rc)[1] - rcy) ** 2
        ),
    )


def _observe_elems(hwnd):
    data = run(api.observe_data, str(hwnd), "auto", 400)
    return [el.Element.from_dict(d) for d in data["elements"]]


def main() -> int:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    sessions = manifest.get("sessions", {})
    answers = {}

    for key in ("grid", "text", "checkbox"):
        sess = sessions.get(key)
        if not sess:
            print(f"skip {key}: no session in manifest")
            continue
        hwnd = int(sess["hwnd"])
        dims = sess.get("dims") or {}
        run(api.focus, str(hwnd))
        time.sleep(0.4)
        assert_safe_target(sess["token"])
        _png, info = run(api.challenge, str(hwnd), dims.get("rows"), dims.get("cols"))
        cid = int(info.split("id=")[1].split()[0])
        grid = (st.get_challenge() or {}).get("grid") or {}
        print(f"{key}: fresh challenge id={cid} grid={grid.get('rows')}x{grid.get('cols')}")

        elems = _observe_elems(hwnd)
        if key == "grid":
            picked = []
            for idx in GRID_ANSWER_TILES:
                tile = next(
                    (e for e in elems if e.role == "Button" and e.name == f"tile {idx}"),
                    None,
                )
                if tile is None:
                    print(f"  !! tile {idx} not found in observe")
                    continue
                cell = _pick_cell(grid, list(tile.rect))
                print(f"  tile {idx} rect={tile.rect} -> cell {ch.cell_label(*cell) if cell else None}")
                if cell:
                    picked.append(ch.cell_label(*cell))
            answers["grid"] = {"cells": " ".join(picked)}
        elif key == "text":
            answers["text"] = {"text": TEXT_ANSWER}
            edit = next((e for e in elems if e.role == "Edit" and e.name != "Address and search bar"), None)
            print(f"  text answer={TEXT_ANSWER!r}; page Edit={edit.name if edit else None} rect={edit.rect if edit else None}")
        elif key == "checkbox":
            cb = next((e for e in elems if e.role == "CheckBox"), None)
            if cb is None:
                print("  !! no CheckBox element found")
                continue
            # the <label> row toggles the input - expand the box rect to the
            # label row so a cell center lands on clickable text, not just
            # the 40px box
            x, y, w, h = cb.rect
            label_rect = [x - 30, y - 12, w + 340, h + 24]
            cell = _pick_cell(grid, label_rect)
            print(f"  checkbox rect={cb.rect} label~{label_rect} -> cell {ch.cell_label(*cell) if cell else None}")
            if cell:
                answers["checkbox"] = {"cells": ch.cell_label(*cell)}

    out = Path(sys.argv[1] if len(sys.argv) > 1 else "tests/answers.json")
    out.write_text(json.dumps(answers, indent=2), encoding="utf-8")
    print("wrote", out, json.dumps(answers))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
