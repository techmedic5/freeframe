"""`GET /share/{token}/assets?folder_id=` lists a folder only inside the link's scope.

A multi-item share is a project link with ShareLinkItem rows. Its `folder_id`
branch checked only that the folder sat in the link's project, so any folder of
the project could be listed (names, thumbnails, sizes, creators, subfolders)
although streaming an asset from it was already refused by
`validate_asset_in_share`. The listing now applies the same rule: a shared
folder item or a descendant of one.
"""
import uuid
from datetime import datetime, timezone

import pytest


def _preview(real_db, folder, name="preview", **asset_fields):
    from apps.api.models.asset import Asset, AssetType, AssetVersion, FileType, MediaFile, ProcessingStatus
    fields = dict(project_id=folder.project_id, folder_id=folder.id,
                  created_by=folder.created_by, name=name, asset_type=AssetType.image)
    fields.update(asset_fields)
    asset = Asset(**fields)
    real_db.add(asset); real_db.flush()
    version = AssetVersion(asset_id=asset.id, version_number=1, created_by=folder.created_by,
                           processing_status=ProcessingStatus.ready)
    real_db.add(version); real_db.flush()
    media = MediaFile(version_id=version.id, file_type=FileType.image, original_filename=name + ".jpg",
                      mime_type="image/jpeg", file_size_bytes=100, s3_key_raw="raw/" + name,
                      s3_key_thumbnail="thumb/" + name)
    real_db.add(media); real_db.flush()
    return asset, version, media


def _folder(real_db, project, owner, name, parent=None):
    from apps.api.models.folder import Folder
    f = Folder(project_id=project.id, name=name, created_by=owner.id,
               parent_id=parent.id if parent else None)
    real_db.add(f); real_db.flush()
    return f


def _asset(real_db, project, owner, name, folder=None):
    from apps.api.models.asset import Asset, AssetType
    a = Asset(project_id=project.id, name=name, asset_type=AssetType.video,
              created_by=owner.id, folder_id=folder.id if folder else None)
    real_db.add(a); real_db.flush()
    return a


@pytest.fixture
def seed(real_db):
    """Project P: shared/ (with sub/ and sub/deep/), sibling/ (not shared).
    Project Q: other/. One link of each kind."""
    from apps.api.models.user import User
    from apps.api.models.project import Project, ProjectType
    from apps.api.models.share import ShareLink, ShareLinkItem

    owner = User(email=f"owner-{uuid.uuid4()}@t.local", name="owner")
    real_db.add(owner); real_db.flush()
    p = Project(name="p", project_type=ProjectType.personal, created_by=owner.id)
    q = Project(name="q", project_type=ProjectType.personal, created_by=owner.id)
    real_db.add_all([p, q]); real_db.flush()

    shared = _folder(real_db, p, owner, "shared")
    sub = _folder(real_db, p, owner, "sub", parent=shared)
    deep = _folder(real_db, p, owner, "deep", parent=sub)
    sibling = _folder(real_db, p, owner, "sibling")
    sibling_child = _folder(real_db, p, owner, "sibling-child", parent=sibling)
    other = _folder(real_db, q, owner, "other")

    _asset(real_db, p, owner, "in-shared", shared)
    _asset(real_db, p, owner, "in-sub", sub)
    _asset(real_db, p, owner, "in-sibling", sibling)
    _asset(real_db, q, owner, "in-other", other)

    multi = ShareLink(token=f"multi-{uuid.uuid4()}", created_by=owner.id, project_id=p.id)
    folder_link = ShareLink(token=f"folder-{uuid.uuid4()}", created_by=owner.id, folder_id=shared.id)
    project_link = ShareLink(token=f"proj-{uuid.uuid4()}", created_by=owner.id, project_id=p.id)
    real_db.add_all([multi, folder_link, project_link]); real_db.flush()
    real_db.add(ShareLinkItem(share_link_id=multi.id, folder_id=shared.id))
    real_db.flush()

    return {
        "multi": multi, "folder_link": folder_link, "project_link": project_link,
        "shared": shared, "sub": sub, "deep": deep, "sibling": sibling,
        "sibling_child": sibling_child, "other": other,
    }


def _list(real_db, link, folder_id):
    from apps.api.routers.share import get_folder_share_assets
    return get_folder_share_assets(
        link.token, folder_id=folder_id, page=1, per_page=50,
        share_session=None, db=real_db, current_user=None,
    )


def _refused(real_db, link, folder_id):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        _list(real_db, link, folder_id)
    assert exc.value.status_code == 403
    return exc.value.detail


# ── multi-item project link ───────────────────────────────────────────────────

def test_multi_share_lists_its_shared_folder(real_db, seed):
    res = _list(real_db, seed["multi"], seed["shared"].id)

    assert [a.name for a in res.assets] == ["in-shared"]
    assert [f.name for f in res.subfolders] == ["sub"]


def test_multi_share_lists_descendants_of_its_shared_folder(real_db, seed):
    assert [a.name for a in _list(real_db, seed["multi"], seed["sub"].id).assets] == ["in-sub"]
    assert _list(real_db, seed["multi"], seed["deep"].id).assets == []


def test_multi_share_refuses_an_unshared_folder_of_the_same_project(real_db, seed):
    assert _refused(real_db, seed["multi"], seed["sibling"].id) == "Folder is not in the shared items"
    assert _refused(real_db, seed["multi"], seed["sibling_child"].id) == "Folder is not in the shared items"


def test_multi_share_refuses_a_folder_of_another_project(real_db, seed):
    assert _refused(real_db, seed["multi"], seed["other"].id) == "Folder is not within the shared project"


def test_multi_share_with_only_assets_refuses_every_folder(real_db, seed):
    """An asset-only multi share has no folder items, so no folder is in scope."""
    from apps.api.models.asset import Asset
    from apps.api.models.share import ShareLinkItem
    real_db.query(ShareLinkItem).filter(ShareLinkItem.share_link_id == seed["multi"].id).delete()
    in_shared = real_db.query(Asset).filter(Asset.name == "in-shared",
                                            Asset.folder_id == seed["shared"].id).one()
    real_db.add(ShareLinkItem(share_link_id=seed["multi"].id, asset_id=in_shared.id))
    real_db.flush()

    _refused(real_db, seed["multi"], seed["shared"].id)


def test_multi_share_refuses_a_deleted_shared_folder(real_db, seed):
    from datetime import datetime, timezone
    seed["shared"].deleted_at = datetime.now(timezone.utc)
    real_db.flush()

    _refused(real_db, seed["multi"], seed["shared"].id)


# ── plain folder link ─────────────────────────────────────────────────────────

def test_folder_link_lists_its_subtree(real_db, seed):
    assert [a.name for a in _list(real_db, seed["folder_link"], seed["sub"].id).assets] == ["in-sub"]


def test_folder_link_refuses_a_folder_outside_its_subtree(real_db, seed):
    assert _refused(real_db, seed["folder_link"], seed["sibling"].id) == "Folder is not within the shared folder"
    assert _refused(real_db, seed["folder_link"], seed["other"].id) == "Folder is not within the shared folder"


# ── plain project link ────────────────────────────────────────────────────────

def test_project_link_without_items_lists_any_folder_of_its_project(real_db, seed):
    assert [a.name for a in _list(real_db, seed["project_link"], seed["sibling"].id).assets] == ["in-sibling"]
    assert [a.name for a in _list(real_db, seed["project_link"], seed["sub"].id).assets] == ["in-sub"]


def test_project_link_refuses_a_folder_of_another_project(real_db, seed):
    assert _refused(real_db, seed["project_link"], seed["other"].id) == "Folder is not within the shared project"


@pytest.mark.parametrize("link_name,card", [
    ("project_link", "shared"), ("multi", "shared"), ("folder_link", "sub"),
])
def test_shared_folder_previews_include_nested_media(real_db, seed, monkeypatch, link_name, card):
    monkeypatch.setattr("apps.api.routers.share.generate_presigned_get_url", lambda key: "signed:" + key)
    _preview(real_db, seed["deep"])
    response = _list(real_db, seed[link_name], None)
    folder = next(f for f in response.subfolders if f.name == card)
    assert folder.thumbnail_urls == ["signed:thumb/preview"]


@pytest.mark.parametrize("excluded", ["sibling", "other", "deleted_branch", "deleted_asset", "foreign_asset", "foreign_folder"])
def test_nested_previews_exclude_unshared_deleted_and_foreign_media(real_db, seed, monkeypatch, excluded):
    from apps.api.models.folder import Folder
    from apps.api.routers.share import _folder_thumbnail_urls
    monkeypatch.setattr("apps.api.routers.share.generate_presigned_get_url", lambda key: "signed:" + key)
    _preview(real_db, seed["sub"], "visible")
    if excluded in ("sibling", "other"):
        _preview(real_db, seed[excluded], "hidden")
    elif excluded == "deleted_branch":
        seed["deep"].deleted_at = datetime.now(timezone.utc)
        child = Folder(project_id=seed["deep"].project_id, parent_id=seed["deep"].id,
                       name="below-trash", created_by=seed["deep"].created_by)
        real_db.add(child); real_db.flush()
        _preview(real_db, child, "hidden")
    elif excluded == "deleted_asset":
        _preview(real_db, seed["deep"], "hidden", deleted_at=datetime.now(timezone.utc))
    elif excluded == "foreign_asset":
        _preview(real_db, seed["deep"], "hidden", project_id=seed["other"].project_id)
    else:
        seed["other"].parent_id = seed["shared"].id
        _preview(real_db, seed["other"], "hidden")
    real_db.flush()
    assert _folder_thumbnail_urls(real_db, seed["shared"]) == ["signed:thumb/visible"]


def test_folder_previews_keep_direct_media_first_and_limit_to_four(real_db, seed, monkeypatch):
    from apps.api.routers.share import _folder_thumbnail_urls
    monkeypatch.setattr("apps.api.routers.share.generate_presigned_get_url", lambda key: "signed:" + key)
    _preview(real_db, seed["shared"], "direct")
    for i in range(6):
        _preview(real_db, seed["deep"], f"nested-{i}")
    urls = _folder_thumbnail_urls(real_db, seed["shared"])
    assert len(urls) == 4
    assert urls[0] == "signed:thumb/direct"


@pytest.mark.parametrize("missing_thumbnail", [None, ""])
def test_carousel_preview_uses_later_media_with_a_thumbnail(real_db, seed, monkeypatch, missing_thumbnail):
    from apps.api.models.asset import AssetType, FileType, MediaFile
    from apps.api.routers.share import _folder_thumbnail_urls
    monkeypatch.setattr("apps.api.routers.share.generate_presigned_get_url", lambda key: "signed:" + key)
    _, version, first = _preview(real_db, seed["deep"], "carousel", asset_type=AssetType.image_carousel)
    first.id = uuid.UUID(int=1)
    first.s3_key_thumbnail = missing_thumbnail
    real_db.add(MediaFile(
        id=uuid.UUID(int=2), version_id=version.id, file_type=FileType.image,
        original_filename="second.jpg", mime_type="image/jpeg", file_size_bytes=100,
        s3_key_raw="raw/second.jpg", s3_key_thumbnail="thumb/second.jpg",
    ))
    real_db.flush()
    assert _folder_thumbnail_urls(real_db, seed["shared"]) == ["signed:thumb/second.jpg"]


def test_audio_waveforms_do_not_take_folder_image_preview_slots(real_db, seed, monkeypatch):
    from apps.api.models.asset import AssetType, FileType
    from apps.api.routers.share import _folder_thumbnail_urls
    monkeypatch.setattr("apps.api.routers.share.generate_presigned_get_url", lambda key: "signed:" + key)
    _preview(real_db, seed["deep"], "photo")
    for index in range(4):
        _, _, media = _preview(real_db, seed["shared"], f"audio-{index}", asset_type=AssetType.audio)
        media.file_type = FileType.audio
        media.mime_type = "audio/mpeg"
        media.s3_key_thumbnail = f"waveforms/{index}.json"
    real_db.flush()
    assert _folder_thumbnail_urls(real_db, seed["shared"]) == ["signed:thumb/photo"]


def test_empty_folder_preview_and_parent_cycle_terminate(real_db, seed, monkeypatch):
    from sqlalchemy import text
    from apps.api.routers.share import _folder_thumbnail_urls
    monkeypatch.setattr("apps.api.routers.share.generate_presigned_get_url", lambda key: "signed:" + key)
    assert _folder_thumbnail_urls(real_db, seed["shared"]) == []
    _preview(real_db, seed["deep"])
    seed["shared"].parent_id = seed["deep"].id
    real_db.flush()
    real_db.execute(text("SET LOCAL statement_timeout = '2s'"))
    assert _folder_thumbnail_urls(real_db, seed["shared"]) == ["signed:thumb/preview"]


def test_missing_or_unready_thumbnails_do_not_hide_available_previews(real_db, seed, monkeypatch):
    from apps.api.models.asset import ProcessingStatus
    from apps.api.routers.share import _folder_thumbnail_urls
    monkeypatch.setattr("apps.api.routers.share.generate_presigned_get_url", lambda key: "signed:" + key)
    _preview(real_db, seed["deep"], "visible")
    for i in range(5):
        _, version, media = _preview(real_db, seed["shared"], f"missing-{i}")
        if i == 0:
            media.s3_key_thumbnail = None
        elif i == 1:
            media.s3_key_thumbnail = ""
        elif i == 2:
            version.deleted_at = datetime.now(timezone.utc)
        else:
            version.processing_status = ProcessingStatus.processing
    real_db.flush()
    assert _folder_thumbnail_urls(real_db, seed["shared"]) == ["signed:thumb/visible"]


def test_multi_share_preview_hides_media_below_a_deleted_ancestor(real_db, seed, monkeypatch):
    monkeypatch.setattr("apps.api.routers.share.generate_presigned_get_url", lambda key: "signed:" + key)
    _preview(real_db, seed["deep"])
    seed["shared"].parent_id = seed["sibling"].id
    seed["sibling"].deleted_at = datetime.now(timezone.utc)
    real_db.flush()
    response = _list(real_db, seed["multi"], None)
    assert all(not folder.thumbnail_urls for folder in response.subfolders)


@pytest.mark.parametrize("new_status,deleted,thumbnail,expected", [
    ("uploading", False, "new", "preview"),
    ("failed", False, "new", "preview"),
    ("ready", True, "new", "preview"),
    ("ready", False, "new", "new"),
    ("ready", False, None, None),
])
def test_folder_preview_uses_only_latest_live_ready_version(real_db, seed, monkeypatch,
                                                         new_status, deleted, thumbnail, expected):
    from apps.api.models.asset import AssetVersion, FileType, MediaFile, ProcessingStatus
    from apps.api.routers.share import _folder_thumbnail_urls
    monkeypatch.setattr("apps.api.routers.share.generate_presigned_get_url", lambda key: "signed:" + key)
    asset, _, _ = _preview(real_db, seed["deep"])
    version = AssetVersion(asset_id=asset.id, version_number=2, created_by=asset.created_by,
                           processing_status=ProcessingStatus(new_status),
                           deleted_at=datetime.now(timezone.utc) if deleted else None)
    real_db.add(version); real_db.flush()
    real_db.add(MediaFile(version_id=version.id, file_type=FileType.image, original_filename="new.jpg",
                          mime_type="image/jpeg", file_size_bytes=100, s3_key_raw="raw/new",
                          s3_key_thumbnail="thumb/" + thumbnail if thumbnail else None))
    real_db.flush()
    assert _folder_thumbnail_urls(real_db, seed["shared"]) == (["signed:thumb/" + expected] if expected else [])
