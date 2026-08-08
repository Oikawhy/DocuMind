"""Storage admission tests for the MinIO adapter."""

from __future__ import annotations

import hashlib
import io
from unittest.mock import MagicMock

import pytest

from documind.domain.errors import UploadTooLargeError
from documind.services.storage_service import StorageService


async def _run_direct(function: object, *args: object, **kwargs: object) -> object:
    return function(*args, **kwargs)  # type: ignore[operator]


async def test_initialize_creates_private_object_lock_bucket() -> None:
    client = MagicMock()
    client.bucket_exists.return_value = False
    service = StorageService(client, bucket_name="documents", hard_cap_bytes=32, run_sync=_run_direct)

    await service.initialize()

    client.make_bucket.assert_called_once_with("documents", object_lock=True)


async def test_stream_upload_hashes_bytes_without_buffering_whole_file() -> None:
    payload = b"streamed-document"
    client = MagicMock()

    def consume_upload(_bucket: str, _key: str, reader: object, **_kwargs: object) -> None:
        assert reader.read(4) == payload[:4]  # type: ignore[attr-defined]
        assert reader.read() == payload[4:]  # type: ignore[attr-defined]

    client.put_object.side_effect = consume_upload
    service = StorageService(client, bucket_name="documents", hard_cap_bytes=64, run_sync=_run_direct)

    digest, byte_size = await service.stream_upload(io.BytesIO(payload), "quarantine/version/original")

    assert digest == hashlib.sha256(payload).hexdigest()
    assert byte_size == len(payload)
    client.put_object.assert_called_once()


async def test_stream_upload_rejects_hard_cap_and_removes_partial_object() -> None:
    client = MagicMock()

    def consume_upload(_bucket: str, _key: str, reader: object, **_kwargs: object) -> None:
        while reader.read(4):  # type: ignore[attr-defined]
            pass

    client.put_object.side_effect = consume_upload
    service = StorageService(client, bucket_name="documents", hard_cap_bytes=5, run_sync=_run_direct)

    with pytest.raises(UploadTooLargeError):
        await service.stream_upload(io.BytesIO(b"123456"), "quarantine/version/original")

    client.remove_object.assert_called_once_with("documents", "quarantine/version/original")


async def test_move_to_accepted_copies_then_removes_quarantine() -> None:
    client = MagicMock()
    service = StorageService(client, bucket_name="documents", hard_cap_bytes=32, run_sync=_run_direct)

    await service.move_to_accepted(
        "quarantine/version/original",
        "originals/document/1/digest",
    )

    assert client.copy_object.call_args.args[:2] == ("documents", "originals/document/1/digest")
    client.remove_object.assert_called_once_with("documents", "quarantine/version/original")
