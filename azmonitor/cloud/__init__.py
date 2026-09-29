"""Running the existing engine against cloud storage instead of a persistent disk.

The engine is unchanged. It still keeps its dataset in SQLite and its reports in a directory tree,
because those are the formats its safeguards are built on: append-only vintages, edition
fingerprints, immutable version directories, a delivery ledger keyed by recipient. Rewriting that
against a different store would mean re-earning every one of those guarantees.

What changes is where the directory lives between runs. The engine already had the seam for this -
`AZMONITOR_RESTORE_CMD` and `AZMONITOR_SAVE_CMD`, which run inside the job lock around the whole
cycle - so cloud persistence is a matter of supplying commands, not of changing the pipeline.

Four pieces:

* `objectstore` - pull the dataset down before a run, push it and the new reports back after
* `readmodel`   - publish what the dashboard needs into Postgres, so a web request never has to
                  open a 374 MB SQLite file
* `lock`        - a lease in Postgres, because a file lock on an ephemeral disk coordinates nothing
* `publish`     - the command-line glue the worker calls

The read model is derived and disposable: it is rebuilt from the dataset on every run, and nothing
reads back from it into the engine. That keeps one authority for every number.
"""
from __future__ import annotations

__all__ = ["objectstore", "readmodel", "lock", "publish"]
