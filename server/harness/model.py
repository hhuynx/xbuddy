from __future__ import annotations

import json
from urllib.parse import urlparse

import httpx

from server.config import endpoint_is_safe
from server.storage.database import encode
from server.tools.fuyao import redact


class ModelFailure(Exception):
    def __init__(self, code: str, usage: dict | None = None):
        super().__init__(code)
        self.usage = usage or {}


class JsonModel:
    """One bounded Chat Completions request. Harness owns all retries and accounting."""
    def __init__(self, settings, transport=None):
        self.settings, self.transport = settings, transport

    @property
    def configured(self):
        s = self.settings
        return bool(s.llm_model and endpoint_is_safe(s.llm_base_url)
                    and (s.llm_api_key or s.llm_allow_no_key))

    def output_token_limit(self, messages):
        limit = self.settings.model_max_output_tokens
        try:
            context = json.loads(messages[-1]['content'])
            errors = context.get('repair_errors', {})
            truncated = context.get('repair_error') == 'model_output_incomplete' or 'model_output_incomplete' in errors.values()
        except (ValueError, KeyError, TypeError, AttributeError):
            truncated = False
        return min(24000, limit * 2) if truncated else limit

    def request_options(self):
        effort = self.settings.llm_reasoning_effort
        deepseek = urlparse(self.settings.llm_base_url).hostname == 'api.deepseek.com'
        if effort == 'auto':
            effort = 'low' if deepseek and self.settings.llm_model in {'deepseek-flash','deepseek-v4-pro'} else ''
        if effort == 'none' and deepseek:
            return {'thinking':{'type':'disabled'}}
        if effort in {'low','high','max'}:
            return {'reasoning_effort':effort}
        return {}

    async def complete(self, messages: list[dict]) -> tuple[dict, dict]:
        if not self.configured:
            raise ModelFailure("model_not_configured")
        headers = {"Authorization": "Bearer " + self.settings.llm_api_key} if self.settings.llm_api_key else {}
        counts = {}
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=self.settings.model_timeout_seconds,
                                        follow_redirects=False) as client:
                async with client.stream("POST", self.settings.llm_base_url + "/chat/completions", headers=headers,
                    json={"model": self.settings.llm_model, "messages": messages,
                          "response_format": {"type": "json_object"}, "max_tokens": self.output_token_limit(messages), **self.request_options()}) as response:
                    response.raise_for_status()
                    chunks, size = [], 0
                    async for part in response.aiter_bytes():
                        size += len(part)
                        if size > 1024 * 1024:
                            raise ModelFailure("model_response_limit")
                        chunks.append(part)
            body = json.loads(b"".join(chunks), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            usage = body.get('usage') or {}
            counts = {k:usage[k] for k in ('prompt_tokens','completion_tokens','total_tokens')
                if isinstance(usage,dict) and type(usage.get(k)) is int and usage[k]>=0}
            details = usage.get('completion_tokens_details', {}) if isinstance(usage,dict) else {}
            if isinstance(details,dict) and type(details.get('reasoning_tokens')) is int and details['reasoning_tokens']>=0:
                counts['reasoning_tokens'] = details['reasoning_tokens']
            counts.update(token_status='reported' if 'total_tokens' in counts else 'estimated',cost_status='unknown')
            choice = body["choices"][0]
            if choice.get("finish_reason") not in {None, "stop"}:
                raise ModelFailure("model_output_incomplete")
            payload = json.loads(choice["message"]["content"], parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            encode(payload)
            if not isinstance(payload, dict):
                raise ModelFailure("model_json_object_required")
            return redact(payload, self.settings.secrets), counts
        except ModelFailure as exc:
            exc.usage = counts
            raise
        except httpx.HTTPStatusError as exc:
            raise ModelFailure('model_http_' + str(exc.response.status_code)) from None
        except httpx.TimeoutException:
            raise ModelFailure('model_timeout') from None
        except json.JSONDecodeError:
            raise ModelFailure('model_invalid_json', counts) from None
        except (KeyError, IndexError, TypeError):
            raise ModelFailure('model_response_structure_invalid', counts) from None
        except Exception:
            # URLs, response bodies, authorization headers and provider error text never enter events.
            raise ModelFailure("model_request_or_json_failed") from None
