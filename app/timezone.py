"""Shared timezone constants.

Single source of truth for WITA (Waktu Indonesia Tengah, UTC+8) used across
routers, ML pipeline, scheduler, and seed scripts. Importing the same
timezone object means a future change (e.g. DST rule or operator override)
touches exactly one place — previously there were 8 separate literal copies
of `timedelta(hours=8)` or `timezone(timedelta(hours=8))` scattered across
the codebase, which drifted at runtime as imports loaded in different orders.
"""
from datetime import timezone, timedelta

# WITA = UTC+8 (Waktu Indonesia Tengah). Used for day-boundary math, daily
# report windows, and cron schedule anchoring.
WITA = timezone(timedelta(hours=8))

__all__ = ["WITA"]
