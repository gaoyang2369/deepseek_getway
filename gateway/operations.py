"""Transactional idempotency and audit records shared by HTTP entry points."""

import hashlib
import json

from sqlalchemy import text

from .models import Audit, Operation
from .security import fail


def audit(db, request, action, target, detail=None, *, actor=None):
    db.add(
        Audit(
            actor=actor or request.app.state.settings.admin_name,
            action=action,
            target=target,
            detail=detail or {},
            request_id=request.state.request_id,
        )
    )


async def operation(db, operation_id, data):
    # Includes the action context; reused IDs with different payloads must fail.
    fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:id, 0))"), {"id": operation_id})
    previous = await db.get(Operation, operation_id)
    if previous and previous.fingerprint != fingerprint:
        fail(409, "idempotency_conflict")
    return previous, fingerprint
