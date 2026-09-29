"""Configuration loading: settings, sources, metrics, reports, theme, glossary.

All operating defaults live in config/*.yaml. Environment variables
AZMONITOR_DATA_DIR and AZMONITOR_OUTPUT_DIR override the persistent locations so a
cloud runner can mount storage without editing files, and AZMONITOR_PROFILE selects
a distribution profile: which theme reports are rendered with, whose name is on
them, and whether their output may be uploaded outside the organisation.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(os.environ.get("AZMONITOR_ROOT", Path(__file__).resolve().parents[1]))
CONFIG_DIR = ROOT / "config"


def load_yaml(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@dataclass(frozen=True)
class Paths:
    root: Path
    data_dir: Path
    output_dir: Path

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "monitor.sqlite"

    @property
    def snapshots_dir(self) -> Path:
        return self.data_dir / "snapshots"

    @property
    def analytics_dir(self) -> Path:
        return self.data_dir / "analytics"

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    def ensure(self) -> None:
        for p in (self.data_dir, self.raw_dir, self.snapshots_dir, self.analytics_dir, self.state_dir, self.logs_dir, self.output_dir):
            p.mkdir(parents=True, exist_ok=True)


def _resolve(root: Path, value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (root / p)


@lru_cache(maxsize=None)
def profile() -> dict[str, Any]:
    """The active distribution profile: whose identity reports carry, and where they may go.

    Selected by AZMONITOR_PROFILE, defaulting to the one named in config/distribution.yaml. An
    unknown name is an error rather than a fallback: silently rendering under the branded profile
    because a deployment misspelled "neutral" is exactly the mistake this is here to prevent.
    """
    cfg = load_yaml(CONFIG_DIR / "distribution.yaml")
    profiles = cfg.get("profiles") or {}
    name = os.environ.get("AZMONITOR_PROFILE") or cfg.get("default") or "internal"
    if name not in profiles:
        raise KeyError(
            f"unknown distribution profile {name!r}; config/distribution.yaml defines "
            f"{sorted(profiles)}"
        )
    return {"name": name, **profiles[name]}


@lru_cache(maxsize=None)
def settings() -> dict[str, Any]:
    cfg = load_yaml(CONFIG_DIR / "settings.yaml")
    cfg.setdefault("paths", {})
    # The profile owns the identity a report is published under, so it wins over the file. Applied
    # here, once, rather than at each of the places that render a footer.
    active = profile()
    report = cfg.setdefault("report", {})
    for key in ("organisation_label", "audience_label"):
        if active.get(key):
            report[key] = active[key]
    return cfg


def paths() -> Paths:
    cfg = settings()
    data_dir = os.environ.get("AZMONITOR_DATA_DIR") or cfg["paths"].get("data_dir", "data")
    output_dir = os.environ.get("AZMONITOR_OUTPUT_DIR") or cfg["paths"].get("output_dir", "outputs")
    return Paths(root=ROOT, data_dir=_resolve(ROOT, data_dir), output_dir=_resolve(ROOT, output_dir))


@lru_cache(maxsize=None)
def sources() -> dict[str, Any]:
    return load_yaml(CONFIG_DIR / "sources.yaml")


@lru_cache(maxsize=None)
def metrics_config() -> dict[str, Any]:
    return load_yaml(CONFIG_DIR / "metrics.yaml")


@lru_cache(maxsize=None)
def reports_config() -> dict[str, Any]:
    return load_yaml(CONFIG_DIR / "reports.yaml")


@lru_cache(maxsize=None)
def schedule_config() -> dict[str, Any]:
    """When runs happen and what must be true before a report is produced."""
    return load_yaml(CONFIG_DIR / "schedule.yaml")


@lru_cache(maxsize=None)
def delivery_config() -> dict[str, Any]:
    """Channels, recipients and provider settings. Credentials are never stored here."""
    return load_yaml(CONFIG_DIR / "delivery.yaml")


@lru_cache(maxsize=None)
def theme() -> dict[str, Any]:
    """The presentation theme the active profile selects.

    The profile decides, not settings.yaml, so that choosing a profile cannot leave a deployment
    with a neutral organisation label and a branded logo — a combination that would be worse than
    either alone.
    """
    return load_yaml(_resolve(ROOT, profile()["theme"]))


@lru_cache(maxsize=None)
def glossary() -> dict[str, Any]:
    return load_yaml(_resolve(ROOT, settings()["report"].get("glossary", "config/glossary.yaml")))


def term(key: str, lang: str | None = None) -> str:
    """Controlled-terminology lookup. Falls back to the key itself so a missing term is visible."""
    lang = lang or settings().get("language", "en")
    entry = glossary().get("terms", {}).get(key)
    if not entry:
        return key
    return entry.get(lang) or entry.get("en") or key


def iter_datasets():
    """Yield (source_id, source_cfg, dataset_cfg) for every configured dataset."""
    for sid, scfg in sources().get("sources", {}).items():
        for ds in scfg.get("datasets", []) or []:
            yield sid, scfg, ds


def dataset(dataset_id: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
    for sid, scfg, ds in iter_datasets():
        if ds["id"] == dataset_id:
            return sid, scfg, ds
    raise KeyError(dataset_id)
