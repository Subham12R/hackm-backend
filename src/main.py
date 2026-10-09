import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager

from cryptography.hazmat.primitives.serialization import load_pem_private_key, load_pem_public_key
from dotenv import load_dotenv

if "pytest" not in sys.modules:
    load_dotenv()
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from src.api.auth import auth_router
from src.api.admin import admin_router
from src.api.admin_checkin import admin_checkin_router
from src.api.admin_event import admin_event_router
from src.api.admin_gallery import admin_gallery_router
from src.api.admin_judging import admin_judging_router
from src.api.admin_ops import admin_ops_router
from src.api.admin_payments import admin_payments_router
from src.api.judge import judge_router
from src.api.public import router as public_router
from src.api.participant import participant_router
from src.api.registrations import router as registration_router
from src.payments.providers.razorpay import client as razorpay_client
from src.communications.providers.resend import ResendEmailSender
from src.communications.providers.sink import LocalEmailSink
from src.api.errors import ApiError
from src.cache import cache_from_env
from src.communications.outbox import dispatch_pending
from src.api.middleware import request_id_middleware
from src.config import Settings, decode_pem_env
from src.db.session import create_db_engine
from src.storage.s3 import Storage, storage_from_env


def create_app(
    database_url: str | None = None,
    jwt_private_key: str | None = None,
    jwt_public_key: str | None = None,
    email_sender: LocalEmailSink | ResendEmailSender | None = None,
    payment_client=None,
    razorpay_webhook_secret: str | None = None,
    razorpay_key_id: str | None = None,
    storage: Storage | None = None,
) -> FastAPI:
    settings = Settings(
        database_url=database_url
        or os.getenv("DATABASE_URL")
        or os.getenv("NEON_DATABASE_URL")
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = None
        if app.state.database_url and os.getenv("EMAIL_OUTBOX_WORKER", "1") == "1":
            task = asyncio.create_task(_outbox_loop(app))
        yield
        if task is not None:
            task.cancel()

    app = FastAPI(title="HackMatrix API", lifespan=lifespan)
    app.state.cache = cache_from_env()
    app.state.storage = storage or storage_from_env()
    app.state.database_url = settings.database_url
    app.state.razorpay_key_id = razorpay_key_id or os.getenv("RAZORPAY_KEY_ID")
    app.state.razorpay_webhook_secret = razorpay_webhook_secret or os.getenv("RAZORPAY_WEBHOOK_SECRET")
    app.state.payment_client = payment_client or (
        razorpay_client(app.state.razorpay_key_id, os.environ["RAZORPAY_KEY_SECRET"])
        if app.state.razorpay_key_id and os.getenv("RAZORPAY_KEY_SECRET")
        else None
    )
    app.state.email_sender = email_sender or (
        ResendEmailSender(os.environ["RESEND_API_KEY"], os.environ["EMAIL_FROM"])
        if os.getenv("RESEND_API_KEY") and os.getenv("EMAIL_FROM")
        else LocalEmailSink()
    )
    app.include_router(registration_router)
    app.include_router(public_router)
    app.middleware("http")(request_id_middleware)

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, error: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content={
                "error": {
                    "code": error.code,
                    "request_id": request.state.request_id,
                }
            },
        )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        if not settings.database_url:
            raise ApiError(503, "database_unavailable")
        try:
            with create_db_engine(settings.database_url).connect() as connection:
                connection.execute(text("SELECT 1"))
        except (SQLAlchemyError, OSError):
            raise ApiError(503, "database_unavailable") from None
        return {"status": "ok"}

    private_pem = jwt_private_key or decode_pem_env(os.getenv("JWT_PRIVATE_KEY_PEM"))
    public_pem = jwt_public_key or decode_pem_env(os.getenv("JWT_PUBLIC_KEY_PEM"))
    if private_pem and public_pem:
        public_key = load_pem_public_key(public_pem.encode())
        app.include_router(
            auth_router(
                load_pem_private_key(private_pem.encode(), password=None),
                public_key,
                app.state.email_sender,
            )
        )
        app.include_router(participant_router(public_key))
        app.include_router(admin_router(public_key))
        app.include_router(admin_payments_router(public_key))
        app.include_router(admin_event_router(public_key))
        app.include_router(admin_judging_router(public_key))
        app.include_router(admin_ops_router(public_key))
        app.include_router(admin_gallery_router(public_key))
        app.include_router(admin_checkin_router(public_key))
        app.include_router(judge_router(public_key))

    return app


async def _outbox_loop(app: FastAPI) -> None:
    """Retry emails whose immediate post-commit delivery failed."""
    while True:
        await asyncio.sleep(60)
        try:
            await asyncio.to_thread(dispatch_pending, app.state.database_url, app.state.email_sender)
        except Exception:
            logging.getLogger(__name__).warning("outbox retry pass failed", exc_info=True)


app = create_app()
