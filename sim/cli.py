"""`tasking <role>`: run one member of the simulated team.

    tasking planner | qa | release
    tasking worker <n> | reviewer <n>
    tasking status          the tracker's counts (read-only)
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    a = sys.argv[1:]
    if a[:1] in (["worker"], ["reviewer"]) and len(a) == 2 and a[1].isdigit():
        mod = __import__(f"sim.{a[0]}", fromlist=["main"])
        mod.main(int(a[1]))
        return 0
    if a == ["status"]:
        from .bd_client import Beads
        print(json.dumps(Beads("status").stats().get("summary", {}), indent=1))
        return 0
    if len(a) != 1 or a[0] not in ("planner", "qa", "release"):
        print(__doc__.strip())
        return 2
    __import__(f"sim.{a[0]}", fromlist=["main"]).main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
