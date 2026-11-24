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
