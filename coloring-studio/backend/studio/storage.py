"""Asset storage (local filesystem by default, S3-compatible optional).

Every stored file gets an Asset row with a SHA-256 checksum so later steps
(QA, targeted-edit verification, exports) can prove a file did not change.
"""
from __future__ import annotations

import hashlib
import io
from pathlib import Path

from sqlalchemy.orm import Session

from .config import get_settings
from .models import Asset, new_id

EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp",
       "application/pdf": "pdf", "application/zip": "zip", "application/json": "json"}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class LocalBackend:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root.resolve() not in p.parents:
            raise ValueError("invalid storage key")
        return p

    def put(self, key: str, data: bytes) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)  # atomic on POSIX

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        p = self._path(key)
        if p.exists():
            p.unlink()

    def local_path(self, key: str) -> Path:
        return self._path(key)


class S3Backend:  # pragma: no cover - requires boto3 + a bucket
    def __init__(self, bucket: str, endpoint: str | None):
        import boto3  # optional dependency
        self.bucket = bucket
        self.client = boto3.client("s3", endpoint_url=endpoint)

    def put(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data)

    def get(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def local_path(self, key: str) -> Path:
        raise NotImplementedError


_backend = None


def backend():
    global _backend
    if _backend is None:
        s = get_settings()
        if s.storage_backend == "s3":
            if not s.s3_bucket:
                raise RuntimeError("STUDIO_STORAGE=s3 requires S3_BUCKET")
            _backend = S3Backend(s.s3_bucket, s.s3_endpoint)
        else:
            _backend = LocalBackend(s.assets_dir)
    return _backend


def reset_backend() -> None:
    global _backend
    _backend = None


def store_bytes(db: Session, data: bytes, *, mime: str, kind: str, project_id: str | None,
                width: int | None = None, height: int | None = None) -> Asset:
    aid = new_id()
    key = f"{project_id or 'shared'}/{kind}/{aid}.{EXT.get(mime, 'bin')}"
    backend().put(key, data)
    if mime.startswith("image/") and (width is None or height is None):
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            width, height = im.size
    asset = Asset(id=aid, project_id=project_id, kind=kind, storage_key=key, mime=mime,
                  size=len(data), checksum=sha256(data), width=width, height=height)
    db.add(asset)
    db.flush()
    return asset


def read_asset(asset: Asset, verify: bool = True) -> bytes:
    data = backend().get(asset.storage_key)
    if verify and sha256(data) != asset.checksum:
        raise IOError(f"checksum mismatch for asset {asset.id}")
    return data
