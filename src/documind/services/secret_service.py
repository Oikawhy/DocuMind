"""OpenBao (Vault-compatible) async client for short-lived credential retrieval.

Uses ``httpx.AsyncClient`` rather than the synchronous ``hvac`` library to
stay async-native.  The client reads KV v2 secrets at
``secret/data/{path}`` and returns the requested key from the nested
``data.data`` envelope.
"""

from __future__ import annotations

import httpx
import structlog

from documind.domain.errors import SecretRetrievalError

logger = structlog.get_logger()


class SecretService:
    """Retrieve secrets from an OpenBao/Vault KV v2 mount."""

    def __init__(self, openbao_addr: str, auth_token: str) -> None:
        self._addr = openbao_addr.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._addr,
            headers={"X-Vault-Token": auth_token},
            timeout=httpx.Timeout(10.0),
        )

    async def get_secret(self, path: str, key: str = "value") -> str:
        """Read a single key from an OpenBao KV v2 secret.

        Args:
            path: Secret path relative to the ``secret/`` mount
                  (e.g. ``documind/database``).
            key: Key within the secret data map.  Defaults to ``"value"``.

        Returns:
            The secret string value.

        Raises:
            SecretRetrievalError: On any network, auth, or missing-key failure.
        """
        url = f"/v1/secret/data/{path}"
        try:
            resp = await self._client.get(url)
            resp.raise_for_status()
            payload = resp.json()
            value = payload["data"]["data"][key]
        except httpx.HTTPStatusError as exc:
            await logger.aerror(
                "openbao_secret_http_error",
                path=path,
                status=exc.response.status_code,
            )
            raise SecretRetrievalError(f"OpenBao returned HTTP {exc.response.status_code} for path '{path}'.") from exc
        except (httpx.RequestError, KeyError, TypeError) as exc:
            await logger.aerror("openbao_secret_error", path=path, error=str(exc))
            raise SecretRetrievalError(f"Failed to retrieve secret at path '{path}'.") from exc
        else:
            await logger.ainfo("openbao_secret_retrieved", path=path)
            return str(value)

    async def put_secret(self, path: str, data: dict[str, str]) -> None:
        """Write secret data to an OpenBao KV v2 secret.

        T9-11: Used to provision webhook HMAC secrets at registration time.

        Args:
            path: Secret path relative to the ``secret/`` mount
                  (e.g. ``documind/webhooks/{id}``).
            data: Key-value pairs to store.

        Raises:
            SecretRetrievalError: On any network or auth failure.
        """
        url = f"/v1/secret/data/{path}"
        try:
            resp = await self._client.post(url, json={"data": data})
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            await logger.aerror(
                "openbao_secret_write_http_error",
                path=path,
                status=exc.response.status_code,
            )
            raise SecretRetrievalError(
                f"OpenBao returned HTTP {exc.response.status_code} writing path '{path}'."
            ) from exc
        except httpx.RequestError as exc:
            await logger.aerror("openbao_secret_write_error", path=path, error=str(exc))
            raise SecretRetrievalError(f"Failed to write secret at path '{path}'.") from exc
        else:
            await logger.ainfo("openbao_secret_written", path=path)

    async def close(self) -> None:
        """Shut down the underlying HTTP client."""
        await self._client.aclose()
