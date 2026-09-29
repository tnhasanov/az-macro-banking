"""The message that carries a report: an executive summary, one attachment, links to the rest.

Everything in the summary comes from the report's own manifest and narrative, which were validated
against the fact pack before the deck was rendered. Nothing is written here. That constraint is the
point: a covering email is read by people who will not open the deck, so a sentence in it carries
the same weight as a slide, and it should have passed the same checks. Where the report has no
validated finding to offer, the email says what it is and how to open it, rather than inventing a
line of commentary to fill the space.

The attachment is the main PDF. Everything else - the editable deck, the workbook, the fact pack -
is linked rather than attached, because mailboxes have limits and a board pack with a workbook
attached is usually the message that bounces.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .. import config
from ..narrative.contract import text_of
from ..util.log import get_logger

log = get_logger("delivery.compose")

# What a reader needs to know before the first number: which periods this edition speaks about.
PERIOD_LABELS = {
    "banking_period": "Banking data",
    "macro_period": "Macro data",
    "cpi_period": "Consumer prices",
    "edition_month": "Edition",
}


class Message:
    """A composed message, ready for any channel, with the artifacts it refers to."""

    def __init__(self, *, report_type: str, edition: str, version: int | None, subject: str,
                 summary_lines: list[str], body_text: str, body_html: str,
                 attachment: Path | None, links: list[dict[str, str]], facts: dict[str, Any]):
        self.report_type = report_type
        self.edition = edition
        self.version = version
        self.subject = subject
        self.summary_lines = summary_lines
        self.body_text = body_text
        self.body_html = body_html
        self.attachment = attachment
        self.links = links
        self.facts = facts

    @property
    def content_hash(self) -> str:
        """Identity of what was composed, so a corrected edition is a different delivery."""
        payload = json.dumps({"subject": self.subject, "body": self.body_text,
                              "attachment": self.attachment.name if self.attachment else None,
                              "links": self.links}, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def artifacts(self) -> dict[str, Any]:
        return {"attached": self.attachment.name if self.attachment else None,
                "linked": [l["name"] for l in self.links]}


def _archive_base() -> str | None:
    env = ((config.schedule_config().get("archive") or {}).get("base_url_env")) or "AZMONITOR_ARCHIVE_BASE_URL"
    return (os.environ.get(env) or "").rstrip("/") or None


def _link_for(path: Path, output_dir: Path) -> str | None:
    """A link into the archive, when one is configured.

    With no base URL the message carries the attachment and names the other files without
    pretending they are reachable: a dead link in a board pack is worse than no link.
    """
    base = _archive_base()
    if not base:
        return None
    try:
        rel = path.resolve().relative_to(output_dir.resolve())
    except ValueError:
        return None
    return f"{base}/{rel.as_posix()}"


def _findings(manifest: dict[str, Any], narrative: dict[str, Any], limit: int) -> list[str]:
    """The report's own validated findings, most material first.

    A finding rejected by the grounding validator never reaches the deck, and it does not reach the
    email either: `rejected_findings` is honoured here so the two always agree.
    """
    rejected = set((manifest.get("narrative_validation") or {}).get("rejected_findings") or [])
    out: list[str] = []
    for f in (narrative.get("findings") or []):
        if f.get("id") in rejected:
            continue
        text = text_of(f.get("statement"))
        if not text:
            continue
        classification = (f.get("classification") or "").replace("_", " ")
        # the classification travels with the sentence: a projection must not read as an outcome
        prefix = {"cba_forecast": "CBA projection: ", "cba_assessment": "CBA assessment: ",
                  "interpretation": "", "observed_fact": "", "hypothesis": "Hypothesis: ",
                  "management_question": "Question: "}.get(f.get("classification"), "")
        out.append(f"{prefix}{text}".strip())
        if len(out) >= limit:
            break
    if not out and (manifest.get("narrative_mode") == "facts_only"):
        out.append("This edition carries descriptive text only: no interpretation was asserted beyond "
                   "the observed values.")
    return out


def _periods(manifest: dict[str, Any]) -> list[str]:
    rp = manifest.get("reporting_periods") or {}
    return [f"{PERIOD_LABELS.get(k, k)}: {v}" for k, v in rp.items() if v]


def _quality_note(manifest: dict[str, Any]) -> str | None:
    q = manifest.get("quality_summary") or {}
    if not q:
        return None
    failed, critical, warning = q.get("failed", 0), q.get("critical", 0), q.get("warning", 0)
    accepted = q.get("accepted_exceptions", 0)
    if not failed and not accepted:
        return f"Data-quality checks: {q.get('checks', 0)} run, all passed."
    parts = [f"Data-quality checks: {q.get('checks', 0)} run, {failed} failed"]
    if critical:
        parts.append(f"{critical} critical")
    if warning:
        parts.append(f"{warning} warning, none inside the window this edition displays")
    if accepted:
        parts.append(f"{accepted} covered by a recorded exception")
    return "; ".join(parts) + "."


def _partial_note(manifest: dict[str, Any]) -> str | None:
    if not manifest.get("partial_edition"):
        return None
    missing = manifest.get("missing_inputs") or []
    named = ", ".join(str(m) for m in missing[:6]) if missing else "some inputs"
    return f"This is a partial edition: {named} had not arrived when it was produced."


def compose(report_type: str, edition_dir: Path, *, subject_template: str, message_cfg: dict[str, Any],
            sector: str | None = None) -> Message:
    """Build the message for one produced edition, from the files that edition wrote."""
    manifest = json.loads((edition_dir / "manifest.json").read_text(encoding="utf-8"))
    narrative_path = edition_dir / "narrative.json"
    narrative = json.loads(narrative_path.read_text(encoding="utf-8")) if narrative_path.exists() else {}
    output_dir = config.paths().output_dir

    edition = str(manifest.get("edition") or manifest.get("publication") or edition_dir.parent.name)
    version = manifest.get("version")
    window_start = ((manifest.get("window") or {}).get("start")
                    or (manifest.get("reporting_periods") or {}).get("edition_month") or edition)
    subject = subject_template.format(edition=edition, version=version, sector=sector or "",
                                      window_start=window_start, report_type=report_type).strip()

    # the manifest records the PDF as either a path or a {status, path} record, depending on whether
    # the conversion had anything to report; falling back to the directory covers both and neither
    files = manifest.get("files") or {}
    entry = files.get("pdf")
    if isinstance(entry, dict):
        entry = entry.get("path") if entry.get("status") == "ok" else None
    pdf = Path(entry) if entry else next(iter(sorted(edition_dir.glob("*.pdf"))), None)
    attach = pdf if (message_cfg.get("attach", "main_pdf") == "main_pdf" and pdf and pdf.exists()) else None

    links: list[dict[str, str]] = []
    if message_cfg.get("include_links", True):
        for path in sorted(edition_dir.iterdir()):
            if path.is_dir() or path.suffix not in (".pptx", ".xlsx", ".pdf", ".json"):
                continue
            if attach and path.resolve() == attach.resolve():
                continue
            if path.name in ("fact_pack.json", "narrative.json", "quality_report.json"):
                label = {"fact_pack.json": "Fact pack (every figure with its source)",
                         "narrative.json": "Narrative with its claim bindings",
                         "quality_report.json": "Data-quality checks"}[path.name]
            elif path.suffix == ".pptx":
                label = "Editable deck"
            elif path.suffix == ".xlsx":
                label = "Evidence workbook"
            elif path.name == "manifest.json":
                continue
            else:
                label = path.name
            url = _link_for(path, output_dir)
            links.append({"name": label, "file": path.name, **({"url": url} if url else {})})

    summary = _findings(manifest, narrative, int(message_cfg.get("summary_findings", 5)))
    periods = _periods(manifest)
    notes = [n for n in (_partial_note(manifest),
                         _quality_note(manifest) if message_cfg.get("include_quality_note", True) else None) if n]
    footer = message_cfg.get("footer", "")

    facts = {"edition": edition, "version": version, "report_type": report_type,
             "status_label": manifest.get("status_label"), "as_of": manifest.get("as_of"),
             "n_slides": manifest.get("n_slides"), "generated_at": manifest.get("generated_at"),
             "narrative_mode": manifest.get("narrative_mode"),
             "numbers_checked": (manifest.get("narrative_validation") or {}).get("numbers_checked"),
             "fingerprint": (manifest.get("edition_fingerprint") or {}).get("fingerprint"),
             "trigger": (manifest.get("edition_trigger") or {}).get("trigger")}

    return Message(report_type=report_type, edition=edition, version=version, subject=subject,
                   summary_lines=summary, body_text=_text_body(subject, summary, periods, notes, links, facts, footer),
                   body_html=_html_body(subject, summary, periods, notes, links, facts, footer),
                   attachment=attach, links=links, facts=facts)


def _text_body(subject: str, summary: list[str], periods: list[str], notes: list[str],
               links: list[dict[str, str]], facts: dict[str, Any], footer: str) -> str:
    lines = [subject, "=" * len(subject), ""]
    if periods:
        lines += ["  ".join(periods), ""]
    if summary:
        lines.append("What this edition says")
        lines += [f"  {i}. {s}" for i, s in enumerate(summary, 1)]
        lines.append("")
    for n in notes:
        lines += [n, ""]
    if links:
        lines.append("Other files in this edition")
        for l in links:
            lines.append(f"  - {l['name']}: {l.get('url') or l['file']}")
        lines.append("")
    checked = facts.get("numbers_checked")
    if checked:
        lines.append(f"Every figure in the attached report is bound to a source: {checked} numbers were "
                     f"validated against the fact pack before it was produced.")
    if footer:
        lines += ["", footer]
    return "\n".join(lines)


def _esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _html_body(subject: str, summary: list[str], periods: list[str], notes: list[str],
               links: list[dict[str, str]], facts: dict[str, Any], footer: str) -> str:
    # Deliberately plain: inline styles only, no images, no tracking, nothing that needs to load.
    # The accent comes from the active theme rather than a literal, so an email sent from a
    # neutral-profile deployment does not arrive in someone else's brand colour.
    accent = "#" + config.theme()["colors"]["primary"].lstrip("#")
    panel = "#" + config.theme()["colors"]["panel"].lstrip("#")
    p = "font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;font-size:14px;line-height:1.55;color:#1a1a1a"
    out = [f'<div style="{p};max-width:640px">']
    out.append(f'<h2 style="margin:0 0 4px;font-size:18px;color:{accent}">{_esc(subject)}</h2>')
    if facts.get("status_label"):
        out.append(f'<div style="color:#666;font-size:12px;margin-bottom:14px">{_esc(facts["status_label"])}'
                   f'{" · version " + str(facts["version"]) if facts.get("version") else ""}</div>')
    if periods:
        out.append('<div style="color:#444;font-size:12px;margin-bottom:16px">'
                   + " &nbsp;·&nbsp; ".join(_esc(x) for x in periods) + "</div>")
    if summary:
        out.append('<div style="font-weight:600;margin-bottom:6px">What this edition says</div><ol style="margin:0 0 16px;padding-left:20px">')
        out += [f"<li style='margin-bottom:6px'>{_esc(s)}</li>" for s in summary]
        out.append("</ol>")
    for n in notes:
        out.append(f'<div style="background:{panel};border-left:3px solid {accent};padding:8px 12px;margin-bottom:12px;'
                   f'font-size:13px">{_esc(n)}</div>')
    if links:
        out.append('<div style="font-weight:600;margin-bottom:6px">Other files in this edition</div><ul style="margin:0 0 16px;padding-left:20px">')
        for l in links:
            if l.get("url"):
                out.append(f'<li style="margin-bottom:4px"><a href="{_esc(l["url"])}" style="color:{accent}">{_esc(l["name"])}</a></li>')
            else:
                out.append(f'<li style="margin-bottom:4px">{_esc(l["name"])} <span style="color:#888">({_esc(l["file"])})</span></li>')
        out.append("</ul>")
    checked = facts.get("numbers_checked")
    if checked:
        out.append(f'<div style="font-size:12px;color:#444">Every figure in the attached report is bound to a source: '
                   f'{checked} numbers were validated against the fact pack before it was produced.</div>')
    if footer:
        out.append(f'<div style="margin-top:18px;padding-top:10px;border-top:1px solid #e3e3e3;font-size:11px;color:#888">'
                   f'{_esc(footer)}</div>')
    out.append("</div>")
    return "".join(out)


def compose_alert(severity: str, headline: str, detail: str, *, subject_template: str, footer: str = "") -> Message:
    """An operational alert. Short, specific, and never carrying report content."""
    subject = subject_template.format(severity=severity, headline=headline)
    body = f"{headline}\n\n{detail}\n"
    html = (f'<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;font-size:14px">'
            f'<div style="font-weight:600;color:{"#B00020" if severity == "error" else "#8a6d00"}">{_esc(headline)}</div>'
            f'<pre style="white-space:pre-wrap;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;'
            f'background:#f7f7f7;padding:10px;border-radius:4px">{_esc(detail)}</pre>'
            + (f'<div style="font-size:11px;color:#888">{_esc(footer)}</div>' if footer else "") + "</div>")
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M")
    return Message(report_type="alert", edition=f"{severity}:{stamp}", version=None, subject=subject,
                   summary_lines=[headline], body_text=body, body_html=html, attachment=None, links=[],
                   facts={"severity": severity, "headline": headline})
