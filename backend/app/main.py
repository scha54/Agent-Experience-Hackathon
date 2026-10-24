"""FastAPI application entry point and request safety middleware.

The application is deliberately kept free of request-body logging or storage.
The middleware reads a request body only long enough to enforce the configured
size cap and then either rejects it or replays it to the selected router.

Requirements: 1.7, 15.1-15.3, 26.4-26.6, 26.8, 27.3-27.4.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
from contextlib import asynccontextmanager
from threading import Lock
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import ConfigError, get_settings
from app.logging_config import configure_logging, get_logger
from app.models.responses import FieldError, ValidationErrorResponse


_SAFE_ERROR_MESSAGES: dict[int, str] = {
    400: "The request could not be processed.",
    401: "Authentication is required.",
    403: "The request is not permitted.",
    404: "The requested resource was not found.",
    405: "The requested method is not allowed.",
    413: "Request body exceeds the configured size limit.",
    415: "The request content type is not supported.",
    429: "The request limit for this session has been reached.",
    500: "An unexpected server error occurred.",
    502: "An upstream service is unavailable.",
    503: "The service is temporarily unavailable.",
    504: "The request timed out.",
}

_RECIPE_ENDPOINTS = {
    "/api/recipes/generate",
    "/api/recipes/regenerate",
}
_ROUTER_MODULES = (
    "app.api.health",
    "app.api.recipes",
    "app.api.metrics",
)


async def _send_safe_error(send: Send, status_code: int, message: str) -> None:
    """Send a minimal error response without echoing request or exception data."""
    payload = {"error": "request_rejected", "message": message}
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class RequestBodyLimitMiddleware:
    """Reject oversized bodies before a router can parse or retain their input.

    The global cap applies to every HTTP request. Recipe generation endpoints
    use the smaller endpoint cap. Bodies are buffered only for the duration of
    the current request so downstream FastAPI parsing still works; no body is
    copied to application state, metrics, or logs.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        global_limit_bytes: int,
        endpoint_limit_bytes: int,
    ) -> None:
        self.app = app
        self.global_limit_bytes = global_limit_bytes
        self.endpoint_limit_bytes = endpoint_limit_bytes

    @staticmethod
    def _header_value(scope: Scope, name: bytes) -> int | None:
        for key, value in scope.get("headers", []):
            if key.lower() == name:
                try:
                    parsed = int(value)
                except (TypeError, ValueError):
                    return None
                return parsed if parsed >= 0 else None
        return None

    def _limit_for_scope(self, scope: Scope) -> int:
        global_limit = self.global_limit_bytes
        path = scope.get("path", "").rstrip("/") or "/"
        method = scope.get("method", "").upper()
        if method == "POST" and path in _RECIPE_ENDPOINTS:
            return min(global_limit, self.endpoint_limit_bytes)
        return global_limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        limit = self._limit_for_scope(scope)
        content_length = self._header_value(scope, b"content-length")
        if content_length is not None and content_length > limit:
            await _send_safe_error(send, 413, _SAFE_ERROR_MESSAGES[413])
            return

        chunks: list[bytes] = []
        received_bytes = 0
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                # Let the downstream app observe a disconnected request without
                # retaining any partial user input here.
                await self.app(scope, receive, send)
                return

            if message.get("type") != "http.request":
                continue

            chunk = message.get("body", b"")
            received_bytes += len(chunk)
            if received_bytes > limit:
                # ``chunks`` is local and is released as soon as this response
                # returns. It is never persisted or included in the error.
                await _send_safe_error(send, 413, _SAFE_ERROR_MESSAGES[413])
                return

            chunks.append(chunk)
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay_receive() -> Message:
            nonlocal replayed
            if replayed:
                return {"type": "http.disconnect"}
            replayed = True
            return {
                "type": "http.request",
                "body": b"".join(chunks),
                "more_body": False,
            }

        await self.app(scope, replay_receive, send)


class SessionRequestLimitMiddleware:
    """Apply an in-memory request cap to recipe-generation sessions.

    The client supplies an opaque ``X-Session-ID`` header. When absent, the
    client address is used only as an anonymous bucket. Only a SHA-256 digest
    is retained in the process-local counter, never the raw identifier or any
    request body. Health, CORS preflight, and non-generation routes are not
    generation requests and do not consume this budget.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_requests: int,
        session_counts: dict[str, int],
        lock: Lock,
    ) -> None:
        self.app = app
        self.max_requests = max_requests
        self.session_counts = session_counts
        self.lock = lock

    @staticmethod
    def _header(scope: Scope, name: bytes) -> str | None:
        for key, value in scope.get("headers", []):
            if key.lower() == name:
                return value.decode("utf-8", errors="ignore")
        return None

    def _session_key(self, scope: Scope) -> str:
        supplied = self._header(scope, b"x-session-id")
        if supplied is None:
            client = scope.get("client")
            supplied = f"anonymous:{client[0]}" if client else "anonymous:unknown"
        # Retain no caller-provided identifier in process state.
        return hashlib.sha256(supplied.encode("utf-8")).hexdigest()

    @staticmethod
    def _is_generation_request(scope: Scope) -> bool:
        path = scope.get("path", "").rstrip("/") or "/"
        return scope.get("method", "").upper() == "POST" and path in _RECIPE_ENDPOINTS

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._is_generation_request(scope):
            await self.app(scope, receive, send)
            return

        session_key = self._session_key(scope)
        rejected = False
        with self.lock:
            current_count = self.session_counts.get(session_key, 0)
            if current_count >= self.max_requests:
                rejected = True
            else:
                self.session_counts[session_key] = current_count + 1

        if rejected:
            await _send_safe_error(
                send, 429, _SAFE_ERROR_MESSAGES[429]
            )
            return

        await self.app(scope, receive, send)


def _safe_field_name(location: tuple[Any, ...]) -> str:
    """Convert a validation location into a non-sensitive field path."""
    parts: list[str] = []
    for part in location:
        if part in {"body", "query", "path", "header", "cookie"}:
            continue
        text = re.sub(r"[^A-Za-z0-9_.-]", "_", str(part))
        if text:
            parts.append(text)
    return ".".join(parts) or "request"


def _safe_validation_message(error_type: str) -> str:
    """Map Pydantic error types to generic messages without echoing input."""
    error_type = error_type.rsplit(".", 1)[-1]
    if error_type == "missing":
        return "This field is required."
    if error_type in {
        "literal_error",
        "enum",
        "string_type",
        "list_type",
        "int_type",
        "bool_type",
        "json_invalid",
    }:
        return "This field has an invalid value."
    if error_type in {
        "greater_than",
        "greater_than_equal",
        "less_than",
        "less_than_equal",
        "multiple_of",
    }:
        return "This field is outside the allowed range."
    if "length" in error_type or "too_long" in error_type:
        return "This field exceeds the configured limit."
    return "This field is invalid."


def _validation_response(exc: RequestValidationError) -> ValidationErrorResponse:
    """Create a safe field-level response from validation metadata only."""
    fields = [
        FieldError(
            field=_safe_field_name(tuple(error.get("loc", ()))),
            message=_safe_validation_message(str(error.get("type", "invalid"))),
        )
        for error in exc.errors()
    ]
    return ValidationErrorResponse(fields=fields)


def _configure_routers(application: FastAPI) -> None:
    """Include whichever API routers are available in the current build."""
    for module_name in _ROUTER_MODULES:
        module = importlib.import_module(module_name)
        router = getattr(module, "router", None)
        if router is not None:
            application.include_router(router)


def _configure_error_handlers(application: FastAPI) -> None:
    @application.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        del request
        response = _validation_response(exc)
        return JSONResponse(
            status_code=422,
            content=response.model_dump(mode="json", exclude_none=True),
        )

    @application.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        del request
        message = _SAFE_ERROR_MESSAGES.get(
            exc.status_code, "The request could not be processed."
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": "request_rejected", "message": message},
        )

    @application.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        del request
        # OE-2: log a SAFE error-category line server-side. ``logger.exception``
        # records the traceback for CloudWatch debugging (it never reaches the
        # client), while the structured safe field carries only the exception
        # CLASS NAME — never the message, prompts, credentials, or request
        # values. The HTTP response stays exactly as before (non-leaking).
        get_logger().exception(
            "unhandled_exception",
            extra={"error_category": type(exc).__name__},
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_error",
                "message": _SAFE_ERROR_MESSAGES[500],
            },
        )


def _cors_origins_from_environment() -> list[str]:
    """Read origins for the import-safe bootstrap app without echoing secrets."""
    raw = os.getenv("ALLOWED_ORIGINS", "")
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


@asynccontextmanager
async def _validate_deferred_configuration(application: FastAPI):
    """Validate required configuration when the import-safe app starts."""
    application.state.settings = get_settings()
    yield


def create_app(settings: Any | None = None) -> FastAPI:
    """Create a configured FastAPI application.

    ``settings`` is injectable for tests and deployments that construct config
    outside the process environment. When omitted, ``get_settings`` performs
    the normal startup validation and raises ``ConfigError`` for missing or
    invalid required variables.
    """
    resolved_settings = settings if settings is not None else get_settings()
    # OE-2: configure structured JSON logging once at startup (idempotent).
    configure_logging()
    application = FastAPI(
        title="CraveQuest Backend API",
        version="0.1.0",
    )
    application.state.settings = resolved_settings
    application.state.session_counts = {}
    application.state.session_lock = Lock()

    _configure_routers(application)
    _configure_error_handlers(application)

    # Register the inner limiter first, then the body limiter. Starlette wraps
    # later registrations outside earlier ones, so an oversized body is
    # rejected before it can consume the per-session generation budget.
    application.add_middleware(
        SessionRequestLimitMiddleware,
        max_requests=resolved_settings.max_session_requests,
        session_counts=application.state.session_counts,
        lock=application.state.session_lock,
    )
    application.add_middleware(
        RequestBodyLimitMiddleware,
        global_limit_bytes=resolved_settings.max_global_body_bytes,
        endpoint_limit_bytes=resolved_settings.max_body_bytes,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_settings.allowed_origins_list,
        allow_credentials=False,
        # SEC-3: tighten CORS to the actual methods/headers the API uses instead
        # of the wildcard. Keep allow_credentials=False.
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["content-type", "authorization"],
    )
    return application


def _build_import_safe_app() -> FastAPI:
    """Keep package imports usable while preserving startup config validation.

    The repository's package smoke test imports ``app.main`` without a local
    ``.env``. Production construction still uses ``create_app`` and fails fast;
    the fallback instance defers that same validation to its lifespan.
    """
    try:
        return create_app()
    except ConfigError:
        configure_logging()
        application = FastAPI(
            title="CraveQuest Backend API",
            version="0.1.0",
            lifespan=_validate_deferred_configuration,
        )
        application.state.settings = None
        application.state.session_counts = {}
        application.state.session_lock = Lock()
        _configure_routers(application)
        _configure_error_handlers(application)
        application.add_middleware(
            SessionRequestLimitMiddleware,
            max_requests=20,
            session_counts=application.state.session_counts,
            lock=application.state.session_lock,
        )
        application.add_middleware(
            RequestBodyLimitMiddleware,
            global_limit_bytes=262144,
            endpoint_limit_bytes=65536,
        )
        application.add_middleware(
            CORSMiddleware,
            allow_origins=_cors_origins_from_environment(),
            allow_credentials=False,
            # SEC-3: tighten CORS to the actual methods/headers used.
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["content-type", "authorization"],
        )
        return application


app = _build_import_safe_app()

__all__ = [
    "app",
    "create_app",
    "RequestBodyLimitMiddleware",
    "SessionRequestLimitMiddleware",
]
