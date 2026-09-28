"""Narrative layer: facts-only fallback, analyst/Claude-authored JSON, optional API provider.

The narrative never computes numbers. Every number in generated text must match an approved
number in the fact pack (see validate.py); otherwise the statement is replaced by the
facts-only description.
"""
