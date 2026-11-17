"""Image uploads with membership-scoped handles; no raw image bytes in the DB."""

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal

import anyio
import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import or_, select, tuple_
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from .keys import authenticate
from .models import Credential, UploadedFile, now, uid
from .security import fail

router = APIRouter(prefix="/v1/files")
log = logging.getLogger("lab_gateway")
MAX_IMAGE = 64 * 1024 * 1024


@asynccontextmanager
async def image_form(request):
    parser = MultiPartParser(request.headers, request.stream(), max_files=1, max_fields=3, max_part_size=1024)
    try:
        yield await parser.parse()
    except MultiPartException:
        fail(422, "invalid_multipart_upload")
    finally:
        # The pinned Starlette parser closes these only on MultiPartException.
        # Also close on our 413 guard, disconnect, cancellation and upload errors.
        for file in parser._files_to_close_on_error:
            file.close()


def metadata(row):
    return {
        "id": row.id,
        "object": "file",
        "bytes": row.size,
        "created_at": int(row.created_at.timestamp()),
        "filename": row.filename,
        "purpose": "user_data",
        **({"expires_at": int(row.expires_at.timestamp())} if row.expires_at else {}),
    }


async def identity(request, db):
    _, member, _, team = await authenticate(
        db,
        request.headers.get("authorization"),
        request.app.state.settings,
    )
    return member, team


async def upstream(request, credential, method, path, **kwargs):
    app = request.app
    try:
        key = app.state.cipher.decrypt(credential.ciphertext.encode()).decode()
    except Exception:
        fail(503, "upstream_configuration_error")
    try:
        response = await app.state.http.request(
            method,
            app.state.settings.upstream_url + path,
            headers={"Authorization": "Bearer " + key},
            **kwargs,
        )
    except httpx.HTTPError:
        fail(502, "file_upstream_unavailable")
    if response.status_code == 404 and method == "DELETE":
        return {"deleted": True}
    if response.status_code >= 400:
        # Provider payloads can contain credentials or file content. Return only
        # a fixed error and numeric status; SDKs must not blindly retry uploads.
        raise HTTPException(
            502,
            detail={
                "message": "file_upstream_rejected",
                "details": {"upstream_status": response.status_code},
            },
        )
    try:
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError()
    except ValueError:
        fail(502, "file_upstream_protocol_error")
    return result


def image_type(header):
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp"
    fail(422, "only_image_uploads_supported")


@router.post("")
async def upload(request: Request):
    if request.query_params:
        fail(400, "query_parameters_not_supported")
    async with request.app.state.db() as db:
        member, team = await identity(request, db)
        credential = await db.scalar(
            select(Credential)
            .where(
                Credential.team_id == team.id,
                Credential.active.is_(True),
            )
            .order_by(Credential.version.desc())
            .limit(1)
        )
        if not credential:
            fail(503, "team_upstream_unavailable")
    async with image_form(request) as form:
        allowed = {"file", "purpose", "expires_after[anchor]", "expires_after[seconds]"}
        if set(form) - allowed or len(form.multi_items()) != len(form) or form.get("purpose") != "user_data":
            fail(422, "image_upload_requires_user_data")
        file = form.get("file")
        if not isinstance(file, UploadFile) or not file.size:
            fail(422, "image_file_required")
        if file.size > MAX_IMAGE:
            details = {"limit_type": "file_bytes", "received_bytes": file.size, "limit_bytes": MAX_IMAGE}
            request.state.rejection_details = details
            raise HTTPException(413, detail={"message": "image_file_too_large", "details": details})
        seconds = None  # Match the official API: permanent unless an expiry is requested.
        if "expires_after[anchor]" in form or "expires_after[seconds]" in form:
            value = form.get("expires_after[seconds]")
            if (
                form.get("expires_after[anchor]") != "created_at"
                or not isinstance(value, str)
                or not value.isascii()
                or not value.isdigit()
                or len(value) > 7
            ):
                fail(422, "invalid_file_expiry")
            seconds = int(value)
            if not 3600 <= seconds <= 2592000:
                fail(422, "invalid_file_expiry")
        mime = image_type(await file.read(16))
        await file.seek(0)
        filename = (file.filename or "image").replace("\\", "/").rsplit("/", 1)[-1]
        if not filename or len(filename.encode()) > 512 or any(ord(c) < 32 for c in filename):
            fail(422, "invalid_filename")
        result = await upstream(
            request,
            credential,
            "POST",
            "/files",
            data={
                "purpose": "user_data",
                **(
                    {"expires_after[anchor]": "created_at", "expires_after[seconds]": str(seconds)}
                    if seconds
                    else {}
                ),
            },
            files={"file": (filename, file.file, mime)},
        )
        upstream_id = result.get("id")
        if not isinstance(upstream_id, str) or not re.fullmatch(r"file-[A-Za-z0-9_-]{1,120}", upstream_id):
            fail(502, "file_upstream_protocol_error")
        try:
            expires = result.get("expires_at")
            if (seconds is not None and expires is None) or (
                expires is not None
                and (
                    type(expires) is not int or not now().timestamp() < expires <= now().timestamp() + 2592060
                )
            ):
                fail(502, "file_upstream_protocol_error")
            row = UploadedFile(
                id="file-lab-" + uid().replace("-", ""),
                membership_id=member.id,
                credential_id=credential.id,
                upstream_id=upstream_id,
                filename=filename,
                size=file.size,
                created_at=now(),
                expires_at=datetime.fromtimestamp(expires, timezone.utc) if expires else None,
            )
            async with request.app.state.db() as db, db.begin():
                # A revoked key or disabled identity during upload must not gain a handle.
                await identity(request, db)
                db.add(row)
            return metadata(row)
        except BaseException:
            with anyio.CancelScope(shield=True):
                try:
                    await asyncio.shield(upstream(request, credential, "DELETE", "/files/" + upstream_id))
                except Exception:
                    log.error("file_cleanup_failed request_id=%s", request.state.request_id)
            raise


async def owned(db, member_id, file_id, include_deleted=False):
    row = await db.get(UploadedFile, file_id)
    if not row or row.membership_id != member_id or (row.deleted_at and not include_deleted):
        fail(404, "file_not_found")
    return row


@router.get("")
async def list_files(
    request: Request,
    after: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    order: Literal["asc", "desc"] = "asc",
    purpose: Literal["user_data"] = "user_data",
):
    async with request.app.state.db() as db:
        member, _ = await identity(request, db)
        q = select(UploadedFile).where(
            UploadedFile.membership_id == member.id,
            UploadedFile.deleted_at.is_(None),
            or_(UploadedFile.expires_at.is_(None), UploadedFile.expires_at > now()),
        )
        if after:
            cursor = await owned(db, member.id, after, include_deleted=True)
            left = tuple_(UploadedFile.created_at, UploadedFile.id)
            right = (cursor.created_at, cursor.id)
            q = q.where(left > right if order == "asc" else left < right)
        ordering = (UploadedFile.created_at, UploadedFile.id)
        q = q.order_by(*(column.desc() if order == "desc" else column.asc() for column in ordering))
        rows = (await db.scalars(q.limit(limit + 1))).all()
        items = [metadata(row) for row in rows[:limit]]
        return {
            "object": "list",
            "data": items,
            "has_more": len(rows) > limit,
            "first_id": items[0]["id"] if items else None,
            "last_id": items[-1]["id"] if items else None,
        }


@router.get("/{file_id}")
async def retrieve(request: Request, file_id: str):
    async with request.app.state.db() as db:
        member, _ = await identity(request, db)
        row = await owned(db, member.id, file_id)
        if row.expires_at and row.expires_at <= now():
            fail(410, "file_expired")
        return metadata(row)


@router.delete("/{file_id}")
async def delete_file(request: Request, file_id: str):
    async with request.app.state.db() as db:
        member, _ = await identity(request, db)
        row = await owned(db, member.id, file_id, include_deleted=True)
        credential = await db.get(Credential, row.credential_id)
    if not row.deleted_at and (row.expires_at is None or row.expires_at > now()):
        result = await upstream(request, credential, "DELETE", "/files/" + row.upstream_id)
        if result.get("deleted") is not True:
            fail(502, "file_upstream_protocol_error")
    async with request.app.state.db() as db, db.begin():
        current = await owned(db, member.id, file_id, include_deleted=True)
        current.deleted_at = current.deleted_at or now()
    return {"id": file_id, "object": "file", "deleted": True}


async def resolve_references(app, call, body):
    parts = [
        part
        for message in body["messages"]
        if isinstance(message.get("content"), list)
        for part in message["content"]
        if part.get("type") == "file"
    ]
    if not parts:
        return
    async with app.state.db() as db:
        ids = {part["file_id"] for part in parts}
        rows = {
            row.id: row
            for row in (
                await db.scalars(
                    select(UploadedFile).where(
                        UploadedFile.id.in_(ids),
                        UploadedFile.membership_id == call.membership_id,
                        UploadedFile.deleted_at.is_(None),
                    )
                )
            ).all()
        }
        if rows.keys() != ids:
            fail(404, "file_not_found")
        for part in parts:
            row = rows[part["file_id"]]
            if row.expires_at and row.expires_at <= now():
                fail(410, "file_expired")
            if row.credential_id != call.credential_id:
                fail(409, "file_credential_changed_reupload")
            part["file_id"] = row.upstream_id
