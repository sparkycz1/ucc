"""Scheduling: run an existing action (inventory sync, firmware update, ...)
against a site, a Fabric, or "All Fabrics" on a cron-like
schedule.

See:
- `app.scheduling.actions` — the registry a schedulable action plugs into.
- `app.scheduling.builtin_actions` — what's actually registered today;
  `register_builtin_actions()` must run once before the registry is used
  (`app.main` and `app.scheduling.jobs` call it at import time, and each
  forked Celery worker child calls it again after the fork).
- `app.scheduling.cron` — cron expression validation / next-run computation.
- `app.scheduling.jobs` — the Celery tasks that evaluate and fire due
  schedules.
"""

from __future__ import annotations
