"""Read-only reporting semantics shared by summaries, lists and CSV export."""

from datetime import datetime

from sqlalchemy import func, select

from ..billing import SHANGHAI
from ..models import Audit, Call
from ..security import fail


def test_team_ids():
    # Explicit project labels are versioned in the existing administrative audit log.
    # No schema or request-admission change is needed for this reporting metadata.
    latest = (
        select(Audit.target, Audit.detail)
        .where(Audit.action == "report_test_label")
        .distinct(Audit.target)
        .order_by(Audit.target, Audit.created_at.desc(), Audit.id.desc())
        .subquery()
    )
    return select(latest.c.target).where(latest.c.detail["is_test"].as_boolean().is_(True))


def time_bound(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=SHANGHAI)
    except (ValueError, OverflowError):
        fail(400, "invalid_time_range")


def report_filters(
    query,
    start=None,
    end=None,
    team_id=None,
    student_id=None,
    key_id=None,
    model=None,
    request_id=None,
    status=None,
    outcome=None,
    data_scope=None,
):
    if start and end and time_bound(start) >= time_bound(end):
        fail(400, "invalid_time_range")
    for value, column in (
        (team_id, Call.team_id),
        (student_id, Call.student_id),
        (key_id, Call.key_id),
        (model, Call.model),
        (request_id, Call.id),
        (outcome, Call.outcome),
    ):
        if value:
            query = query.where(column == value)
    if start:
        query = query.where(Call.started_at >= time_bound(start))
    if end:
        query = query.where(Call.started_at < time_bound(end))
    if status == "in_progress":
        query = query.where(Call.status.in_(("reserved", "running")))
    elif status:
        query = query.where(Call.status == status)
    if data_scope == "test":
        query = query.where(Call.team_id.in_(test_team_ids()))
    elif data_scope == "regular":
        query = query.where(Call.team_id.not_in(test_team_ids()))
    elif data_scope not in (None, "", "all"):
        fail(400, "invalid_data_scope")
    return query


def totals_columns(c):
    return [
        func.count().label("calls"),
        func.coalesce(func.sum(c.cost).filter(c.status == "settled"), 0).label("cost"),
        func.count().filter(c.status == "pending").label("pending"),
        func.count().filter(c.status.in_(("reserved", "running"))).label("running"),
        func.count(func.distinct(c.student_id)).label("active_members"),
        func.count().filter(c.outcome == "success").label("success"),
        func.count().filter(c.outcome == "upstream_failure").label("failures"),
        func.count().filter(c.outcome == "unknown").label("unknown"),
        func.count().filter(c.outcome == "client_disconnected").label("client_disconnected"),
        func.coalesce(func.sum(c.usage["prompt_tokens"].as_integer()), 0).label("input_tokens"),
        func.coalesce(func.sum(c.usage["completion_tokens"].as_integer()), 0).label("output_tokens"),
        func.coalesce(func.sum(c.usage["prompt_cache_hit_tokens"].as_integer()), 0).label("cache_hit_tokens"),
    ]


def totals_result(row):
    result = dict(row._mapping)
    result["pending_cost"] = None if result["pending"] else 0
    return result
