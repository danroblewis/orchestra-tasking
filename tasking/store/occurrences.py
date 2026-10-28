"""The recurring half of the tickets record (tasking/recurring.py): per definition, the due time of
the last occurrence handled, what became of it, and the ticket the last one made. Kept in
``tickets.json`` beside the tickets, so an occurrence is checked, made and recorded in one write
under the store's lock: a restart or a second writer never makes it twice. An occurrence's
ticket is published as the planner's scheduled engagement (``planner/recurring``), with the
occurrence as its ``correlationid``."""

from __future__ import annotations

from typing import Any

from .. import measures
from ..model import Ticket, now
from .files import write


class Occurrences:
    """Part of ``LocalStore`` (store/local.py), over the tickets half's record (store/tickets.py)."""

    def _recurring(self, data: dict) -> dict:
        return data.setdefault("recurring", {})               # a record from before recurring tickets has none

    def recurring(self) -> dict[str, dict]:
        """Per definition: ``due`` (epoch seconds of the last occurrence handled, or of when it was
        first seen), ``occurrence``, ``outcome``, ``ticket`` (the last one made), ``at``, ``removed``."""
        return dict(self._t().get("recurring") or {})

    def recurring_seen(self, names: list[str], at: float) -> None:
        """The definitions in use now: one new, or back after it was removed, disabled or invalid,
        counts from ``at`` (nothing is due before it existed); one no longer in use is marked
        removed. Written only when something changed."""
        def changes(rec: dict) -> bool:
            return any(n not in rec or rec[n].get("removed") for n in names) or \
                any(not r.get("removed") for n, r in rec.items() if n not in names)
        if not changes(self._t().get("recurring") or {}):
            return
        with self._locked():
            data = self._t()
            rec = self._recurring(data)
            for n in names:
                if n not in rec or rec[n].get("removed"):
                    rec[n] = {**rec.get(n, {}), "due": at, "removed": False, "since": at}
            for n, r in rec.items():
                if n not in names:
                    r["removed"] = True
            write(self.project.tickets_file, data)

    def settle_occurrence(self, name: str, *, due: float, occurrence: str, outcome: str, missed: int = 0,
                          ticket: dict[str, Any] | None = None, src: str | None = None, late: bool = False) -> Ticket | bool:
        """The occurrence of ``name`` due at ``due`` handled, once: ``outcome`` (created, late,
        skipped, missed, failed) and the ``missed`` ones before it counted, and with ``ticket``
        (its fields) the ticket made, all in one write. False when an occurrence due at or after
        ``due`` was already handled (nothing is done); the ticket when one was made; else True."""
        with self._locked():
            data = self._t()
            rec = self._recurring(data).setdefault(name, {})
            if rec.get("due") is not None and float(rec["due"]) >= due:
                return False
            t = self._add_ticket(data, (src or "").partition("/")[0] or self.machine, {**ticket, "occurrence": occurrence}) if ticket else None
            rec.update(due=due, occurrence=occurrence, outcome=outcome, at=now(), **({"ticket": t.id} if t else {}))
            measures.count(data["metric_counts"], "recurring", {"recurring": name, "outcome": outcome})
            if missed:
                measures.count(data["metric_counts"], "recurring", {"recurring": name, "outcome": "missed"}, missed)
            write(self.project.tickets_file, data)
        if t is None:
            return True
        self.events.emit("ticket.created", src=src, correlation=occurrence, ticket=t.id, title=t.title, by=t.created_by,
                         recurring=name, occurrence=occurrence, late=late)
        return t
