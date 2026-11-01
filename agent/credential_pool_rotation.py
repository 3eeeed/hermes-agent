"""Rotation onto deferred single-use-token candidates (openai-codex, xai-oauth).

``mark_exhausted_and_rotate`` selects under the pool lock with ``refresh=True``; for
single-use-token providers every candidate whose access token aged out comes back as
``pending_refresh`` instead of being refreshed under the lock. Handing such an entry back
unrefreshed made the very next request die with ``401 token_expired`` on a healthy account,
benching it and advancing the lap onto the next stale one.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from agent.credential_pool import CredentialPool, PooledCredential

logger = logging.getLogger(__name__)


def refresh_first_usable(
    pool: "CredentialPool", pending: "list[PooledCredential]", *, failed_id: str,
) -> "Optional[PooledCredential]":
    """Refresh deferred candidates ONE AT A TIME in priority order; return the first usable.

    Refreshing every deferred entry up front would spend healthy accounts' single-use
    refresh tokens on a rotation that only needed one. A candidate whose refresh fails
    (dead grant, network) is benched so the same lap does not re-offer it. Runs outside
    the pool lock: each refresh takes the cross-process auth-store lock and does I/O.
    """
    from agent.credential_pool import STATUS_DEAD

    for candidate in pending:
        if candidate.id == failed_id:
            continue
        if pool._refresh_entry(candidate, force=False) is not None:
            with pool._lock:
                next_entry, _ = pool._select_unlocked(refresh=False)
            if next_entry is not None and next_entry.id != failed_id:
                logger.info("credential pool: rotated to %s", next_entry.label or next_entry.id[:8])
                return next_entry
            continue
        with pool._lock:
            stale = pool._find(lambda e, cid=candidate.id: e.id == cid)
            if stale is not None and stale.last_status != STATUS_DEAD:
                pool._mark_exhausted(stale, None, failure_reason="refresh_failed")
    return None
