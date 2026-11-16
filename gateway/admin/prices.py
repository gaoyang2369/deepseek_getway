import httpx
from fastapi import APIRouter, Request
from sqlalchemy import or_, select, text

from ..models import (
    Price,
    now,
    uid,
)
from ..official_prices import SOURCE, PricingFormatError, fetch_prices
from ..operations import audit
from ..schemas import PriceCreate
from ..security import fail

router = APIRouter()


async def publish_price(db, values, previous, at):
    """The caller holds the model publication lock and owns the audit policy."""
    for old in previous:
        old.ends_at = at
    row = Price(**values, starts_at=at)
    db.add(row)
    await db.flush()
    return row


@router.post("/prices", status_code=201)
async def create_price(data: PriceCreate, request: Request):
    if (
        any(not x or len(x) > 128 for x in (data.model, data.upstream_model, data.version))
        or len(data.version) > 64
        or not 1 <= len(data.note) <= 500
    ):
        fail(400, "invalid_price_metadata")
    if (
        not 0 <= data.input_hit <= data.input_miss
        or min(data.input_miss, data.output) <= 0
        or max(data.input_miss, data.output) > 10**15
    ):
        fail(400, "invalid_price")
    async with request.app.state.db() as db, db.begin():
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:id, 2))"), {"id": data.model})
        at = now()
        previous = (
            await db.scalars(select(Price).where(Price.model == data.model, Price.ends_at.is_(None)))
        ).all()
        p = await publish_price(db, data.model_dump(), previous, at)
        audit(db, request, "price_create", p.id, data.model_dump())
        return {"id": p.id, "version": p.version}


@router.post("/prices/import-official")
async def import_official_prices(request: Request):
    try:
        tariffs = await fetch_prices()
    except (httpx.HTTPError, UnicodeError):
        fail(502, "official_pricing_unavailable")
    except PricingFormatError:
        fail(502, "official_pricing_format_changed")
    created, unchanged = [], []
    async with request.app.state.db() as db, db.begin():
        # Match manual price publication's lock and lock all models in stable order.
        for tariff in sorted(tariffs, key=lambda item: item["model"]):
            await db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:id, 2))"), {"id": tariff["model"]}
            )
        at = now()
        for tariff in tariffs:
            model, rates = tariff["model"], tariff["peak"]
            off_peak_note = "空闲价（元/百万 Token，未命中/命中/输出）：" + "/".join(
                f"{tariff['off_peak'][key] / 1_000_000:g}" for key in ("input_miss", "input_hit", "output")
            )
            existing = list(
                (await db.scalars(select(Price).where(Price.model == model, Price.ends_at.is_(None)))).all()
            )
            if (
                len(existing) == 1
                and existing[0].upstream_model == model
                and existing[0].estimated
                and existing[0].note.startswith("DeepSeek 官网自动导入")
                and all(getattr(existing[0], key) == value for key, value in rates.items())
                and off_peak_note in existing[0].note
            ):
                unchanged.append(model)
                continue
            note = (
                f"DeepSeek 官网自动导入；核对日期 {at.date()}；人民币/百万 Token。"
                "按官方高峰价保守估算，不按时段或法定节假日切换，实际账单可能较低。"
                f"{off_peak_note}。来源：{SOURCE}"
            )
            row = await publish_price(
                db,
                {
                    "model": model,
                    "upstream_model": model,
                    "version": "official-" + at.strftime("%Y%m%d") + "-" + uid(),
                    "estimated": True,
                    "note": note,
                    **rates,
                },
                existing,
                at,
            )
            audit(db, request, "price_import_official", row.id, {"model": model, "source": SOURCE, **rates})
            created.append(model)
    return {
        "created": created,
        "unchanged": unchanged,
        "source": SOURCE,
        "fetched_at": at,
        "policy": "official_peak_estimate",
        "tariffs": tariffs,
    }


@router.delete("/prices/{price_id}")
async def remove_price(price_id: str, request: Request):
    async with request.app.state.db() as db, db.begin():
        row = await db.get(Price, price_id)
        if row is None:
            fail(404, "price_not_found")
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:id, 2))"), {"id": row.model})
        await db.refresh(row)
        at = now()
        if row.ends_at is None or row.ends_at > at:
            row.ends_at = at
            audit(db, request, "price_remove", price_id, {"model": row.model, "version": row.version})
    return {"ok": True, "history_preserved": True}


@router.get("/prices")
async def prices(request: Request, include_ended: bool = False):
    async with request.app.state.db() as db:
        query = select(Price).order_by(Price.starts_at.desc())
        if not include_ended:
            query = query.where(Price.starts_at <= now(), or_(Price.ends_at.is_(None), Price.ends_at > now()))
        return [
            {
                k: getattr(p, k)
                for k in (
                    "id",
                    "model",
                    "upstream_model",
                    "version",
                    "input_miss",
                    "input_hit",
                    "output",
                    "estimated",
                    "note",
                    "starts_at",
                    "ends_at",
                )
            }
            for p in (await db.scalars(query)).all()
        ]
