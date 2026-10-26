"""The store's errors."""

from __future__ import annotations


class StoreError(Exception):
    pass


class Conflict(StoreError):
    pass
