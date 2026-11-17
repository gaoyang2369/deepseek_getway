import base64
import time
from datetime import timedelta

import httpx
from conftest import completion, price, student, team
from fastapi import FastAPI, Request
from openai import AsyncOpenAI
from sqlalchemy import select

from gateway.models import Call, UploadedFile, now
from scripts.mock_upstream import chat as mock_chat

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aL1sAAAAASUVORK5CYII="
)


async def file_upstream(app):
    mock = FastAPI()
    uploads, chats, deletes = [], [], []

    @mock.post("/v1/files")
    async def upload(request: Request):
        async with request.form() as form:
            data = await form["file"].read()
            uploads.append(
                {"data": data, "purpose": form["purpose"], "key": request.headers["authorization"]}
            )
            return {
                "id": f"file-api-{len(uploads)}",
                "object": "file",
                "bytes": len(data),
                "created_at": int(time.time()),
                "filename": form["file"].filename,
                "purpose": form["purpose"],
                **(
                    {"expires_at": int(time.time()) + int(form["expires_after[seconds]"])}
                    if "expires_after[seconds]" in form
                    else {}
                ),
            }

    @mock.delete("/v1/files/{file_id}")
    async def delete(request: Request, file_id: str):
        deletes.append((file_id, request.headers["authorization"]))
        return {"id": file_id, "object": "file", "deleted": True}

    @mock.post("/v1/chat/completions")
    async def chat(request: Request):
        chats.append(await request.json())
        return await mock_chat(request)

    await app.state.http.aclose()
    app.state.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock))
    return uploads, chats, deletes


async def upload(client, key, **kwargs):
    return await client.post(
        "/v1/files",
        headers={"Authorization": "Bearer " + key},
        data={"purpose": "user_data"},
        files={"file": ("picture.png", PNG, "image/png")},
        **kwargs,
    )


async def test_python_sdk_upload_reuse_list_retrieve_delete(admin, app):
    await price(admin)
    person, _ = await student(admin, await team(admin))
    uploads, chats, deletes = await file_upstream(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as transport:
        sdk = AsyncOpenAI(
            api_key=person["key"], base_url="http://gateway/v1", http_client=transport, max_retries=0
        )
        image = await sdk.files.create(file=("picture.png", PNG, "image/png"), purpose="user_data")
        assert image.id.startswith("file-lab-") and image.bytes == len(PNG)
        assert (await sdk.files.retrieve(image.id)).id == image.id
        assert [f.id for f in (await sdk.files.list()).data] == [image.id]
        for streaming in (False, True):
            result = await sdk.chat.completions.create(
                model="lab-chat",
                stream=streaming,
                messages=[{"role": "user", "content": [{"type": "file", "file_id": image.id}]}],
            )
            if streaming:
                async for _ in result:
                    pass
        assert len(uploads) == 1 and uploads[0]["data"] == PNG
        assert len(chats) == 2 and all(
            c["messages"][0]["content"] == [{"type": "file", "file_id": "file-api-1"}] for c in chats
        )
        assert (await sdk.files.delete(image.id)).deleted
        assert (await sdk.files.delete(image.id)).deleted
        assert len(deletes) == 1
        assert not (await sdk.files.list()).data
    async with app.state.db() as db:
        calls = (await db.scalars(select(Call))).all()
        assert len(calls) == 2 and all(c.cost == 188 and c.reservation == 0 for c in calls)
        row = await db.get(UploadedFile, image.id)
        assert row.deleted_at and row.membership_id == person["membership_id"]


async def test_files_cannot_cross_members_or_teams(admin, app):
    await price(admin)
    group = await team(admin)
    owner, _ = await student(admin, group)
    peer, _ = await student(admin, group, "002")
    outsider, _ = await student(admin, await team(admin, "B"), "003")
    _, chats, deletes = await file_upstream(app)
    image = (await upload(admin, owner["key"])).json()
    for person in (peer, outsider):
        headers = {"Authorization": "Bearer " + person["key"]}
        assert not (await admin.get("/v1/files", headers=headers)).json()["data"]
        assert (await admin.get("/v1/files/" + image["id"], headers=headers)).status_code == 404
        assert (await admin.delete("/v1/files/" + image["id"], headers=headers)).status_code == 404
        assert (
            await completion(admin, person["key"], [{"type": "file", "file_id": image["id"]}])
        ).status_code == 404
    assert not chats and not deletes


async def test_expiry_rotation_and_deleted_handles_fail_before_inference(admin, app):
    await price(admin)
    group = await team(admin)
    person, _ = await student(admin, group)
    _, chats, deletes = await file_upstream(app)
    image = (await upload(admin, person["key"])).json()
    content = [{"type": "file", "file_id": image["id"]}]
    assert (
        await admin.post(
            f"/admin/api/teams/{group['id']}/credential", json={"upstream_key": "MOCK-new-credential"}
        )
    ).status_code == 200
    assert (await completion(admin, person["key"], content)).status_code == 409
    async with app.state.db() as db, db.begin():
        (await db.get(UploadedFile, image["id"])).expires_at = now() - timedelta(seconds=1)
    assert (await completion(admin, person["key"], content)).status_code == 410
    headers = {"Authorization": "Bearer " + person["key"]}
    assert (await admin.delete("/v1/files/" + image["id"], headers=headers)).status_code == 200
    assert (await completion(admin, person["key"], content)).status_code == 404
    assert not chats and not deletes


async def test_bad_uploads_and_oversize_report_safe_metadata(admin, app, monkeypatch):
    await price(admin)
    person, _ = await student(admin, await team(admin))
    uploads, _, _ = await file_upstream(app)
    headers = {"Authorization": "Bearer " + person["key"]}
    response = await admin.post(
        "/v1/files",
        headers=headers,
        data={"purpose": "user_data"},
        files={"file": ("secret.txt", b"secret-file-body", "image/png")},
    )
    assert response.status_code == 422 and "secret-file-body" not in response.text
    monkeypatch.setattr("gateway.files.MAX_IMAGE", 32)
    response = await upload(admin, person["key"])
    assert response.status_code == 413
    assert response.json()["error"]["details"]["limit_bytes"] == 32
    rows = (await admin.get("/admin/api/rejections")).json()["rows"]
    assert rows[0]["details"]["received_bytes"] == len(PNG)
    assert not uploads


async def test_file_persistence_failure_deletes_upstream_upload(admin, app, monkeypatch):
    await price(admin)
    person, _ = await student(admin, await team(admin))
    _, _, deletes = await file_upstream(app)
    from sqlalchemy import event

    def reject(mapper, connection, target):
        raise RuntimeError("injected DB failure")

    event.listen(UploadedFile, "before_insert", reject)
    try:
        assert (await upload(admin, person["key"])).status_code == 500
    finally:
        event.remove(UploadedFile, "before_insert", reject)
    assert len(deletes) == 1


async def test_oversized_chunked_upload_closes_temporary_files(admin, app, monkeypatch):
    from dataclasses import replace

    import starlette.formparsers

    from gateway.http import Guard

    await price(admin)
    person, _ = await student(admin, await team(admin))
    uploads, _, _ = await file_upstream(app)
    middleware = app.middleware_stack
    while not isinstance(middleware, Guard):
        middleware = middleware.app
    middleware.settings = replace(middleware.settings, max_upload_body=2048)
    temporary = []
    original = starlette.formparsers.SpooledTemporaryFile

    def opened(*args, **kwargs):
        result = original(*args, **kwargs)
        temporary.append(result)
        return result

    monkeypatch.setattr(starlette.formparsers, "SpooledTemporaryFile", opened)
    body = (
        b'--boundary\r\nContent-Disposition: form-data; name="purpose"\r\n\r\nuser_data\r\n'
        b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="image.png"\r\n'
        b"Content-Type: image/png\r\n\r\n" + PNG + b"x" * 5000 + b"\r\n--boundary--\r\n"
    )

    async def chunks():
        for start in range(0, len(body), 512):
            yield body[start : start + 512]

    response = await admin.post(
        "/v1/files",
        content=chunks(),
        headers={
            "Authorization": "Bearer " + person["key"],
            "Content-Type": "multipart/form-data; boundary=boundary",
        },
    )
    assert response.status_code == 413
    assert response.json()["error"]["details"]["received_bytes"] == 2560
    assert temporary and all(f.closed for f in temporary)
    assert not uploads


async def test_file_expiry_and_pagination(admin, app):
    await price(admin)
    person, _ = await student(admin, await team(admin))
    await file_upstream(app)
    headers = {"Authorization": "Bearer " + person["key"]}
    permanent = (await upload(admin, person["key"])).json()
    assert "expires_at" not in permanent
    timed = await admin.post(
        "/v1/files",
        headers=headers,
        data={
            "purpose": "user_data",
            "expires_after[anchor]": "created_at",
            "expires_after[seconds]": "3600",
        },
        files={"file": ("image.png", PNG, "image/png")},
    )
    assert timed.status_code == 200 and timed.json()["expires_at"] > int(time.time())
    first = (await admin.get("/v1/files?limit=1", headers=headers)).json()
    assert first["has_more"] and first["data"][0]["id"] == permanent["id"]
    second = (
        await admin.get("/v1/files", headers=headers, params={"after": first["last_id"], "limit": 1})
    ).json()
    assert not second["has_more"] and second["data"][0]["id"] == timed.json()["id"]
