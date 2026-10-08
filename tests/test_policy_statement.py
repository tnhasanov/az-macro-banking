"""A CBA decision carries its own figures from the day it is announced.

The rate, floor and ceiling used to come only from the decision table printed in the next Monetary
Policy Review, so the newest decision had no figures for weeks — the August 2026 deck showed the
23 September decision as "n/a" while quoting the statement that gave them. And the action was read
from the wording, so "the floor was reduced" made a held rate a "cut". The sentences below are the
CBA's own, in both languages.
"""
import datetime as dt

import pytest

from azmonitor.publications import policy as pol
from azmonitor.publications.factpack import PublicationFacts
from azmonitor.storage.db import Database

EN = {
    "floor cut, rate held": ("23 September 2026, Baku: The Management Board of the Central Bank of the Republic of Azerbaijan decided "
                             "to keep unchanged the refinancing rate at 6.5% and the ceiling at 7.5%, while the floor of the interest "
                             "rate corridor was reduced by 0.5 pp to 5%. The decision was based on the dynamics of actual and "
                             "forecasted inflation.", (6.5, 5.0, 7.5)),
    "all lowered": ("4 February 2026: The Management Board of the Central Bank of the Republic of Azerbaijan has decided to lower all "
                    "parameters of the interest rate corridor by 0.25 percentage points - the refinancing rate has been reduced to "
                    "6.5%, the floor of the interest rate corridor to 5.5%, and the ceiling to 7.5%.", (6.5, 5.5, 7.5)),
    "two cut, floor held": ("1 May 2024, Baku: The Management Board of the Central Bank of the Republic of Azerbaijan decided to "
                            "decrease the refinancing rate to 7.25% from 7.5%, the ceiling of the interest rate corridor to 8.25% "
                            "from 8.5% and leave the floor of the interest rate corridor unchanged at 6.25%.", (7.25, 6.25, 8.25)),
    "from-to order": ("18 September 2020, Baku: The Management Board of the Central Bank of the Republic of Azerbaijan decided to "
                      "shift the refinancing rate from 6.75% to 6.5%. The floor of the interest rate corridor was set at 6%, while "
                      "the ceiling at 7%.", (6.5, 6.0, 7.0)),
    "steps written in pp": ("3 May 2023, Baku: The Management Board of the Central Bank of the Republic of Azerbaijan decided to "
                            "increase the refinancing rate by 0.25 pp to 9%, the floor of the interest rate corridor by 0.5 pp to "
                            "7.5% and the ceiling by 0.25 pp to 10%.", (9.0, 7.5, 10.0)),
    "a range, not levels": ("31 January 2020, Baku: The Management Board of the Central Bank of the Republic of Azerbaijan decided "
                            "to reduce the refinancing rate by 25 basis points to 7.25% from 7.5%. The ceiling and the floor of the "
                            "interest rate corridor were set at ±1.75% range to the refinancing rate (9% and 5.5%) .", (7.25, None, None)),
}
AZ = {
    "floor cut, rate held": ("23 sentyabr 2026-cı il, Bakı: Azərbaycan Respublikası Mərkəzi Bankının İdarə Heyətinin qərarı ilə uçot "
                             "dərəcəsi 6.5%, yuxarı həddi isə 7.5% səviyyəsində sabit saxlanılıb. Faiz dəhlizinin aşağı həddi isə 0.5 "
                             "faiz bəndi azaldılaraq 5% səviyyəsində müəyyən edilib.", (6.5, 5.0, 7.5)),
    "from-to in the dative": ("1 may 2024-cü il: Azərbaycan Respublikası Mərkəzi Bankının İdarə Heyəti uçot dərəcəsinin 7.5%-dən "
                              "7.25%-ə, faiz dəhlizinin yuxarı həddinin 8.5%-dən 8.25%-ə endirilməsi, faiz dəhlizinin aşağı həddinin "
                              "isə 6.25% səviyyəsində dəyişməz saxlanılması haqqında qərar qəbul etmişdir.", (7.25, 6.25, 8.25)),
    "a step with a percent sign": ("16 dekabr 2022-ci il, Bakı: Azərbaycan Respublikası Mərkəzi Bankının İdarə Heyətinin qərarı ilə uçot "
                                   "dərəcəsi 0.25% bəndi artırılaraq 8.25%-ə, faiz dəhlizinin aşağı həddi 1.25 faiz bəndi artırılaraq "
                                   "6.25%-ə qaldırılmış, faiz dəhlizinin yuxarı həddi 9.25% səviyyəsində dəyişməz saxlanılmışdır.",
                                   (8.25, 6.25, 9.25)),
    "two parameters in one clause": ("19 mart 2020-ci il, Bakı: Azərbaycan Respublikası Mərkəzi Bankının İdarə Heyəti uçot dərəcəsinin "
                                     "7.25% səviyyəsində dəyişməz saxlanılması haqqında qərar qəbul etmişdir. Faiz dəhlizinin yuxarı "
                                     "həddi 9% saxlanılmaqla aşağı həddi 6.75%-ə qaldırılmışdır.", (7.25, 6.75, 9.0)),
}


@pytest.mark.parametrize("case", sorted(EN))
def test_the_corridor_is_read_from_an_english_statement(case):
    text, (rate, floor, ceiling) = EN[case]
    got = pol.parse_corridor(text, "en")
    assert (got["rate"], got["floor"], got["ceiling"]) == (rate, floor, ceiling)
    assert got["all_unchanged"] is False


@pytest.mark.parametrize("case", sorted(AZ))
def test_the_corridor_is_read_from_an_azerbaijani_statement(case):
    text, (rate, floor, ceiling) = AZ[case]
    got = pol.parse_corridor(text, "az")
    assert (got["rate"], got["floor"], got["ceiling"]) == (rate, floor, ceiling)


def test_a_statement_that_keeps_everything_unchanged_states_no_figures():
    for text, lang in (("31 July 2026, Baku: The Management Board of the Central Bank of the Republic of Azerbaijan decided to keep "
                        "all parameters of the interest rate corridor unchanged. The decision was made considering inflation.", "en"),
                       ("31 iyul 2026-cı il, Bakı: Azərbaycan Respublikası Mərkəzi Bankının İdarə Heyətinin qərarı ilə faiz "
                        "dəhlizinin bütün parametrləri dəyişməz saxlanılıb.", "az")):
        got = pol.parse_corridor(text, lang)
        assert got["all_unchanged"] and got["rate"] is None and got["floor"] is None


def test_the_effective_date_is_read_when_the_decision_will_take_effect():
    passages = [{"passage_id": "p1", "kind": "text", "text": EN["floor cut, rate held"][0]},
                {"passage_id": "p2", "kind": "text", "text": "This decision will take effect as of 24 September 2026."}]
    rel = pol.parse_press_release(passages, "en", dt.date(2026, 9, 23))
    assert rel.effective_date == dt.date(2026, 9, 24)
    assert (rel.rate, rel.floor, rel.ceiling) == (6.5, 5.0, 7.5)


def _decision(day, rate=None, floor=None, ceiling=None, action=None, text=""):
    return {"decision_id": f"decision:{day}", "announcement_date": day, "policy_rate": rate, "corridor_floor": floor,
            "corridor_ceiling": ceiling, "action": action, "rationale_text": text, "rationale_language": "en",
            "validation_status": "verified"}


def test_a_held_rate_with_a_lower_floor_is_a_hold_and_says_what_moved(tmp_path):
    db = Database(tmp_path / "d.sqlite")
    db.upsert_decision(_decision("2026-07-31", 6.5, 5.5, 7.5, "hold"))
    db.upsert_decision(_decision("2026-09-23", 6.5, 5.0, 7.5, "cut", EN["floor cut, rate held"][0]))
    last = PublicationFacts(db, dt.date(2026, 10, 9)).decisions()[-1]
    assert last["action"] == "hold" and last["action_wording"] == "cut"
    assert last["rate_change_bp"] == 0 and last["floor_change_bp"] == -50 and last["ceiling_change_bp"] == 0
    assert last["corridor_change"] == "floor cut 50 bp"


def test_a_statement_keeping_all_parameters_carries_the_previous_levels_forward(tmp_path):
    db = Database(tmp_path / "d.sqlite")
    db.upsert_decision(_decision("2026-06-24", 6.5, 5.5, 7.5, "hold"))
    db.upsert_decision(_decision("2026-07-31", None, None, None, "hold",
                                 "The Management Board ... decided to keep all parameters of the interest rate corridor unchanged."))
    last = PublicationFacts(db, dt.date(2026, 10, 9)).decisions()[-1]
    assert (last["policy_rate"], last["corridor_floor"], last["corridor_ceiling"]) == (6.5, 5.5, 7.5)
    assert "2026-06-24" in last["rate_basis"] and last["action"] == "hold"


def test_two_versions_of_a_decision_within_one_second_do_not_collide(tmp_path):
    db = Database(tmp_path / "d.sqlite")
    db.upsert_decision(_decision("2026-09-23"))
    db.upsert_decision(_decision("2026-09-23", 6.5, 5.0, 7.5))
    db.upsert_decision(_decision("2026-09-23", 6.5, 5.0, 7.5) | {"effective_date": "2026-09-24"})
    rows = db.conn.execute("SELECT decision_id, status FROM policy_decisions ORDER BY decision_id").fetchall()
    assert [r["status"] for r in rows].count("current") == 1 and len(rows) == 3


def test_the_wording_of_the_action_alone_is_not_a_revision(tmp_path):
    db = Database(tmp_path / "d.sqlite")
    db.upsert_decision(_decision("2026-09-23", 6.5, 5.0, 7.5, "cut"))
    assert db.upsert_decision(_decision("2026-09-23", 6.5, 5.0, 7.5, "hold")) == "unchanged"


def test_the_rationale_comparison_drops_the_formula_every_statement_opens_with():
    from azmonitor.facts import _rationale_difference
    held = ("31 July 2026, Baku: The Management Board of the Central Bank of the Republic of Azerbaijan decided to keep all "
            "parameters of the interest rate corridor unchanged.")
    previous, current = _rationale_difference(held, EN["floor cut, rate held"][0])
    assert previous == "Keep all parameters of the interest rate corridor unchanged."
    assert current == ("Keep unchanged the refinancing rate at 6.5% and the ceiling at 7.5%, while the floor of the interest rate "
                       "corridor was reduced by 0.5 pp to 5%.")
    # a dateline without the city is dropped with the formula
    _, lowered = _rationale_difference(held, EN["all lowered"][0])
    assert lowered.startswith("Lower all parameters of the interest rate corridor by 0.25 percentage points")
