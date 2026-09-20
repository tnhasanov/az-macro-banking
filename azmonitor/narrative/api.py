"""Optional API-backed narrative provider. Enabled only when credentials are configured.

The provider and model are configurable (settings.narrative.api). No model id is hardcoded and
no paid call is required to build, test or produce a facts-only report. The prompt sends the
fact pack (facts and approved numbers) and requests the narrative JSON contract; the response
is validated by validate.py like any other narrative source.
"""
from __future__ import annotations

import json
import os
from typing import Any

from .. import config
from .contract import empty_narrative

SYSTEM = (
    "You are a senior economic and banking analyst writing for a bank CRO and Management Board. "
    "You receive a fact pack with validated official statistics (CBA, SSC). Write direct, bounded statements. "
    "Use ONLY numbers present in the fact pack's approved_numbers (copy them exactly, one decimal). Never compute new numbers. "
    "Classify each finding as observed_fact, interpretation, hypothesis or management_question. "
    "Do not assert causes, forecasts, default probabilities or lending recommendations. Do not imply knowledge of any bank's internal data. "
    "Return only JSON matching the contract."
)


def available() -> tuple[bool, str]:
    cfg = config.settings().get("narrative", {}).get("api", {})
    provider = cfg.get("provider", "anthropic")
    model = os.environ.get(cfg.get("model_env", "AZMONITOR_NARRATIVE_MODEL"), "")
    key = os.environ.get("ANTHROPIC_API_KEY") if provider == "anthropic" else None
    if not key:
        return False, f"no credentials for provider {provider}"
    if not model:
        return False, f"model id not set ({cfg.get('model_env')})"
    return True, f"{provider}:{model}"


def generate(fp: dict[str, Any], lang: str = "en") -> tuple[dict[str, Any], dict[str, Any]]:
    """Returns (narrative, usage). Raises RuntimeError when the provider is unavailable."""
    ok, why = available()
    if not ok:
        raise RuntimeError(why)
    cfg = config.settings().get("narrative", {}).get("api", {})
    model = os.environ[cfg.get("model_env", "AZMONITOR_NARRATIVE_MODEL")]
    try:
        import anthropic  # type: ignore
    except ImportError as exc:
        raise RuntimeError("anthropic SDK not installed (pip install .[api])") from exc
    client = anthropic.Anthropic()
    contract = open(os.path.join(os.path.dirname(__file__), "contract.py"), encoding="utf-8").read().split('"""')[1]
    compact = {k: fp[k] for k in ("report_type", "as_of", "edition", "slides", "approved_numbers", "flags", "availability") if k in fp}
    compact["metrics"] = {k: {kk: v[kk] for kk in ("label", "unit", "period_type", "latest", "prior", "change", "basis") if kk in v} for k, v in fp.get("metrics", {}).items()}
    msg = client.messages.create(
        model=model, max_tokens=int(cfg.get("max_tokens", 4000)), system=SYSTEM,
        messages=[{"role": "user", "content": f"Language: {lang}. Narrative contract:\n{contract}\n\nFact pack:\n{json.dumps(compact, ensure_ascii=False, default=str)}"}],
    )
    text = "".join(getattr(b, "text", "") for b in msg.content)
    start, end = text.find("{"), text.rfind("}")
    nar = json.loads(text[start:end + 1]) if start >= 0 else empty_narrative(fp, "api")
    nar["mode"] = "api"
    nar["fact_pack_hash"] = fp.get("fact_pack_hash")
    usage = {"model": model, "input_tokens": getattr(msg.usage, "input_tokens", None), "output_tokens": getattr(msg.usage, "output_tokens", None)}
    return nar, usage
