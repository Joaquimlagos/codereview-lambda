"""S3-backed storage abstraction: diff/context resolved by reference key only (FR-013)."""

from abc import ABC, abstractmethod


class StorageError(Exception):
    """Raised when a referenced key cannot be found/read (spec Edge Case)."""


class Storage(ABC):
    @abstractmethod
    def get_text(self, key: str) -> str:
        """Resolve a diff_ref/context_ref key to its stored text content."""

    @abstractmethod
    def put_text(self, key: str, content: str) -> str:
        """Store text content under `key` and return that key."""


class S3Storage(Storage):
    def __init__(self, bucket: str, client=None):
        import boto3

        self._bucket = bucket
        self._client = client or boto3.client("s3")

    def get_text(self, key: str) -> str:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except self._client.exceptions.NoSuchKey as exc:
            raise StorageError(f"Key not found: {key}") from exc
        return response["Body"].read().decode("utf-8")

    def put_text(self, key: str, content: str) -> str:
        self._client.put_object(Bucket=self._bucket, Key=key, Body=content.encode("utf-8"))
        return key


class StubStorage(Storage):
    """Network-free in-memory stand-in for tests (Principle IV, FR-011)."""

    def __init__(self, initial: dict[str, str] | None = None):
        self._data: dict[str, str] = dict(initial or {})

    def get_text(self, key: str) -> str:
        if key not in self._data:
            raise StorageError(f"Key not found: {key}")
        return self._data[key]

    def put_text(self, key: str, content: str) -> str:
        self._data[key] = content
        return key
