"""A simulated team working through tickets in Beads (bd), fast enough to produce data.

The team knows nothing about Orchestra. It only talks to the tracker, Beads, through ``bd serve``'s
HTTP API (and the ``bd`` command for leases). Orchestra must learn about this system from the
outside, as it would about any real one."""
