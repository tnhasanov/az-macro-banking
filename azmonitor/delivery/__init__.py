"""Delivering a finished report to the people who asked for it.

Four pieces:

* `records`  - the ledger of what was sent, to whom, and what the provider said
* `compose`  - the message itself, built only from what the report already validated
* `providers`- Microsoft Graph for email, an official provider for WhatsApp notifications
* `dispatch` - putting the three together under the retry rules

Nothing here writes new analysis. The executive summary in an email is the report's own findings,
which were validated against the fact pack before the deck was rendered; if a sentence was not good
enough for the deck it is not good enough for the covering email either.
"""
from __future__ import annotations

from .records import DeliveryLedger, delivery_id, mask

__all__ = ["DeliveryLedger", "delivery_id", "mask"]
