from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.openapi.docs import get_swagger_ui_html
from starlette.middleware.trustedhost import TrustedHostMiddleware

from server.config import Settings
from server.models import ApprovalDecision, FollowUpCreate, InputResponse, MemoryCreate, MemoryDecision, MemoryUpdate, PlanUpdate, ReportRevision, RunCreate, RunRecord, TaskCreate, TaskRecord, ThreadCreate, ThreadRecord, ToolCallCreate, ToolCallRecord
from server.events import run_stream, workspace_snapshot
from server.harness.runtime import Harness
from server.harness.context import ContextManager
from server.memory import MemoryService
from server.artifacts import ArtifactService
from server.exports import export_report, html_report
from server.security import SessionMiddleware, session_owner, write_owner
from server.services import ResearchService, ServiceError, view
from server.models import DocumentImport


def router_for(service: ResearchService) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get('/documents')
    def documents(owner: str = Depends(session_owner)):
        return {'documents':service.documents.listing(owner)}

    @router.post('/documents', status_code=201)
    def import_document(payload: DocumentImport, owner: str = Depends(write_owner)):
        return service.documents.import_text(owner,**payload.model_dump())

    @router.get('/documents/search')
    def search_documents(query: str = Query(min_length=1,max_length=300), limit: int = Query(default=5,ge=1,le=8), owner: str = Depends(session_owner)):
        return {'excerpts':service.documents.search(owner,query,limit)}

    @router.get('/runs/{run_id}/trace')
    def trace(run_id: str, owner: str = Depends(session_owner)):
        from server.tracing import trace_view
        return trace_view(service,owner,run_id)

    @router.get("/session")
    def session(request: Request):
        session_owner(request)
        return {"csrf_token": request.state.session["csrf_token"], "expires_at": request.state.session["expires_at"]}

    @router.get("/tools")
    def tools(owner: str = Depends(session_owner)):
        return {"tools": service.tools(), "ifind_status": "configured_unverified" if service.settings.ifind_mcp_url else "not_configured"}

    @router.post("/tools/ifind/discover")
    async def discover(owner: str = Depends(write_owner)):
        return await service.discover_ifind()

    @router.get("/threads")
    def threads(owner: str = Depends(session_owner)):
        return {"threads": service.threads(owner)}

    @router.post("/threads", status_code=201, response_model=ThreadRecord)
    def create_thread(payload: ThreadCreate, owner: str = Depends(write_owner)):
        return service.create_thread(owner, payload.title, payload.goal)

    @router.get("/threads/{thread_id}")
    def thread(thread_id: str, owner: str = Depends(session_owner)):
        record = service.resource("threads", thread_id, owner)
        record["runs"] = [view(x) for x in service.db.all(
            "SELECT * FROM runs WHERE thread_id=? AND session_id=? ORDER BY created_at DESC LIMIT 100", (thread_id, owner))]
        return record

    @router.delete('/threads/{thread_id}')
    def delete_thread(thread_id: str, owner: str = Depends(write_owner)):
        return service.delete_thread(owner, thread_id)

    @router.post("/threads/{thread_id}/runs", status_code=201, response_model=RunRecord)
    def create_run(thread_id: str, payload: RunCreate, owner: str = Depends(write_owner)):
        if payload.mode == "agent":
            return service.harness.submit(owner, thread_id, payload.goal, payload.budget)
        return service.create_run(owner, thread_id, payload.goal, payload.mode)

    @router.get('/threads/{thread_id}/messages')
    def messages(thread_id: str, before: int | None = Query(None,ge=1), limit: int = Query(60,ge=1,le=60), owner: str = Depends(session_owner)):
        return service.dialogue.message_page(owner,thread_id,before=before,limit=limit)

    @router.post('/runs/{run_id}/input')
    def answer_input(run_id: str, payload: InputResponse, owner: str = Depends(write_owner)):
        return service.dialogue.respond(owner,run_id,payload.question_id,payload.answer)

    @router.get("/runs/{run_id}")
    def run(run_id: str, owner: str = Depends(session_owner)):
        record = service.harness.snapshot(owner, run_id)
        record["tasks"] = service.collection(owner, run_id, "tasks")
        record["tool_calls"] = service.collection(owner, run_id, "tool_calls")
        return record

    @router.post("/runs/{run_id}/tasks", status_code=201, response_model=TaskRecord)
    def create_task(run_id: str, payload: TaskCreate, owner: str = Depends(write_owner)):
        if service.resource("runs", run_id, owner)["mode"] == "agent":
            raise ServiceError(409, "自动研究任务由已验证计划生成")
        return service.create_task(owner, run_id, **payload.model_dump())

    @router.post("/runs/{run_id}/control/{action}")
    def control(run_id: str, action: str, owner: str = Depends(write_owner)):
        return service.harness.control(owner, run_id, action)

    @router.get("/runs/{run_id}/snapshot")
    def snapshot(run_id: str, owner: str = Depends(session_owner)):
        return workspace_snapshot(service, owner, run_id)

    @router.put("/runs/{run_id}/plan")
    def edit_plan(run_id: str, payload: PlanUpdate, owner: str = Depends(write_owner)):
        return service.harness.edit_plan(owner, run_id, payload)

    @router.get("/runs/{run_id}/approvals")
    def approvals(run_id: str, owner: str = Depends(session_owner)):
        return {"approvals": service.harness.snapshot(owner, run_id)["approvals"]}

    @router.post("/approvals/{approval_id}/decision")
    def decision(approval_id: str, payload: ApprovalDecision, owner: str = Depends(write_owner)):
        return service.harness.decide(owner, approval_id, payload)

    @router.get("/runs/{run_id}/report")
    def report(run_id: str, owner: str = Depends(session_owner)):
        rows = service.harness.snapshot(owner, run_id)["artifacts"]
        reports = [x for x in rows if x["kind"] == "research_report"]
        if not reports:
            raise ServiceError(404, "当前研究尚未生成报告")
        return service.artifacts.verify(owner, reports[-1]['id'])

    @router.get("/artifacts/{artifact_id}")
    def artifact(artifact_id: str, owner: str = Depends(session_owner)):
        record = service.resource("artifacts", artifact_id, owner)
        return service.artifacts.verify(owner, artifact_id) if record['kind']=='research_report' else record

    @router.get('/runs/{run_id}/reports')
    def report_versions(run_id: str, owner: str = Depends(session_owner)):
        return {'reports': service.artifacts.versions(owner, run_id)}

    @router.post('/runs/{run_id}/reports', status_code=201)
    def report_revision(run_id: str, payload: ReportRevision, owner: str = Depends(write_owner)):
        return service.artifacts.revise(owner, run_id, payload)

    @router.post('/runs/{run_id}/followups', status_code=201, response_model=RunRecord)
    def followup(run_id: str, payload: FollowUpCreate, owner: str = Depends(write_owner)):
        return service.artifacts.followup(owner, run_id, payload)

    @router.get('/artifacts/{artifact_id}/export')
    def export(artifact_id: str, format: Literal['markdown','html','csv','evidence'] = 'markdown', owner: str = Depends(session_owner)):
        data, media, filename = export_report(service, owner, artifact_id, format)
        return Response(data, media_type=media, headers={'Content-Disposition': f'attachment; filename="{filename}"', 'Cache-Control': 'no-store'})

    @router.get('/artifacts/{artifact_id}/print', response_class=HTMLResponse)
    def print_report(artifact_id: str, owner: str = Depends(session_owner)):
        artifact = service.artifacts.verify(owner, artifact_id)
        return HTMLResponse(html_report(service, owner, artifact, print_view=True), headers={'Cache-Control': 'no-store'})

    @router.get('/memories')
    def memories(q: str = Query(default='', max_length=200), include_inactive: bool = True, owner: str = Depends(session_owner)):
        return {'memories': service.memories.list(owner, q, include_inactive)}

    @router.post('/memories', status_code=201)
    def save_memory(payload: MemoryCreate, owner: str = Depends(write_owner)):
        return service.memories.create(owner, payload)

    @router.get('/memories/{memory_id}')
    def memory(memory_id: str, owner: str = Depends(session_owner)):
        return service.memories.get(owner, memory_id)

    @router.patch('/memories/{memory_id}')
    def update_memory(memory_id: str, payload: MemoryUpdate, owner: str = Depends(write_owner)):
        return service.memories.update(owner, memory_id, payload)

    @router.delete('/memories/{memory_id}')
    def delete_memory(memory_id: str, expected_version: int = Query(ge=1), owner: str = Depends(write_owner)):
        return service.memories.delete(owner, memory_id, expected_version)

    @router.get('/memories/{memory_id}/versions')
    def memory_versions(memory_id: str, owner: str = Depends(session_owner)):
        return {'versions': service.memories.versions(owner, memory_id)}

    @router.post('/memories/{memory_id}/decision')
    def memory_decision(memory_id: str, payload: MemoryDecision, owner: str = Depends(write_owner)):
        return service.memories.decide(owner, memory_id, payload)

    @router.post('/runs/{run_id}/memory-proposals')
    def propose_memory(run_id: str, owner: str = Depends(write_owner)):
        return {'memories': service.memories.propose(owner, run_id)}

    @router.get('/runs/{run_id}/context')
    def context(run_id: str, owner: str = Depends(session_owner)):
        return {'snapshots': service.contexts.snapshots(owner, run_id), 'memories': service.memories.usages(owner, run_id)}

    @router.get('/runs/{run_id}/context/entries')
    def context_entries(run_id: str, after: int = Query(default=0, ge=0), limit: int = Query(default=200, ge=1, le=500), owner: str = Depends(session_owner)):
        service.contexts.capture(owner, run_id)
        return {'entries': service.contexts.entries(owner, run_id, after, limit)}

    @router.get('/context/{snapshot_id}')
    def context_snapshot(snapshot_id: str, owner: str = Depends(session_owner)):
        return service.contexts.read(owner, snapshot_id)

    @router.get('/evidence/{evidence_id}/slice')
    def evidence_slice(evidence_id: str, offset: int = Query(default=0, ge=0, le=100000), limit: int = Query(default=20, ge=1, le=100),
                       fields: str = Query(default='', max_length=400), owner: str = Depends(session_owner)):
        return service.contexts.evidence_slice(owner, evidence_id, offset, limit, [f.strip() for f in fields.split(',')] if fields else None)

    @router.post("/runs/{run_id}/tools/{tool_name}", response_model=ToolCallRecord)
    async def execute(run_id: str, tool_name: str, payload: ToolCallCreate, owner: str = Depends(write_owner)):
        return await service.execute(owner, run_id, tool_name, **payload.model_dump())

    @router.get("/calls/{call_id}")
    def call(call_id: str, owner: str = Depends(session_owner)):
        record = service.resource("tool_calls", call_id, owner)
        record["attempt_log"] = service.db.all("SELECT attempt,started_at,finished_at,elapsed_ms,http_status,business_code,request_id,error,cost_status FROM tool_attempts WHERE call_id=? ORDER BY attempt", (call_id,))
        return record

    @router.get("/runs/{run_id}/evidence")
    def evidence_for_run(run_id: str, owner: str = Depends(session_owner)):
        return {"evidence": service.collection(owner, run_id, "evidence")}

    @router.get("/evidence/{evidence_id}")
    def evidence(evidence_id: str, owner: str = Depends(session_owner)):
        record = service.resource("evidence", evidence_id, owner)
        record["raw_url"] = "/api/evidence/" + evidence_id + "/raw"
        return record

    @router.get("/evidence/{evidence_id}/raw")
    def raw(evidence_id: str, owner: str = Depends(session_owner)):
        record = service.resource("evidence", evidence_id, owner)
        return service.resource("raw_records", record["raw_id"], owner)

    @router.get("/metrics/{metric_id}")
    def metric(metric_id: str, owner: str = Depends(session_owner)):
        return service.resource("derived_metrics", metric_id, owner)

    @router.get("/runs/{run_id}/metrics")
    def metrics(run_id: str, owner: str = Depends(session_owner)):
        return {"metrics": service.collection(owner, run_id, "derived_metrics")}

    @router.get("/runs/{run_id}/events")
    def events(run_id: str, after: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=500),
               owner: str = Depends(session_owner)):
        rows = service.events(owner, run_id, after, limit)
        return {"events": rows, "next_cursor": rows[-1]["id"] if rows else after}

    @router.get("/runs/{run_id}/events/stream", response_class=StreamingResponse)
    async def stream(run_id: str, request: Request, after: int = Query(default=0, ge=0, le=9223372036854775807),
                     last_event_id: str | None = Header(default=None, alias="Last-Event-ID"), owner: str = Depends(session_owner)):
        service.resource("runs", run_id, owner)
        if last_event_id is not None:
            if not last_event_id.isdecimal() or len(last_event_id) > 19 or int(last_event_id) > 9223372036854775807:
                raise ServiceError(422, "事件游标格式无效")
            after = max(after, int(last_event_id))
        return StreamingResponse(run_stream(service, owner, run_id, after, request, request.state.session["expires_at"]),
                                 media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    return router


def install_api(app: FastAPI, settings: Settings, transport=None, ifind_client=None, model=None) -> ResearchService:
    service = ResearchService(settings, transport=transport, ifind_client=ifind_client)
    service.memories = MemoryService(service)
    service.contexts = ContextManager(service)
    service.artifacts = ArtifactService(service)
    service.harness = Harness(service, model)
    app.state.research = service
    app.state.harness = service.harness
    if hasattr(app, "on_startup"):
        app.on_startup(service.harness.start)
        app.on_shutdown(service.harness.close)
    else:
        previous_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(application):
            async with previous_lifespan(application):
                await service.harness.start()
                try:
                    yield
                finally:
                    await service.harness.close()

        app.router.lifespan_context = lifespan
    app.include_router(router_for(service))
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    if "/api/openapi.json" not in paths:
        @app.get("/api/openapi.json", include_in_schema=False)
        def schema():
            return app.openapi()
    if "/api/docs" not in paths:
        @app.get("/api/docs", include_in_schema=False)
        def documentation():
            return get_swagger_ui_html(openapi_url="/api/openapi.json", title="XBuddy API")
    app.add_middleware(SessionMiddleware, db=service.db, secure=settings.cookie_secure)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))

    @app.exception_handler(ServiceError)
    async def service_error(request, error: ServiceError):
        return JSONResponse(status_code=error.status, content={"detail": error.message})

    return service


def create_api(settings: Settings, transport=None, ifind_client=None, model=None) -> FastAPI:
    app = FastAPI(title="XBuddy research API", version="0.9.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
    install_api(app, settings, transport, ifind_client, model)
    return app
