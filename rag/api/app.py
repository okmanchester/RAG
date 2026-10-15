"""HTTP transport for the existing durable management and retrieval services."""
from contextlib import asynccontextmanager
import os
from pathlib import Path
import tempfile
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, File, Form, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from rag.management.config import ROOT, Settings
from rag.management.errors import public_error
from rag.management.service import ManagementService
from rag.management.storage import contained, relative_path

from . import schemas as s
from .browser import BrowserCORS, LocalBrowserGuard, browser_origins
from .errors import APIError, SafeBoundary, http_error, validation_error
from .runtime import Runtime

Limit = Annotated[int, Query(ge=1, le=1000)]
Offset = Annotated[int, Query(ge=0)]


class ContentTypeRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request):
            if self.body_field is None:
                return await original(request)
            expected = self.body_field.field_info.media_type
            received = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if not received and not self.body_field.field_info.is_required():
                return await original(request)
            if expected == "multipart/form-data":
                if received != expected:
                    raise HTTPException(415)
                # Explicit parser bounds; the context closes spooled files even on validation failure.
                async with request.form(max_files=1, max_fields=1, max_part_size=8192) as form:
                    if sorted(form.keys()) != ["file", "source_path"] or len(form.multi_items()) != 2:
                        raise APIError(422, "INVALID_REQUEST", "require exactly one file and one source_path")
                    return await original(request)
            if expected == "application/json" and not (received == expected or
                    received.startswith("application/") and received.endswith("+json")):
                raise HTTPException(415)
            return await original(request)

        return handle


def get_runtime(request: Request) -> Runtime:
    return request.app.state.runtime


Services = Annotated[Runtime, Depends(get_runtime)]


def create_app(*, runtime=None, max_upload_bytes=None, max_json_bytes=None, allowed_browser_origins=None, tmp_root=None):
    """Application factory. Injectable runtime is owned by the caller except its search client."""
    if max_upload_bytes is None:
        max_upload_bytes = int(os.environ.get("RAG_API_MAX_UPLOAD_BYTES", 100 * 1024 * 1024))
    if type(max_upload_bytes) is not int or max_upload_bytes < 1:
        raise ValueError("RAG_API_MAX_UPLOAD_BYTES must be a positive integer")
    if max_json_bytes is None:
        max_json_bytes = int(os.environ.get("RAG_API_MAX_JSON_BYTES", 64 * 1024))
    if type(max_json_bytes) is not int or max_json_bytes < 1:
        raise ValueError("RAG_API_MAX_JSON_BYTES must be a positive integer")
    if allowed_browser_origins is None:
        configured = os.environ.get("RAG_API_BROWSER_ORIGINS", "")
        allowed_browser_origins = [value.strip() for value in configured.split(",") if value.strip()]
    allowed_browser_origins = browser_origins(allowed_browser_origins)
    temporary_root = Path(tmp_root) if tmp_root is not None else ROOT / "tmp"

    @asynccontextmanager
    async def lifespan(app):
        owned_engine, active = None, None
        previous_tempdir = tempfile.tempdir
        try:
            temporary_root.mkdir(parents=True, exist_ok=True)
            # The API runs in its own process. Also keep multipart's implicit spool files in tmp/.
            with tempfile.TemporaryDirectory(prefix="http-spool-", dir=temporary_root) as spool:
                tempfile.tempdir = spool
                try:
                    app.state.runtime_identity = None
                    if runtime is None:
                        settings = Settings.from_env()
                        owned_engine = settings.engine()
                        active = Runtime(ManagementService(owned_engine, settings.storage_root))
                    else:
                        active = runtime
                    active.management.db_check()
                    if owned_engine is not None:
                        from rag.ops.state import identity
                        app.state.runtime_identity = identity(settings)
                    app.state.runtime = active
                    yield
                finally:
                    try:
                        if active is not None:
                            active.close()
                    finally:
                        tempfile.tempdir = previous_tempdir
                        if owned_engine is not None:
                            owned_engine.dispose()
        except Exception as exc:
            # Uvicorn logs startup failures. Do not attach SQL/credentials or exception context.
            raise RuntimeError(public_error(exc)["error_code"]) from None

    app = FastAPI(title="RAG HTTP API", version="1.0.0", lifespan=lifespan,
                  description="本机文档管理、持久化任务状态与检索 API；处理成功后需显式启用 build。",
                  redoc_url=None)
    app.add_middleware(SafeBoundary, max_body_bytes=max_upload_bytes + 1024 * 1024, max_json_bytes=max_json_bytes)
    # CORS wraps the error boundary so allowed browser clients can also read failures.
    app.add_middleware(BrowserCORS, allow_origins=allowed_browser_origins, allow_credentials=False,
                       allow_methods=["GET", "POST", "PUT", "PATCH"], allow_headers=["Content-Type"],
                       expose_headers=["Location"])
    # Reject Host/Origin before CORS preflight handling, request parsing, or any side effects.
    app.add_middleware(LocalBrowserGuard, allowed_origins=allowed_browser_origins)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(HTTPException, http_error)
    router = APIRouter(prefix="/api/v1", route_class=ContentTypeRoute,
        responses={status: {"model": s.Error} for status in (400, 403, 404, 405, 409, 413, 415, 422, 500, 503)})

    @app.get("/health", response_model=s.Health, tags=["health"])
    def health():
        """Process liveness only; does not assert MySQL/ES/model availability."""
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    def management_page():
        return FileResponse(Path(__file__).with_name("web") / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/assets", StaticFiles(directory=Path(__file__).with_name("web")), name="assets")

    @router.get("/runtime-status", response_model=s.RuntimeStatus, tags=["health"])
    def local_runtime_status():
        from rag.ops.state import runtime_status
        return runtime_status(expected_identity=app.state.runtime_identity)

    @router.get("/knowledge-bases", response_model=s.Page[s.KnowledgeBase], tags=["documents"])
    def knowledge_bases(services: Services, status: s.ManagementStatus | None = None,
                        limit: Limit = 100, offset: Offset = 0):
        return services.management.list_records("kb", status=status, limit=limit, offset=offset)

    @router.get("/knowledge-bases/{kb_id}/documents", response_model=s.Page[s.Document], tags=["documents"])
    def documents(kb_id: s.Identifier, services: Services, status: s.ManagementStatus | None = None,
                  limit: Limit = 100, offset: Offset = 0):
        return services.management.list_records("document", kb_id=kb_id, status=status, limit=limit, offset=offset)

    @router.post("/knowledge-bases/{kb_id}/documents", response_model=s.UploadResult, status_code=202,
                 responses={200: {"model": s.UploadResult, "description": "Existing terminal task was reused"}},
                 tags=["documents"])
    def upload(kb_id: s.Identifier, response: Response, services: Services,
               file: Annotated[UploadFile, File()], source_path: Annotated[str, Form()]):
        path = relative_path(source_path)
        if Path(path).suffix.lower() not in (".pdf", ".docx"):
            from rag.management.errors import ManagementError
            raise ManagementError("INVALID_DOCUMENT_FORMAT")
        if file.size is not None and file.size > max_upload_bytes:
            raise HTTPException(413)
        with tempfile.TemporaryDirectory(prefix="http-upload-", dir=temporary_root) as work:
            target = contained(Path(work), path)
            target.parent.mkdir(parents=True, exist_ok=True)
            count = 0
            with target.open("wb") as stream:
                while data := file.file.read(1024 * 1024):
                    count += len(data)
                    if count > max_upload_bytes:
                        raise HTTPException(413)
                    stream.write(data)
            if not count:
                raise APIError(422, "EMPTY_UPLOAD", "uploaded file must not be empty")
            result = services.processing.register_and_submit(kb_id, Path(work), path, **services.processing_options)
        response.status_code = 202 if result["status"] in ("pending", "running") else 200
        response.headers["Location"] = f"/api/v1/processing-tasks/{result['task_id']}"
        return result

    @router.get("/documents/{document_id}", response_model=s.DocumentDetail, tags=["documents"])
    def document(document_id: s.Identifier, services: Services):
        return services.management.show("document", document_id)

    @router.get("/documents/{document_id}/versions", response_model=s.Page[s.Version], tags=["documents"])
    def versions(document_id: s.Identifier, services: Services, limit: Limit = 100, offset: Offset = 0):
        return services.management.list_records("version", document_id=document_id, limit=limit, offset=offset)

    @router.get("/documents/{document_id}/builds", response_model=s.Page[s.Build], tags=["documents"])
    def builds(document_id: s.Identifier, services: Services, limit: Limit = 100, offset: Offset = 0):
        return services.management.list_builds(document_id, limit=limit, offset=offset)

    @router.put("/documents/{document_id}/active-build", response_model=s.ActivationResult, tags=["documents"])
    def activate(document_id: s.Identifier, body: s.Activation, services: Services):
        return services.management.activate_document(document_id, body.build_id)

    @router.patch("/documents/{document_id}/status", response_model=s.StatusResult, tags=["documents"])
    def set_status(document_id: s.Identifier, body: s.StatusChange, services: Services):
        return services.management.set_status("document", document_id, body.status)

    @router.post("/versions/{version_id}/processing-tasks", response_model=s.TaskSubmission, status_code=202,
                 responses={200: {"model": s.TaskSubmission, "description": "Existing terminal task was reused"}},
                 tags=["tasks"])
    def submit(version_id: s.Identifier, response: Response, services: Services, body: s.RequestModel | None = None):
        result = services.processing.submit(version_id, **services.processing_options)
        response.status_code = 202 if result["status"] in ("pending", "running") else 200
        response.headers["Location"] = f"/api/v1/processing-tasks/{result['task_id']}"
        return result

    @router.get("/knowledge-bases/{kb_id}/processing-tasks", response_model=s.Page[s.ProcessingTask], tags=["tasks"])
    def tasks(kb_id: s.Identifier, services: Services, status: s.TaskStatus | None = None,
              limit: Limit = 100, offset: Offset = 0):
        return services.processing.list(kb_id, status=status, limit=limit, offset=offset)

    @router.get("/processing-tasks/{task_id}", response_model=s.ProcessingDetail, tags=["tasks"])
    def task(task_id: s.Identifier, services: Services, limit: Limit = 100, offset: Offset = 0):
        return services.processing.show(task_id, limit=limit, offset=offset)

    @router.post("/processing-tasks/{task_id}/retry", response_model=s.RetryResult, status_code=202, tags=["tasks"])
    def retry(task_id: s.Identifier, services: Services, body: s.RequestModel | None = None):
        return services.processing.retry(task_id)

    @router.get("/knowledge-bases/{kb_id}/index-status", response_model=s.IndexStatus, tags=["tasks"])
    def index_status(kb_id: s.Identifier, services: Services, db_only: bool = True,
                     limit: Limit = 100, offset: Offset = 0):
        return services.index_status(kb_id, db_only, limit, offset)

    @router.post("/knowledge-bases/{kb_id}/index-sync/retry", response_model=s.SyncControl, status_code=202,
                 tags=["tasks"])
    def retry_index(kb_id: s.Identifier, services: Services, body: s.RequestModel | None = None):
        return services.indexing.control(kb_id, "retry")

    @router.post("/knowledge-bases/{kb_id}/search", response_model=s.SearchResult, tags=["search"])
    def search(kb_id: s.Identifier, body: s.SearchRequest, services: Services):
        return services.query(kb_id, body)

    app.include_router(router)
    return app
