"""Pure bisection over an ordered batch of branches. Not yet wired into the gate (SPEC.md section 5).

`is_failing(subset)` must be monotone: a superset of a failing subset also fails."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field


@dataclass
class BisectionResult:
    candidates: list[str]
    safe: list[str] = field(default_factory=list)      # proven safe, in discovery order
    offenders: list[str] = field(default_factory=list)  # isolated single offenders
    unresolved: list[str] = field(default_factory=list)  # neither proven safe nor isolated
    tests: int = 0


def bisect_branches(branches: Sequence[str], is_failing: Callable[[list[str]], bool]) -> BisectionResult:
    res = BisectionResult(candidates=list(branches))
    if not branches:
        return res
    res.tests += 1
    if not is_failing(list(branches)):
        res.safe = list(branches)
        return res

    def recurse(cand: list[str]) -> None:
        if len(cand) == 1:
            res.offenders.append(cand[0])
            return
        mid = (len(cand) + 1) // 2
        first, second = cand[:mid], cand[mid:]
        res.tests += 2
        f1, f2 = is_failing(first), is_failing(second)
        if f1 and f2:
            recurse(first)
            recurse(second)
        elif f1:
            res.safe.extend(second)
            recurse(first)
        elif f2:
            res.safe.extend(first)
            recurse(second)
        else:
            # Only the combination fails: cannot attribute to a single branch.
            res.unresolved.extend(cand)

    recurse(list(branches))
    return res
