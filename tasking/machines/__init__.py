"""Programs that fill a role, shipped with the task system so that any project can run them
(``python -m tasking.machines.shell_worker``). Each is a machine like any other: it imports only
Orchestra's library (``orchestra.config``, ``orchestra.events``), nothing from this package, and
speaks the contract in ``tasking/assignments.py`` on the bus. Nothing in the task system imports
or starts them; a project declares one under ``[[machines]]`` in its ``orchestra.toml``."""
