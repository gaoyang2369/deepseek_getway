from pathlib import Path

from fastapi import Request
from fastapi.responses import FileResponse, RedirectResponse

# URL -> (file, original route name). Keep both slash forms and OpenAPI operation IDs.
ASSETS = {
    "/student": ("student.html", "student_page"),
    "/student/": ("student.html", "student_page"),
    "/student/app.js": ("student.js", "student_script"),
    "/student/guide.js": ("student-guide.js", "student_guide_script"),
    "/student/style.css": ("style.css", "student_style"),
    "/admin": ("index.html", "admin_page"),
    "/admin/": ("index.html", "admin_page"),
    "/admin/app.js": ("app.js", "admin_script"),
    "/admin/students.js": ("admin-students.js", "admin_students_script"),
    "/admin/style.css": ("style.css", "admin_css"),
    **{
        f"/admin/{name}.js": (f"admin-{name}.js", None)
        for name in ("common", "forms", "people", "reports", "prices")
    },
}


async def static_asset(request: Request):
    filename, _ = ASSETS[request.url.path]
    path = Path(__file__).parent / "static" / filename
    media_type = {".js": "text/javascript", ".css": "text/css"}.get(path.suffix)
    return FileResponse(path, media_type=media_type)


def install_pages(app, settings):
    @app.get("/")
    async def home():
        return RedirectResponse("/student", status_code=302)

    @app.get("/student/api/config")
    async def student_config():
        return {"demo": not settings.production, "support_email": settings.support_email}

    for url, (_, name) in ASSETS.items():
        app.add_api_route(url, static_asset, name=name, methods=["GET"], include_in_schema=name is not None)
