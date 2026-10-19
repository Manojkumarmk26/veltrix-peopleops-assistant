"""FastAPI application entrypoint for Veltrix PeopleOps Assistant.

Implements the exact API contract specified in Section 9 of TASK.pdf:
- POST /chat: Authenticated conversational HR assistant endpoint
- GET /health: Healthcheck endpoint (200 when ready, 503 during ingestion)

Start-up work (database checks, ingestion or index reuse, model warm-up) runs
in a background thread so the server answers ``/health`` with 503 while it is
in progress and 200 once the assistant can serve requests.
"""

import asyncio
import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, Header, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictStr, ValidationError, field_validator

from app.config import get_settings
from app.data_access.database import init_database
from app.data_access.employee_repo import EmployeeRepository
from app.orchestration.chat_handler import ChatHandler
from app.orchestration.session import SessionOwnershipError
from app.privacy import configure_logging

configure_logging(get_settings().log_level)
logger = logging.getLogger(__name__)


class AppState:
    """Readiness and the shared chat handler."""

    def __init__(self) -> None:
        self.ready = False
        self.error: Optional[str] = None
        self.chat_handler: Optional[ChatHandler] = None
        self.thread: Optional[threading.Thread] = None


state = AppState()


def initialise_service() -> None:
    """Prepare everything the assistant needs; sets ``state.ready`` on success."""
    from app.ingestion.pipeline import ensure_index
    from app.retrieval.embeddings import embed_query
    from app.retrieval.retriever import Retriever

    settings = get_settings()
    started = time.monotonic()
    try:
        logger.info("Initialising Veltrix PeopleOps Assistant (today=%s)", settings.today)
        init_database(settings.db_path)
        vector_store = ensure_index()
        embed_query("warm-up")  # load the embedding model before the first request
        state.chat_handler = ChatHandler(retriever=Retriever(vector_store))
        state.ready = True
        logger.info(
            "Service ready in %.1fs (LLM configured: %s)",
            time.monotonic() - started, settings.llm_configured,
        )
        if not settings.llm_configured:
            logger.warning(
                "No API key for LLM provider '%s': document answers will be extractive.",
                settings.llm_provider,
            )
    except Exception as e:  # stay at 503 and log the cause
        state.error = type(e).__name__
        logger.error("Initialisation failed: %s", e, exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start initialisation in the background so /health can report 503 meanwhile."""
    configure_logging(get_settings().log_level)  # uvicorn handlers now exist
    state.thread = threading.Thread(target=initialise_service, name="init", daemon=True)
    state.thread.start()
    yield
    logger.info("Shutting down Veltrix PeopleOps Assistant")


app = FastAPI(
    title="Veltrix PeopleOps Assistant",
    version="1.0.0",
    description="HR policy assistant and leave management service",
    lifespan=lifespan,
)

_origins = [o.strip() for o in get_settings().cors_origins.split(",") if o.strip()]
if _origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-Employee-Id"],
    )


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Cache-Control", "no-store")
    return response


# ── Request / Response Models (Section 9.1) ─────────────────────────────────

class ChatRequest(BaseModel):
    """POST /chat request body."""

    session_id: StrictStr = Field(..., min_length=1, description="Unique session identifier chosen by client")
    message: StrictStr = Field(..., min_length=1, description="Message string from employee")

    @field_validator("session_id", "message")
    @classmethod
    def _not_blank(cls, value: str, info) -> str:
        settings = get_settings()
        if not value.strip():
            raise ValueError("must not be blank")
        limit = settings.max_session_id_chars if info.field_name == "session_id" else settings.max_message_chars
        if len(value) > limit:
            raise ValueError(f"must be at most {limit} characters")
        return value


class Citation(BaseModel):
    """Document citation."""

    doc_id: str
    locator: str


class ToolCall(BaseModel):
    """Tool invocation record."""

    name: str
    args: dict[str, Any]
    status: str  # "pending_confirmation" | "success" | "error"


class ChatResponse(BaseModel):
    """POST /chat response body matching Section 9.1 exact schema."""

    request_id: str
    answer: str
    route: str  # "rag" | "tool" | "hybrid" | "clarify" | "no_answer" | "refuse"
    citations: list[Citation] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    pending_confirmation: bool = False


def _error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail})


# ── Exception Handlers ──────────────────────────────────────────────────────

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Return HTTP 400 when request body is missing or invalid (Section 9.1)."""
    return _error(status.HTTP_400_BAD_REQUEST, "The request body is missing or invalid.")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled error on %s: %s", request.url.path, type(exc).__name__, exc_info=exc)
    return _error(status.HTTP_500_INTERNAL_SERVER_ERROR, "An internal error occurred.")


# ── Endpoints ───────────────────────────────────────────────────────────────

@app.get("/health", summary="Healthcheck endpoint")
async def health():
    """Return HTTP 200 with status ok only when service is ready (Section 9.2).

    Until ingestion finishes and service is ready, returns HTTP 503.
    """
    if not state.ready:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "error" if state.error else "starting"},
        )
    return {"status": "ok"}


@app.post(
    "/chat",
    response_model=ChatResponse,
    summary="Main conversational chat endpoint",
    responses={
        200: {"description": "Successful conversational response"},
        400: {"description": "The body is missing or invalid"},
        401: {"description": "X-Employee-Id is missing or not in the database"},
        403: {"description": "The session_id belongs to another employee"},
        503: {"description": "The service is still starting"},
    },
)
async def chat(
    request: Request,
    x_employee_id: Optional[str] = Header(None, alias="X-Employee-Id"),
):
    """Chat with the Veltrix PeopleOps Assistant."""
    if not state.ready or state.chat_handler is None:
        return _error(status.HTTP_503_SERVICE_UNAVAILABLE, "Service is starting.")

    # 1. Authentication: the header must name an employee in the database (401).
    employee_id = (x_employee_id or "").strip()
    if not employee_id:
        return _error(status.HTTP_401_UNAUTHORIZED, "X-Employee-Id header is required.")
    requester = await run_in_threadpool(EmployeeRepository().get_by_id, employee_id)
    if requester is None:
        return _error(status.HTTP_401_UNAUTHORIZED, "Unknown employee.")

    # 2. Body validation (400).
    try:
        payload = ChatRequest.model_validate(await request.json())
    except (ValueError, ValidationError):  # JSONDecodeError is a ValueError
        return _error(status.HTTP_400_BAD_REQUEST, "The request body is missing or invalid.")

    # 3. Process (403 for someone else's session), within the request deadline.
    settings = get_settings()
    try:
        result = await asyncio.wait_for(
            run_in_threadpool(
                state.chat_handler.handle_message,
                session_id=payload.session_id,
                requester=requester,
                message=payload.message,
            ),
            timeout=settings.request_timeout_seconds,
        )
    except SessionOwnershipError:
        return _error(status.HTTP_403_FORBIDDEN, "This session belongs to another employee.")
    except asyncio.TimeoutError:
        logger.error("Request exceeded %.0fs deadline", settings.request_timeout_seconds)
        return ChatResponse(
            request_id=str(uuid.uuid4()),
            answer="Sorry, that took too long to answer. Please try again, or contact the HR team.",
            route="no_answer",
        )
    return ChatResponse(**result)
