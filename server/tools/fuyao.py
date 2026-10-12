from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from server.config import Settings, endpoint_is_safe


def redact(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [redact(x, secrets) for x in value]
    if isinstance(value, dict):
        return {redact(str(k), secrets): ("[REDACTED]" if any(x in str(k).lower() for x in
                 ("api_key", "apikey", "authorization", "password", "access_token", "cookie"))
                 else redact(v, secrets)) for k, v in value.items()}
    return value


@dataclass
class FetchResult:
    body: Any = field(default=None, repr=False)
    http_status: int | None = None
    business_code: int | None = None
    request_id: str | None = None
    error: str | None = None
    retryable: bool = False


class FuyaoClient:
    def __init__(self, settings: Settings, transport=None):
        self.settings, self.transport = settings, transport

    async def fetch(self, path: str, parameters: dict) -> FetchResult:
        settings = self.settings
        if not settings.fuyao_api_key:
            return FetchResult(error="missing_credentials")
        if not endpoint_is_safe(settings.fuyao_base_url):
            return FetchResult(error="invalid_endpoint")
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=settings.timeout_seconds,
                                         follow_redirects=False, headers={"X-api-key": settings.fuyao_api_key}) as client:
                async with client.stream("GET", settings.fuyao_base_url + path, params=parameters) as response:
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 4 * 1024 * 1024:
                            return FetchResult(http_status=response.status_code, error="response_size_limit")
                        chunks.append(chunk)
                    try:
                        import json
                        def reject_constant(value):
                            raise ValueError("Nonfinite JSON number")
                        body = json.loads(b"".join(chunks), parse_constant=reject_constant)
                    except (ValueError, UnicodeError):
                        return FetchResult(http_status=response.status_code, error="invalid_json",
                                           retryable=response.status_code in (429, 500, 502, 503, 504))
            body = redact(body, settings.secrets)
            code = body.get("code") if isinstance(body, dict) and type(body.get("code")) is int else None
            request_id = body.get("request_id") if isinstance(body, dict) else None
            result = FetchResult(body=body, http_status=response.status_code, business_code=code,
                                 request_id=request_id[:200] if isinstance(request_id, str) else None)
            if response.status_code != 200:
                result.error = "http_error"
                result.retryable = response.status_code in (408, 429, 500, 502, 503, 504)
            elif code is None:
                result.error = "invalid_envelope"
            elif code != 0:
                result.error = "business_error"
                result.retryable = code == 4001 or code >= 5000
            return result
        except httpx.TimeoutException:
            return FetchResult(error="timeout", retryable=True)
        except httpx.TransportError:
            return FetchResult(error="transport_error", retryable=True)
