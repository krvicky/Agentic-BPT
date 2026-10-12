"""Thin SDK adapter: strict structured outputs through configured OpenRouter."""
from dataclasses import dataclass, field
import json
from typing import Protocol, Any
from urllib.parse import quote, urlparse
from openai import OpenAI
from ..llm_config import LLMConfig
from .validation import validate_json

class LLMError(ValueError):
    pass

@dataclass
class ModelResult:
    value: dict
    request_id: str = ""
    usage: dict = field(default_factory=dict)

class StructuredClient(Protocol):
    def verify(self, model: str) -> dict: ...
    def complete(self, model: str, messages: list[dict], schema: dict, *, name: str) -> ModelResult: ...

def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise LLMError("Duplicate JSON response fields")
        result[key] = value
    return result

class OpenRouterClient:
    def __init__(self, config: LLMConfig, *, sdk=None):
        # HTTPS is required outside the in-process mocked SDK used in tests.
        if urlparse(config.base_url).scheme != "https":
            raise LLMError("LLM base URL must use HTTPS")
        self.sdk = sdk or OpenAI(api_key=config.api_key, base_url=config.base_url, timeout=60, max_retries=0)
        self.capabilities: dict[str, dict] = {}

    def verify(self, model: str) -> dict:
        if model in self.capabilities:
            return self.capabilities[model]
        try:
            metadata = self.sdk.get("/models/" + quote(model, safe="/") + "/endpoints", cast_to=dict[str, Any])
            data = metadata["data"]
            if data["id"] != model:
                raise LLMError("Model metadata does not match the requested model ID")
            required = {"response_format", "structured_outputs"}
            providers = sorted({endpoint.get("tag") or endpoint.get("provider_name") for endpoint in data["endpoints"]
                                if required <= set(endpoint.get("supported_parameters", []))
                                and (endpoint.get("tag") or endpoint.get("provider_name"))})
            if not providers:
                raise LLMError("Selected model has no provider advertising strict structured-output support")
            capability = {"model": model, "supported_parameters": sorted(required), "providers": providers}
            self.capabilities[model] = capability
            return capability
        except LLMError:
            raise
        except Exception:
            raise LLMError("Could not verify model structured-output capabilities") from None

    def complete(self, model: str, messages: list[dict], schema: dict, *, name: str) -> ModelResult:
        capability = self.capabilities.get(model)
        if capability is None:
            raise LLMError("Model capability verification is required before execution")
        try:
            completion = self.sdk.chat.completions.create(
                model=model, messages=messages,
                response_format={"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}},
                extra_body={"provider": {"only": capability["providers"], "require_parameters": True}},
            )
            choice = completion.choices[0]
            if choice.finish_reason != "stop" or getattr(choice.message, "refusal", None):
                raise LLMError("Model refused or did not complete a structured response")
            value = json.loads(choice.message.content, object_pairs_hook=_unique_object)
            validate_json(value, schema)
            usage = {}
            if completion.usage:
                usage = {key: getattr(completion.usage, key) for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
            # No full SDK dumps: credentials and provider reasoning fields never enter traces.
            return ModelResult(value, getattr(completion, "id", "") or "", usage)
        except LLMError:
            raise
        except Exception:
            raise LLMError("Structured model call failed or returned invalid content") from None
