"""Official CBA narrative publications: monetary policy reviews, policy decisions,
annual policy directions and financial stability reports.

These differ from the statistical tables in three ways that the rest of the system has to respect:

* they are *editions* (a review, a report, a decision), published in Azerbaijani and, usually,
  English. Both language files belong to one publication and must not create two release events;
* their reporting period is rarely the month the file appears in, so publication date and
  reporting period are stored separately and always displayed together;
* most of their content is prose. Numbers are admitted to the fact pack only when they were read
  from running text or a real table; a figure that exists only inside a chart image is never
  invented, and anything read from a scanned page is marked unverified until checked.
"""
from __future__ import annotations

PUB_TYPES = {
    "monetary_policy_review": "Monetary Policy Review",
    "policy_decision": "Monetary policy decision",
    "policy_directions": "Annual monetary policy statement",
    "financial_stability_report": "Financial Stability Report",
}
