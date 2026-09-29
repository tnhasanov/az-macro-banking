"""A slide without data must say so, not take the whole deck down with it.

Found by the end-to-end run: the CBA regional tables carry one month each, so a monthly edition for
any other banking month had no regional rows, python-pptx refused the empty chart, and the entire
deck failed to render.
"""
from azmonitor import config
from azmonitor.render.builder import Deck


def test_an_empty_bar_chart_becomes_a_note():
    theme = dict(config.theme())
    theme["_root"] = str(config.ROOT)
    d = Deck(theme, "en")
    s = d.new_slide()
    before = len(s.shapes)
    assert d.add_bar_chart(s, 1, 1, 5, 3, [], [{"name": "Loan share", "values": []}]) is None
    assert d.add_bar_chart(s, 1, 1, 5, 3, ["A", "B"], [{"name": "x", "values": [None, None]}]) is None
    texts = [sh.text_frame.text for sh in list(s.shapes)[before:] if sh.has_text_frame]
    assert texts == ["No figures are available for this period."] * 2


def test_a_bar_chart_with_data_is_still_a_chart():
    theme = dict(config.theme())
    theme["_root"] = str(config.ROOT)
    d = Deck(theme, "en")
    s = d.new_slide()
    assert d.add_bar_chart(s, 1, 1, 5, 3, ["A"], [{"name": "x", "values": [1.5]}]) is not None
