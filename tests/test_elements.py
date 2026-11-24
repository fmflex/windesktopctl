"""Unit tests for elements.py - pure logic, no desktop interaction."""

from deskctl.core import elements as el


def test_should_keep_interactive_types():
    for role in (
        "Button", "Edit", "CheckBox", "RadioButton", "ComboBox", "MenuItem",
        "ListItem", "TabItem", "Hyperlink", "TreeItem", "Slider",
        "SplitButton", "DataItem", "Spinner", "Document",
    ):
        assert el.should_keep(role, ""), role
        assert el.should_keep(role, "x"), role


def test_should_keep_text_only_with_name():
    assert el.should_keep("Text", "hello")
    assert not el.should_keep("Text", "")
    assert not el.should_keep("Text", "   ")


def test_should_keep_drops_other_types():
    for role in ("Pane", "Window", "Group", "Image", "Custom", ""):
        assert not el.should_keep(role, "some name")


def test_clean_text():
    assert el.clean_text(None) == ""
    assert el.clean_text("  a   b\n\tc  ") == "a b c"
    long = "x" * 200
    out = el.clean_text(long)
    assert len(out) <= 80
    assert out.endswith("…")
    assert el.clean_text("y" * 100, limit=10) == "y" * 9 + "…"


def test_dedup_identical_role_name_rect():
    e1 = el.Element(role="Button", name="OK", rect=(0, 0, 10, 10))
    e2 = el.Element(role="Button", name="OK", rect=(0, 0, 10, 10))
    e3 = el.Element(role="Button", name="OK", rect=(1, 0, 10, 10))
    e4 = el.Element(role="Button", name="Cancel", rect=(0, 0, 10, 10))
    e5 = el.Element(role="text", name="OK", rect=(0, 0, 10, 10), source="ocr")
    out = el.dedup([e1, e2, e3, e4, e5])
    assert out == [e1, e3, e4, e5]


def test_cap_assigns_ids_and_counts_dropped():
    elems = [el.Element(role="Button", name=f"b{i}", rect=(0, 0, 1, 1)) for i in range(5)]
    kept, dropped = el.cap(elems, 3)
    assert dropped == 2
    assert [e.id for e in kept] == [1, 2, 3]
    assert kept[0].name == "b0"


def test_center():
    e = el.Element(role="Button", name="x", rect=(40, 10, 10, 20))
    assert e.center() == (45, 20)


def test_format_observe_exact_style():
    elems = [
        el.Element(role="Button", name="File", rect=(40, 10, 10, 20)),
        el.Element(role="Edit", name="Text editor", value="hello…", rect=(900, 400, 100, 50)),
        el.Element(role="text", name="Some OCR line", rect=(300, 700, 100, 10), source="ocr"),
    ]
    for i, e in enumerate(elems, 1):
        e.id = i
    out = el.format_observe(
        title="Untitled - Notepad",
        proc="notepad.exe",
        hwnd=1234,
        rect=(0, 0, 1920, 1040),
        source="uia",
        changed=True,
        elements=elems,
    )
    lines = out.splitlines()
    assert lines[0].startswith(
        'window: "Untitled - Notepad" notepad.exe hwnd=1234 rect=0,0,1920,1040 '
        "source=uia changed=true ~tokens="
    )
    assert '[1] Button "File" @45,20' in lines
    assert '[2] Edit "Text editor" ="hello…" @950,425' in lines
    assert '[3] text "Some OCR line" @350,705' in lines
    assert "~tokens=" in lines[0]


def test_format_observe_truncated_line():
    elems = [el.Element(role="Button", name=f"b{i}", rect=(0, 0, 5, 5)) for i in range(2)]
    for i, e in enumerate(elems, 1):
        e.id = i
    out = el.format_observe(
        title="t", proc="p", hwnd=1, rect=(0, 0, 1, 1),
        source="uia", changed=False, elements=elems,
        truncated_count=7,
    )
    assert out.splitlines()[-1] == "(truncated: 7 more)"
    assert "changed=false" in out.splitlines()[0]


def test_format_observe_empty():
    out = el.format_observe(
        title="t", proc="p", hwnd=1, rect=(0, 0, 1, 1),
        source="ocr", changed=True, elements=[],
    )
    assert "(no elements found)" in out


def test_element_dict_roundtrip():
    e = el.Element(role="Edit", name="n", value="v", rect=(1, 2, 3, 4), source="ocr", id=9)
    e2 = el.Element.from_dict(e.to_dict())
    assert e2.role == "Edit" and e2.name == "n" and e2.value == "v"
    assert e2.rect == (1, 2, 3, 4) and e2.source == "ocr" and e2.id == 9
    assert e2.center() == (2, 4)


def test_format_flags_order_and_scroll():
    assert el.format_flags(frozenset({"disabled", "focused", "checked"})) == (
        "focused disabled checked"
    )
    assert el.format_flags(frozenset({"v=40%", "selected"})) == "selected v=40%"
    assert el.format_flags(frozenset()) == ""
    assert el.format_flags(None) == ""


def test_format_element_line_flags():
    e = el.Element(
        role="CheckBox", name="Word wrap", rect=(110, 290, 20, 20),
        flags=frozenset({"checked"}),
    )
    e.id = 4
    assert el.format_element_line(e) == '[4] CheckBox "Word wrap" checked @120,300'
    e2 = el.Element(
        role="Scroll", name="list", rect=(0, 0, 100, 100),
        flags=frozenset({"v=40%"}),
    )
    e2.id = 5
    assert el.format_element_line(e2) == '[5] Scroll "list" v=40% @50,50'


def test_password_value_never_emitted():
    # even if a raw record carries a value, a password-flagged element drops it
    recs = [
        {"role": "Edit", "name": "pw", "value": "hunter2", "rect": (0, 0, 10, 10),
         "flags": ["password"]},
        {"role": "Edit", "name": "user", "value": "bob", "rect": (0, 20, 10, 10),
         "flags": []},
    ]
    elems = el.from_uia_records(recs)
    pw = next(e for e in elems if e.name == "pw")
    assert pw.value == ""
    assert pw.effective_value() == ""
    assert "hunter2" not in pw.to_dict()["value"]
    assert pw.to_dict()["flags"] == ["password"]
    line = el.format_element_line(pw)
    assert "hunter2" not in line
    other = next(e for e in elems if e.name == "user")
    assert other.value == "bob"


def test_flags_roundtrip():
    e = el.Element(role="Button", name="x", rect=(0, 0, 1, 1),
                   flags=frozenset({"focused", "disabled"}))
    e.id = 1
    e2 = el.Element.from_dict(e.to_dict())
    assert e2.flags == frozenset({"focused", "disabled"})
    assert "focused" in el.format_element_line(e2)
