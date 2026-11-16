"""Route registration must preserve authentication and the static asset boundary."""

from pathlib import Path


async def test_every_admin_operation_except_login_requires_a_session(app, client):
    for route in app.routes:
        if not route.path.startswith("/admin/api/") or route.path.endswith("/login"):
            continue
        path = route.path
        for parameter in route.param_convertors:
            path = path.replace("{" + parameter + "}", "missing")
        for method in route.methods:
            response = await client.request(method, path, json={})
            assert response.status_code == 401, (method, path, response.text)
            assert response.json()["error"]["message"] == "admin_login_required"


async def test_static_urls_serve_only_their_assigned_assets(client):
    static = Path(__file__).resolve().parents[1] / "gateway/static"
    assets = {
        "/student": "student.html",
        "/student/": "student.html",
        "/student/app.js": "student.js",
        "/student/guide.js": "student-guide.js",
        "/student/style.css": "style.css",
        "/admin": "index.html",
        "/admin/": "index.html",
        "/admin/app.js": "app.js",
        "/admin/students.js": "admin-students.js",
        "/admin/style.css": "style.css",
        **{
            f"/admin/{name}.js": f"admin-{name}.js"
            for name in ("common", "forms", "people", "reports", "prices")
        },
    }
    for url, filename in assets.items():
        response = await client.get(url, params={"name": "../../.env"})
        assert response.status_code == 200, url
        assert response.content == (static / filename).read_bytes()
        media_type = {".js": "text/javascript", ".css": "text/css", ".html": "text/html"}[
            Path(filename).suffix
        ]
        assert response.headers["content-type"].startswith(media_type)
    for path in ("/admin/unknown.js", "/admin/%2e%2e/.env", "/student/%2e%2e/config.py"):
        assert (await client.get(path)).status_code == 404
