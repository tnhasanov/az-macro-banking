---
description: Refresh the sources and draft a validated monthly commentary for the Azerbaijan Macro & Banking Monitor
---

Draft this month's commentary for the monitor. No model API key is involved: you are the analyst,
and everything you write is checked against the fact pack before it is rendered.

## 1. Refresh and build the request

```bash
python -m azmonitor.cli refresh
python -m azmonitor.cli validate
python -m azmonitor.cli commentary-pack
```

The last command prints the path of a commentary request under `data/state/`. Read it. It contains:

- `reporting_periods` and `anchors` — the banking month, the macro period and the CPI month, which
  differ and must each be named where they are used;
- `claim_catalogue` — every number you may cite, each with the `claim_id` that grounds it;
- `quotable_passages` — the Central Bank's own sentences, with the page each appears on;
- `availability` and `quality` — what is missing and what did not reconcile;
- `policy` and `stability` — the latest decision, projection round and stability report.

## 2. Write the narrative

Write `narratives/monthly_<edition>_analyst.json` following `azmonitor/narrative/contract.py`:

- `fact_pack_hash` must be the hash printed by the command. A narrative written against an earlier
  fact pack is rejected in full rather than partly reused.
- Every block is `{"text": ..., "claims": [...], "literals": [...], "quotes": [...]}`.
- Every number in `text` must be covered by a `claim_id` from the catalogue. A number that is not a
  measurement (a table number, a count of findings) goes in `literals` with a reason.
- A claim binds metric, dimensions, period, unit and comparison basis. Writing a year-on-year rate as
  a level, or a percentage-point move as a percentage, fails validation.
- Anything you attribute to the Central Bank must quote a passage id and be classified
  `cba_assessment` (its view) or `cba_forecast` (its projection). Quote the words exactly.
- Keep observed fact, the Bank's assessment, its projection and your own interpretation in separate
  findings with the right classification.
- Say nothing about this bank's own exposures, vulnerabilities or compliance: none of these sources
  contains bank-level data. Implications are conditional and are labelled as such.
- Where the evidence does not support a statement, write what is missing instead.

Aim for: a cover headline, up to five ranked findings, slide texts where you have something to add
beyond the numbers, and three management questions. Slides you leave out are filled from the
facts-only generator and labelled as such.

`python -m azmonitor.cli bind-claims --narrative <path>` proposes claim ids for numbers you have
already written and lists the ambiguous and unsupported ones for you to resolve.

## 3. Validate and render

```bash
python -m azmonitor.cli report --type monthly --narrative-file narratives/monthly_<edition>_analyst.json
```

Read `narrative.json` in the new edition directory. `validation.problems` must be empty. Each problem
names the block, the number and what is wrong: an unbound number, a unit that does not match the
claim, a direction word that contradicts the sign, a quotation that is not in the cited passage.
Fix the narrative and run again. Anything still failing falls back to facts-only text for that item
and is recorded in the manifest, so a partly-rejected narrative is visible rather than silent.

## 4. Briefs

If a policy review, stability report or rate decision was released, produce its brief too:

```bash
python -m azmonitor.cli report --type mpr-brief
python -m azmonitor.cli report --type fsr-brief
python -m azmonitor.cli report --type decision-update
```

## 5. Report back

State the edition, the reporting periods, the validation result, what was excluded and why, and the
paths of the generated files.
