"""Object-store backends for the archive: a local directory or any S3-compatible bucket.

``S3ObjectStore`` takes an already-built client so tests (and callers with exotic setups)
can inject a fake; ``from_settings`` builds a real boto3 client from ``Settings`` — the
boto3 import lives behind the ``[archive]`` extra. Credentials come from ``.env`` via
``Settings`` only. Keys are POSIX-style relative paths (``price_bars/…/2025-06.parquet``).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ibkr_trader.config import Settings, get_settings


@dataclass(frozen=True)
class StoredObject:
    key: str
    size: int


class ObjectStore(Protocol):
    """Minimal blob interface: whole-object put/get plus listing."""

    def put_bytes(self, key: str, data: bytes) -> None: ...

    def get_bytes(self, key: str) -> bytes:
        """Raises KeyError if the object does not exist."""
        ...

    def exists(self, key: str) -> bool: ...

    def list_objects(self, prefix: str = "") -> list[StoredObject]: ...


def _is_missing_s3_object(exc: Exception) -> bool:
    """Return whether a botocore-style client error represents a missing object."""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error", {})
    code = error.get("Code") if isinstance(error, dict) else None
    return str(code) in {"404", "NoSuchKey", "NotFound"}


class LocalDirStore:
    """Archive to a plain directory (external drive, NAS mount) — also the test backend."""

    def __init__(self, root: Path | str):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError(f"key {key!r} escapes the archive root")
        return path

    def put_bytes(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # write-then-rename so a crash mid-write never leaves a truncated object behind
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    def get_bytes(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise KeyError(key)
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def list_objects(self, prefix: str = "") -> list[StoredObject]:
        if not self.root.is_dir():
            return []
        objects = [
            StoredObject(key=path.relative_to(self.root).as_posix(), size=path.stat().st_size)
            for path in self.root.rglob("*")
            if path.is_file() and not path.name.endswith(".tmp")
        ]
        return sorted(
            (obj for obj in objects if obj.key.startswith(prefix)), key=lambda obj: obj.key
        )


class S3ObjectStore:
    """S3-compatible bucket (Cloudflare R2, Backblaze B2, MinIO, AWS)."""

    def __init__(self, client, bucket: str, *, prefix: str = ""):
        if not bucket:
            raise ValueError("S3 archive needs a bucket name (ARCHIVE_S3_BUCKET)")
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    @classmethod
    def from_settings(cls, settings: Settings) -> "S3ObjectStore":
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise RuntimeError(
                "the s3 archive backend needs the archive extra — "
                "install with: uv sync --extra archive"
            ) from exc
        client = boto3.client(
            "s3",
            endpoint_url=settings.archive_s3_endpoint_url or None,
            region_name=settings.archive_s3_region or None,
            aws_access_key_id=settings.archive_s3_access_key_id or None,
            aws_secret_access_key=settings.archive_s3_secret_access_key or None,
        )
        return cls(client, settings.archive_s3_bucket, prefix=settings.archive_s3_prefix)

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def put_bytes(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data)

    def get_bytes(self, key: str) -> bytes:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as exc:
            if _is_missing_s3_object(exc):
                raise KeyError(key) from None
            raise
        return response["Body"].read()

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as exc:
            if _is_missing_s3_object(exc):
                return False
            raise
        return True

    def list_objects(self, prefix: str = "") -> list[StoredObject]:
        objects: list[StoredObject] = []
        strip = len(f"{self.prefix}/") if self.prefix else 0
        kwargs = {"Bucket": self.bucket, "Prefix": self._key(prefix)}
        while True:
            page = self.client.list_objects_v2(**kwargs)
            objects += [
                StoredObject(key=item["Key"][strip:], size=item["Size"])
                for item in page.get("Contents", [])
            ]
            if not page.get("IsTruncated"):
                return sorted(objects, key=lambda obj: obj.key)
            kwargs["ContinuationToken"] = page["NextContinuationToken"]


def store_from_settings(settings: Settings | None = None) -> ObjectStore:
    """The configured archive backend; refuses to guess when none is configured."""
    settings = settings or get_settings()
    if settings.archive_backend == "local":
        return LocalDirStore(settings.archive_local_dir)
    if settings.archive_backend == "s3":
        return S3ObjectStore.from_settings(settings)
    raise RuntimeError(
        "no archive backend configured — set ARCHIVE_BACKEND=s3 (Cloudflare R2/Backblaze B2, "
        "see docs/remote-archive.md) or ARCHIVE_BACKEND=local in .env"
    )
