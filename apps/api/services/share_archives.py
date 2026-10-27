"""Bounded-memory ZIP downloads; Redis holds only short-lived manifests/status."""
from collections import defaultdict, deque
import hashlib
from functools import lru_cache
import json
import logging
import os
import re
import threading
import time
import unicodedata
import uuid
import zipfile

from fastapi import HTTPException
from botocore.config import Config
from redis import Redis, RedisError
from redis.backoff import NoBackoff
from redis.retry import Retry
from sqlalchemy import or_

from ..config import settings
from ..models.asset import Asset
from ..models.folder import Folder
from ..models.share import ShareLinkItem
from ..models.user import User, UserStatus
from .downloads import original_allowed_for_download, pick_or_refuse
from .permissions import (
    _is_descendant_of, folder_chain_is_live, validate_asset_in_share,
    validate_share_link_with_session,
)
from .s3_service import _build_s3_client, build_download_filename

CHUNK_SIZE = 256 * 1024
JOB_TTL = 4 * 3600
START_TTL = 300
SLOT_LEASE_TTL = 90
SLOT_REFRESH_INTERVAL = 20
logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def archive_redis():
    return Redis.from_url(settings.redis_url, decode_responses=True,
                          socket_connect_timeout=5, socket_timeout=5,
                          retry=Retry(NoBackoff(), 0))


@lru_cache(maxsize=1)
def archive_storage():
    return _build_s3_client(Config(connect_timeout=5, read_timeout=15, retries={"total_max_attempts": 2, "mode": "standard"}))


def safe_component(value):
    value = unicodedata.normalize("NFC", value)
    value = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', "_", value).strip(" .")
    if not value:
        value = "Untitled"
    if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", value, re.I):
        value = "_" + value
    stem, extension = os.path.splitext(value)
    while len((stem + extension).encode("utf-8")) > 180:
        if len(stem) > 1:
            stem = stem[:-1]
        else:
            extension = extension[:-1]
    return stem + extension


def unique_component(name, used):
    name = safe_component(name)
    stem, extension = os.path.splitext(name)
    candidate = name
    index = 2
    while candidate.casefold() in used:
        candidate = f"{stem} ({index}){extension}"
        index += 1
    used.add(candidate.casefold())
    return candidate


def owner_key(token, session, user):
    return hashlib.sha256(json.dumps([token, session or "", str(user.id) if user else "" ]).encode()).hexdigest()


def job_key(job_id):
    return "share_archive:" + str(job_id)


def save_job(job):
    saved = {key: value for key, value in job.items() if key != "files"}
    redis = archive_redis()
    redis.setex(job_key(job["id"]), JOB_TTL if job["status"] == "downloading" else START_TTL, json.dumps(saved))
    if job["status"] in ("complete", "failed"):
        redis.delete(job_key(job["id"]) + ":manifest")


def load_job(job_id, token, session, user):
    redis = archive_redis()

    def read_owned_job():
        raw = redis.get(job_key(job_id))
        current = json.loads(raw) if raw else None
        if not current or current["owner"] != owner_key(token, session, user):
            raise HTTPException(404, "ZIP download not found or expired")
        return current

    job = read_owned_job()
    if job["status"] == "downloading" and job.get("slot") and redis.get(job["slot"]) != job["id"]:
        # A completed stream may have saved its final status between these reads.
        # Return inferred failure without overwriting a concurrent terminal update.
        job = read_owned_job()
        if job["status"] == "downloading":
            job.update(status="failed", error="The server could not confirm this ZIP download. Check your browser's Downloads before trying again.")
    return job


def public_status(job):
    return {key: job[key] for key in ("id", "status", "file_count", "completed_files", "filename", "error")}


def _scope(db, link, folder_id):
    if link.project_id:
        project_id = link.project_id
    elif link.folder_id:
        project_id = db.query(Folder).filter(Folder.id == link.folder_id).one().project_id
    else:
        raise HTTPException(400, "ZIP downloads require a folder or project share")
    folders = db.query(Folder).filter(
        Folder.project_id == project_id, Folder.deleted_at.is_(None),
    ).all()
    by_id = {f.id: f for f in folders}
    items = db.query(ShareLinkItem).filter(ShareLinkItem.share_link_id == link.id).all() if link.project_id else []
    selected_folders = {item.folder_id for item in items if item.folder_id}
    selected_assets = {item.asset_id for item in items if item.asset_id}
    target = folder_id or link.folder_id
    if target:
        folder = by_id.get(target)
        if not folder or not folder_chain_is_live(db, target):
            raise HTTPException(403, "Folder is not within the shared content")
        if link.folder_id and target != link.folder_id and not _is_descendant_of(db, target, link.folder_id):
            raise HTTPException(403, "Folder is not within the shared folder")
        if items and not any(target == f or _is_descendant_of(db, target, f) for f in selected_folders):
            raise HTTPException(403, "Folder is not in the shared items")
        roots = [(target, "")]
        selected_assets = set()
    elif items:
        roots = []
        for folder_id in sorted(selected_folders, key=str):
            if folder_id not in by_id or not folder_chain_is_live(db, folder_id):
                continue
            if any(folder_id != other and _is_descendant_of(db, folder_id, other) for other in selected_folders):
                continue
            roots.append((folder_id, None))
    else:
        roots = [(None, "")]
    children = defaultdict(list)
    for folder in folders:
        children[folder.parent_id].append(folder)
    used = defaultdict(set)
    paths = {}
    queue = deque(roots)
    while queue:
        current, path = queue.popleft()
        if current in paths:
            continue
        if path is None:
            path = unique_component(by_id[current].name, used[""])
        paths[current] = path
        for child in sorted(children[current], key=lambda f: (f.name.casefold(), str(f.id))):
            name = unique_component(child.name, used[path])
            queue.append((child.id, f"{path}/{name}" if path else name))
    return project_id, paths, selected_assets, used


def prepare_archive(db, token, session, user, folder_id):
    from ..routers.share import _get_latest_media_file
    link = validate_share_link_with_session(db, token, share_session=session, current_user=user)
    if not link.allow_download:
        raise HTTPException(403, "Downloads are not allowed for this share link")
    project_id, paths, selected_assets, used = _scope(db, link, folder_id)
    criteria = [Asset.folder_id.in_([fid for fid in paths if fid is not None]), Asset.id.in_(selected_assets)]
    if None in paths:
        criteria.append(Asset.folder_id.is_(None))
    assets = db.query(Asset).filter(
        Asset.project_id == project_id, Asset.deleted_at.is_(None), or_(*criteria),
    ).order_by(Asset.name, Asset.id).limit(settings.share_archive_max_files + 1).all()
    if len(assets) > settings.share_archive_max_files:
        raise HTTPException(413, "Too many files for one ZIP; download a smaller folder")
    files = []
    live_folders = {}
    allow_original = original_allowed_for_download(db, assets[0], user, None, True) if assets else False
    for asset in assets:
        if asset.folder_id:
            if asset.folder_id not in live_folders:
                live_folders[asset.folder_id] = folder_chain_is_live(db, asset.folder_id)
            if not live_folders[asset.folder_id]:
                continue
        validate_asset_in_share(db, link, asset)
        media = _get_latest_media_file(db, asset.id)
        if not media:
            raise HTTPException(409, "Some files are still processing, failed, or unavailable; ask the owner to check them before downloading the full ZIP")
        choice = pick_or_refuse(asset, media, None, prefer_original=True,
            allow_original=allow_original)
        parent = paths.get(asset.folder_id, "")
        filename = build_download_filename(asset.name, choice.s3_key, media.original_filename)
        if choice.id == "transcode":
            stem, extension = os.path.splitext(filename)
            filename = f"{stem} (transcode){extension}"
        name = unique_component(filename, used[parent])
        files.append({"asset_id": str(asset.id), "version_id": str(media.version_id), "key": choice.s3_key,
                      "path": f"{parent}/{name}" if parent else name})
    if not files:
        raise HTTPException(404, "There are no ready files to download in this folder")
    job = {"id": str(uuid.uuid4()), "owner": owner_key(token, session, user),
           "folder_id": str(folder_id) if folder_id else None,
           "status": "ready", "created_at": time.time(), "files": files,
           "filename": safe_component(link.title or "Shared media") + ".zip",
           "file_count": len(files), "completed_files": 0, "error": None}
    archive_redis().setex(job_key(job["id"]) + ":manifest", START_TTL, json.dumps(files))
    save_job(job)
    return job


def fail_job(job, message):
    job.update(status="failed", error=message)
    save_job(job)


def _save_stream_status(job):
    attempts = 3 if job["status"] in ("complete", "failed") else 1
    for attempt in range(attempts):
        try:
            save_job(job)
            return
        except RedisError:
            if attempt + 1 < attempts:
                time.sleep(0.1 * 2 ** attempt)
    # Progress outages must not truncate a valid ZIP or prevent lease cleanup.
    logger.warning("Could not update ZIP download status for %s", job["id"])


class _StreamLease:
    def __init__(self, redis, key, owner, acquired_at=None):
        self.redis, self.key, self.owner = redis, key, owner
        acquired_at = time.monotonic() if acquired_at is None else acquired_at
        self.deadline = acquired_at + SLOT_LEASE_TTL
        self.expires_at = acquired_at + JOB_TTL
        self.stopped = threading.Event()
        self.lost = threading.Event()
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)

    def start(self):
        self.thread.start()

    def _renew(self):
        began = time.monotonic()
        try:
            renewed = self.redis.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('expire', KEYS[1], ARGV[2]) end return 0",
                1, self.key, self.owner, SLOT_LEASE_TTL,
            )
        except RedisError:
            return
        if renewed:
            self.deadline = began + SLOT_LEASE_TTL
        else:
            self.lost.set()

    def _heartbeat(self):
        # Renew even while the browser applies backpressure and iteration is paused.
        while not self.stopped.wait(SLOT_REFRESH_INTERVAL):
            if time.monotonic() >= min(self.deadline, self.expires_at):
                self.lost.set()
                return
            self._renew()
            if self.lost.is_set():
                return

    def check(self):
        if self.lost.is_set() or time.monotonic() >= min(self.deadline, self.expires_at):
            raise HTTPException(503, "ZIP download lost its server slot; please try again")

    def close(self):
        self.stopped.set()
        try:
            self.redis.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end return 0",
                1, self.key, self.owner,
            )
        except RedisError:
            # The short lease recovers capacity if Redis is unavailable during cleanup.
            logger.warning("Could not release ZIP download slot for %s", self.owner)


class _Sink:
    def __init__(self):
        self.chunks = deque()

    def write(self, data):
        if data:
            self.chunks.append(data)
        return len(data)

    def flush(self):
        pass

    def drain(self):
        while self.chunks:
            yield self.chunks.popleft()


def zip_chunks(files, open_file, check_access, completed):
    """At most one source chunk plus ZIP directory metadata is buffered."""
    sink = _Sink()
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for item in files:
            check_access(item)
            obj = open_file(item)
            body = obj["Body"]
            count = 0
            checked = time.monotonic()
            try:
                size = obj["ContentLength"]
                with archive.open(item["path"], "w", force_zip64=True) as target:
                    yield from sink.drain()
                    while True:
                        chunk = body.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        if time.monotonic() - checked >= 15:
                            check_access(item)
                            checked = time.monotonic()
                        count += len(chunk)
                        if count > size:
                            raise IOError("Storage object changed during ZIP download")
                        target.write(chunk)
                        yield from sink.drain()
                    if count != size:
                        raise IOError("Incomplete storage object")
                yield from sink.drain()
                completed()
            finally:
                body.close()
    yield from sink.drain()


def stream_archive(db, job, token, session, user):
    from ..routers.share import _get_latest_media_file
    redis = archive_redis()
    if job["status"] != "ready" or time.time() - job["created_at"] > START_TTL:
        raise HTTPException(409, "This ZIP request has expired or already started; try again")
    raw = redis.get(job_key(job["id"]) + ":manifest")
    if raw is None:
        raise HTTPException(409, "This ZIP request has expired; prepare a new download")
    files = json.loads(raw)
    if not redis.set(job_key(job["id"]) + ":started", "1", nx=True, ex=JOB_TTL):
        raise HTTPException(409, "This ZIP download has already started")
    slot = None
    for index in range(settings.share_archive_max_streams):
        key = "share_archive:slot:" + str(index)
        acquired_at = time.monotonic()
        if redis.set(key, job["id"], nx=True, ex=SLOT_LEASE_TTL):
            slot = key
            break
    if slot is None:
        fail_job(job, "The server is busy downloading ZIPs; try again shortly")
        raise HTTPException(429, job["error"])
    lease = _StreamLease(redis, slot, job["id"], acquired_at)
    try:
        lease.start()
        job.update(status="downloading", slot=slot)
        save_job(job)
        redis.delete(job_key(job["id"]) + ":manifest")
    except BaseException:
        lease.close()
        raise
    actor_id = user.id if user else None
    token_version = user.token_version if user else None
    started = time.monotonic()

    def check(item):
        lease.check()
        if time.monotonic() - started >= JOB_TTL:
            raise HTTPException(408, "ZIP download timed out; download a smaller folder")
        db.expire_all()
        actor = db.query(User).filter(User.id == actor_id, User.deleted_at.is_(None)).first() if actor_id else None
        if actor_id and (not actor or actor.status == UserStatus.deactivated or actor.token_version != token_version):
            raise HTTPException(403, "Your access changed; sign in and try again")
        link = validate_share_link_with_session(db, token, share_session=session, current_user=actor)
        if not link.allow_download:
            raise HTTPException(403, "Downloads are no longer allowed for this share link")
        asset = db.query(Asset).filter(Asset.id == uuid.UUID(item["asset_id"]), Asset.deleted_at.is_(None)).first()
        if not asset:
            raise HTTPException(404, "A file is no longer available")
        validate_asset_in_share(db, link, asset)
        target = uuid.UUID(job["folder_id"]) if job["folder_id"] else None
        if target and asset.folder_id != target and (not asset.folder_id or not _is_descendant_of(db, asset.folder_id, target)):
            raise HTTPException(409, "Files moved; start a new ZIP download")
        media = _get_latest_media_file(db, asset.id)
        if not media or str(media.version_id) != item["version_id"]:
            raise HTTPException(409, "Files changed; start a new ZIP download")
        choice = pick_or_refuse(asset, media, None, prefer_original=True,
            allow_original=original_allowed_for_download(db, asset, actor, None, True))
        if choice.s3_key != item["key"]:
            raise HTTPException(409, "Download permissions changed; start a new ZIP download")
        db.rollback()

    def completed():
        job["completed_files"] += 1
        _save_stream_status(job)

    def stream():
        chunks = zip_chunks(files, lambda item: archive_storage().get_object(
            Bucket=settings.s3_bucket, Key=item["key"]), check, completed)
        try:
            while True:
                # Check before advancing, including after a long backpressure pause.
                lease.check()
                try:
                    chunk = next(chunks)
                except StopIteration:
                    break
                lease.check()
                yield chunk
            job.update(status="complete")
            _save_stream_status(job)
        except GeneratorExit:
            job.update(status="failed", error="ZIP download was interrupted; please try again")
            _save_stream_status(job)
            raise
        except Exception as exc:
            job.update(status="failed", error=exc.detail if isinstance(exc, HTTPException) else "ZIP download failed; please try again")
            _save_stream_status(job)
            raise
        finally:
            try:
                chunks.close()
            finally:
                lease.close()
    return stream()
