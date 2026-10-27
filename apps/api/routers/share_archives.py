import logging
import threading
from typing import Optional
from urllib.parse import quote
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlalchemy.orm import Session
import anyio
from starlette.concurrency import run_in_threadpool

from ..database import get_db
from ..middleware.auth import get_optional_user
from ..middleware.rate_limit import rate_limit
from ..models.user import User
from ..services.permissions import validate_share_link_with_session
from ..services.share_archives import prepare_archive, load_job, public_status, stream_archive, fail_job

router = APIRouter(tags=["sharing"])
logger = logging.getLogger(__name__)


class ArchiveRequest(BaseModel):
    folder_id: Optional[uuid.UUID] = None


class _ArchiveBody:
    def __init__(self, first, stream):
        self.first = first
        self.stream = stream
        self.lock = threading.Lock()

    def __iter__(self):
        return self

    def __next__(self):
        with self.lock:
            if self.first is not None:
                first, self.first = self.first, None
                return first
            return next(self.stream)

    def close(self):
        # A cancelled ASGI task can leave a storage read running in a worker.
        # Serialize closing with next(), rather than closing an executing generator.
        with self.lock:
            self.stream.close()


class ArchiveResponse(StreamingResponse):
    def __init__(self, first, stream, **kwargs):
        self.archive_body = _ArchiveBody(first, stream)
        super().__init__(self.archive_body, **kwargs)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # BackgroundTask is skipped when streaming or ASGI send raises.
            with anyio.CancelScope(shield=True):
                await run_in_threadpool(self.archive_body.close)


@router.post("/share/{token}/archives", dependencies=[Depends(rate_limit("share_archive", 2, 60))])
def prepare_share_archive(
    token: str, body: ArchiveRequest, share_session: Optional[str] = Query(None),
    db: Session = Depends(get_db), current_user: Optional[User] = Depends(get_optional_user),
):
    try:
        return public_status(prepare_archive(db, token, share_session, current_user, body.folder_id))
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Could not prepare guest ZIP download")
        raise HTTPException(503, "Could not prepare the ZIP; please try again") from exc


@router.get("/share/{token}/archives/{job_id}")
def share_archive_status(
    token: str, job_id: uuid.UUID, share_session: Optional[str] = Query(None),
    db: Session = Depends(get_db), current_user: Optional[User] = Depends(get_optional_user),
):
    validate_share_link_with_session(db, token, share_session=share_session, current_user=current_user)
    return public_status(load_job(job_id, token, share_session, current_user))


@router.post("/share/{token}/archives/{job_id}/download")
def download_share_archive(
    token: str, job_id: uuid.UUID, share_session: Optional[str] = Form(None),
    access_token: Optional[str] = Form(None), db: Session = Depends(get_db),
):
    # Native form downloads stream to the browser's download manager. Form fields
    # preserve secure-link auth without putting an access token in a URL.
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=access_token) if access_token else None
    user = get_optional_user(credentials=credentials, db=db)
    job = load_job(job_id, token, share_session, user)
    if job["status"] != "ready":
        raise HTTPException(409, "This ZIP request has already started; prepare a new download")
    try:
        validate_share_link_with_session(db, token, share_session=share_session, current_user=user)
        stream = stream_archive(db, job, token, share_session, user)
        first = next(stream)
    except Exception as exc:
        if not isinstance(exc, HTTPException):
            logger.exception("Could not start guest ZIP download")
        if job["status"] == "downloading" or not isinstance(exc, HTTPException) or exc.status_code != 409:
            fail_job(job, exc.detail if isinstance(exc, HTTPException) else "ZIP download could not start; please try again")
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(503, job["error"]) from exc
    return ArchiveResponse(
        first, stream, media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=\"download.zip\"; filename*=UTF-8''" + quote(job["filename"]),
                 "Cache-Control": "no-store", "X-Accel-Buffering": "no", "Accept-Ranges": "none"},
    )
