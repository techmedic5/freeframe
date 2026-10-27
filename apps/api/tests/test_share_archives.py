import io
import asyncio
import json
import time
import uuid
import zipfile
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
import anyio
from fastapi import HTTPException
from starlette.requests import ClientDisconnect

from apps.api.services import share_archives as archives
from apps.api.tests.test_share_folder_scope import seed  # noqa: F401


class MemoryRedis:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def setex(self, key, seconds, value):
        self.values[key] = value

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def eval(self, script, count, key, value):
        if self.values.get(key) == value:
            del self.values[key]


@pytest.fixture
def storage(monkeypatch):
    redis = MemoryRedis()
    monkeypatch.setattr(archives, "get_redis", lambda: redis)
    monkeypatch.setattr("apps.api.services.downloads.get_allow_original_downloads", lambda db: True)
    return redis


@pytest.fixture
def media(real_db, seed, storage):
    from apps.api.models.asset import Asset, AssetVersion, MediaFile, FileType, ProcessingStatus
    result = {}
    for name in ("project_link", "folder_link", "multi"):
        seed[name].allow_download = True
    real_db.flush()
    for asset in real_db.query(Asset).all():
        version = AssetVersion(asset_id=asset.id, version_number=1, processing_status=ProcessingStatus.ready, created_by=asset.created_by)
        real_db.add(version); real_db.flush()
        file = MediaFile(version_id=version.id, file_type=FileType.video, original_filename=asset.name + ".mp4",
                         mime_type="video/mp4", file_size_bytes=3, s3_key_raw="raw/" + asset.name + ".mp4")
        real_db.add(file); real_db.flush()
        result[asset.name] = (asset, version, file)
    return result


@pytest.mark.parametrize("link_name,paths", [
    ("project_link", ["shared/in-shared.mp4", "shared/sub/in-sub.mp4", "sibling/in-sibling.mp4"]),
    ("folder_link", ["in-shared.mp4", "sub/in-sub.mp4"]),
    ("multi", ["shared/in-shared.mp4", "shared/sub/in-sub.mp4"]),
])
def test_prepare_traverses_exact_shared_subtree(real_db, seed, media, link_name, paths):
    job = archives.prepare_archive(real_db, seed[link_name].token, None, None, None)
    assert sorted(f["path"] for f in job["files"]) == sorted(paths)
    assert all("other" not in f["key"] for f in job["files"])


def test_prepare_rejects_sibling_folder_and_preserves_download_gate(real_db, seed, media):
    with pytest.raises(HTTPException) as e:
        archives.prepare_archive(real_db, seed["multi"].token, None, None, seed["sibling"].id)
    assert e.value.status_code == 403
    seed["project_link"].allow_download = False
    real_db.flush()
    with pytest.raises(HTTPException) as e:
        archives.prepare_archive(real_db, seed["project_link"].token, None, None, None)
    assert e.value.status_code == 403


def test_prepare_checks_password_and_expiry(real_db, seed, media, monkeypatch):
    link = seed["project_link"]
    link.password_hash = "hash"
    real_db.flush()
    monkeypatch.setattr("apps.api.services.permissions.verify_share_session", lambda token, session: session == "correct")
    with pytest.raises(HTTPException) as e:
        archives.prepare_archive(real_db, link.token, "wrong", None, None)
    assert e.value.status_code == 403
    assert archives.prepare_archive(real_db, link.token, "correct", None, None)["file_count"] == 3
    link.expires_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
    real_db.flush()
    with pytest.raises(HTTPException) as e:
        archives.prepare_archive(real_db, link.token, "correct", None, None)
    assert e.value.status_code == 410


def test_prepare_excludes_deleted_ancestor_and_respects_original_policy(real_db, seed, media, monkeypatch):
    seed["sub"].deleted_at = datetime.now(timezone.utc)
    file = media["in-shared"][2]
    file.s3_key_processed = "processed/in-shared/hls"
    file.s3_key_download = "processed/in-shared/download.mp4"
    real_db.flush()
    monkeypatch.setattr("apps.api.services.downloads.get_allow_original_downloads", lambda db: False)
    job = archives.prepare_archive(real_db, seed["folder_link"].token, None, None, None)
    assert len(job["files"]) == 1
    assert job["files"][0]["key"] == "processed/in-shared/download.mp4"


def test_archive_collection_has_no_first_page_500_file_limit(real_db, seed, media):
    from apps.api.models.asset import Asset, AssetType, AssetVersion, MediaFile, FileType, ProcessingStatus
    folder = seed["shared"]
    for i in range(501):
        asset = Asset(project_id=folder.project_id, folder_id=folder.id, created_by=folder.created_by, name=f"extra{i}", asset_type=AssetType.image)
        real_db.add(asset); real_db.flush()
        version = AssetVersion(asset_id=asset.id, version_number=1, created_by=folder.created_by, processing_status=ProcessingStatus.ready)
        real_db.add(version); real_db.flush()
        real_db.add(MediaFile(version_id=version.id, file_type=FileType.image, original_filename=f"{i}.jpg", mime_type="image/jpeg", file_size_bytes=1, s3_key_raw=f"raw/{i}.jpg"))
    real_db.flush()
    job = archives.prepare_archive(real_db, seed["folder_link"].token, None, None, None)
    assert len(job["files"]) == 503


def test_sanitizes_paths_and_resolves_case_insensitive_collisions():
    used = set()
    names = [archives.unique_component(n, used) for n in ("../CON.jpg", "a.jpg", "A.jpg", "..", "C:\\test.jpg", "NUL", "漢" * 200 + ".jpg")]
    assert len({name.casefold() for name in names}) == len(names)
    assert all("/" not in name and "\\" not in name and name not in (".", "..") for name in names)
    assert names[2] == "A (2).jpg"
    assert names[5] == "_NUL"
    assert all(len(name.encode()) <= 180 for name in names)


def test_zip64_is_incremental_and_extracts_original_bytes(monkeypatch):
    payload = b"x" * (archives.CHUNK_SIZE * 3 + 7)
    body = io.BytesIO(payload)
    read = MagicMock(wraps=body.read)
    body.read = read
    completed = MagicMock()
    check = MagicMock()
    monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 1024)
    stream = archives.zip_chunks([{"path": "Photos/photo.jpg"}], lambda item: {"Body": body, "ContentLength": len(payload)}, check, completed)
    first = next(stream)
    assert first.startswith(b"PK")
    assert read.call_count == 0
    second = next(stream)
    assert read.call_args.args == (archives.CHUNK_SIZE,)
    data = first + second + b"".join(stream)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.read("Photos/photo.jpg") == payload
        assert archive.testzip() is None
    assert body.closed
    completed.assert_called_once()


def test_stream_checks_short_reads_and_closes_source():
    body = io.BytesIO(b"short")
    with pytest.raises(IOError):
        b"".join(archives.zip_chunks([{"path": "file.mp4"}], lambda item: {"Body": body, "ContentLength": 100}, lambda item: None, lambda: None))
    assert body.closed


def test_invalid_storage_response_still_closes_body():
    body = io.BytesIO(b"abc")
    with pytest.raises(KeyError):
        b"".join(archives.zip_chunks([{"path": "file.mp4"}], lambda item: {"Body": body}, lambda item: None, lambda: None))
    assert body.closed


def test_job_status_is_bound_to_session_and_user(storage):
    job = {"id": str(uuid.uuid4()), "owner": archives.owner_key("token", "session", None), "status": "ready"}
    archives.save_job(job)
    assert archives.load_job(job["id"], "token", "session", None)["id"] == job["id"]
    for token, session in [("other", "session"), ("token", "wrong")]:
        with pytest.raises(HTTPException) as e:
            archives.load_job(job["id"], token, session, None)
        assert e.value.status_code == 404


def test_stream_rechecks_revocation_and_releases_slot(real_db, seed, media, storage):
    link = seed["folder_link"]
    job = archives.prepare_archive(real_db, link.token, None, None, None)
    link.allow_download = False
    real_db.flush()
    stream = archives.stream_archive(real_db, job, link.token, None, None)
    with pytest.raises(HTTPException):
        next(stream)
    saved = json.loads(storage.get(archives.job_key(job["id"])))
    assert saved["status"] == "failed"
    assert not any(key.startswith("share_archive:slot:") for key in storage.values)


def test_stream_policies_rechecked_when_original_setting_changes(real_db, seed, media, storage, monkeypatch):
    link = seed["folder_link"]
    file = media["in-shared"][2]
    file.s3_key_processed = "processed/hls"
    file.s3_key_download = "processed/download.mp4"
    real_db.flush()
    job = archives.prepare_archive(real_db, link.token, None, None, None)
    monkeypatch.setattr("apps.api.services.downloads.get_allow_original_downloads", lambda db: False)
    with pytest.raises(HTTPException) as e:
        next(archives.stream_archive(real_db, job, link.token, None, None))
    assert e.value.status_code == 409


def test_stream_emits_valid_zip_once_and_clears_manifest(real_db, seed, media, storage, monkeypatch):
    link = seed["folder_link"]
    job = archives.prepare_archive(real_db, link.token, None, None, None)
    s3 = MagicMock()
    s3.get_object.side_effect = lambda **kwargs: {"Body": io.BytesIO(b"abc"), "ContentLength": 3}
    monkeypatch.setattr(archives, "archive_storage", lambda: s3)
    # real_db owns an outer rollback-only fixture transaction; production owns
    # its session and returns its read transaction to the pool between chunks.
    monkeypatch.setattr(real_db, "rollback", lambda: None)
    data = b"".join(archives.stream_archive(real_db, job, link.token, None, None))
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert sorted(archive.namelist()) == ["in-shared.mp4", "sub/in-sub.mp4"]
        assert all(archive.read(name) == b"abc" for name in archive.namelist())
    saved = json.loads(storage.get(archives.job_key(job["id"])))
    assert saved["status"] == "complete"
    assert saved["completed_files"] == 2 and saved["files"] == []
    with pytest.raises(HTTPException) as e:
        archives.stream_archive(real_db, saved, link.token, None, None)
    assert e.value.status_code == 409
    assert not any(key.startswith("share_archive:slot:") for key in storage.values)


def test_secure_share_requires_authenticated_user(real_db, seed, media):
    from apps.api.models.share import ShareVisibility
    from apps.api.models.user import User
    link = seed["project_link"]
    link.visibility = ShareVisibility.secure
    real_db.flush()
    with pytest.raises(HTTPException) as e:
        archives.prepare_archive(real_db, link.token, None, None, None)
    assert e.value.status_code == 403
    user = real_db.query(User).filter(User.id == link.created_by).one()
    assert archives.prepare_archive(real_db, link.token, None, user, None)["file_count"] == 3


def test_archive_file_limit_is_explicit_instead_of_truncating(real_db, seed, media, monkeypatch):
    monkeypatch.setattr(archives.settings, "share_archive_max_files", 1)
    with pytest.raises(HTTPException) as e:
        archives.prepare_archive(real_db, seed["project_link"].token, None, None, None)
    assert e.value.status_code == 413


def test_zip_cancellation_closes_open_storage_body():
    body = io.BytesIO(b"abc")
    stream = archives.zip_chunks([{"path": "file.mp4"}], lambda item: {"Body": body, "ContentLength": 3}, lambda item: None, lambda: None)
    next(stream)
    stream.close()
    assert body.closed


@pytest.mark.parametrize("spec_version", ["2.4", "2.0"])
@pytest.mark.parametrize("failure", ["disconnect", "storage"])
def test_response_failure_closes_body_and_releases_archive_slot(
    real_db, seed, media, storage, monkeypatch, spec_version, failure,
):
    from apps.api.routers.share_archives import ArchiveResponse
    link = seed["folder_link"]
    job = archives.prepare_archive(real_db, link.token, None, None, None)
    body = io.BytesIO(b"abc")
    if failure == "storage":
        monkeypatch.setattr(body, "read", MagicMock(side_effect=RuntimeError("storage interrupted")))
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": body, "ContentLength": 3}
    monkeypatch.setattr(archives, "archive_storage", lambda: s3)
    monkeypatch.setattr(real_db, "rollback", lambda: None)
    stream = archives.stream_archive(real_db, job, link.token, None, None)
    response = ArchiveResponse(next(stream), stream, media_type="application/zip")

    async def exercise():
        first_body = anyio.Event()

        async def receive():
            if failure == "storage":
                await anyio.sleep_forever()
            await first_body.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body" and failure == "disconnect":
                if spec_version == "2.4":
                    raise OSError("peer disconnected")
                first_body.set()
                await anyio.sleep_forever()

        await response({"type": "http", "asgi": {"spec_version": spec_version}}, receive, send)

    if failure == "storage":
        with pytest.raises(RuntimeError, match="storage interrupted"):
            asyncio.run(exercise())
    elif spec_version == "2.4":
        with pytest.raises(ClientDisconnect):
            asyncio.run(exercise())
    else:
        asyncio.run(exercise())
    assert body.closed
    saved = json.loads(storage.get(archives.job_key(job["id"])))
    assert saved["status"] == "failed"
    assert not any(key.startswith("share_archive:slot:") for key in storage.values)
