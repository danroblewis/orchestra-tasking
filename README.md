# orchestra-tasking

A task system, built here, that Orchestra observes. Being specified.

Beads was tried first and removed (2026-10-25): in Beads, agents pick their own work from its
`ready` queue, and its scheduling happens inside it, out of Orchestra's sight. The history is in git.

    bin/orchestra-ctl start          # Orchestra's host and web, on http://127.0.0.1:8060
