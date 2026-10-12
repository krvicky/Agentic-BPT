from copy import deepcopy
from dataclasses import asdict
import json
from types import SimpleNamespace as NS
from unittest.mock import MagicMock
import pytest
from bpt_process_agent.ingestion.llm import OpenRouterClient, LLMError
from bpt_process_agent.llm_config import LLMConfig
from bpt_process_agent.ingestion.validation import EXTRACTION_SCHEMA

def config():
    return LLMConfig("test-credential", "test/flash", "test/pro")

def client_with_metadata(parameters=None):
    sdk = MagicMock()
    sdk.get.return_value = {"data": {"id": "test/flash", "endpoints": [
        {"tag": "mock/qualified", "supported_parameters": parameters if parameters is not None else ["response_format", "structured_outputs"]},
        {"tag": "mock/json-only", "supported_parameters": ["response_format"]}]}}
    return OpenRouterClient(config(), sdk=sdk), sdk

def response(content='{"facts": []}', finish="stop", refusal=None):
    return NS(choices=[NS(finish_reason=finish, message=NS(content=content, refusal=refusal))],
              usage=NS(prompt_tokens=10, completion_tokens=20, total_tokens=30), id="mock-response")

def test_strict_schema_and_only_verified_provider():
    client, sdk = client_with_metadata()
    metadata = client.verify("test/flash")
    assert metadata["providers"] == ["mock/qualified"]
    sdk.chat.completions.create.return_value = response()
    result = client.complete("test/flash", [{"role": "user", "content": "source"}], EXTRACTION_SCHEMA, name="facts")
    assert result.value == {"facts": []} and result.usage["total_tokens"] == 30
    kwargs = sdk.chat.completions.create.call_args.kwargs
    assert kwargs["response_format"]["json_schema"]["strict"] is True
    assert kwargs["extra_body"]["provider"] == {"only": ["mock/qualified"], "require_parameters": True}
    client.verify("test/flash")
    assert sdk.get.call_count == 1

@pytest.mark.parametrize("parameters", [[], ["response_format"], ["structured_outputs"]])
def test_unsupported_model_never_calls_completion(parameters):
    client, sdk = client_with_metadata(parameters)
    with pytest.raises(LLMError):
        client.verify("test/flash")
    sdk.chat.completions.create.assert_not_called()

def test_unverified_call_rejected():
    client, sdk = client_with_metadata()
    with pytest.raises(LLMError):
        client.complete("test/flash", [], EXTRACTION_SCHEMA, name="facts")
    sdk.chat.completions.create.assert_not_called()

@pytest.mark.parametrize("content,finish,refusal", [
    ('{"facts": []}', "length", None),
    ('{"facts": []}', "stop", "refused"),
    ("not JSON", "stop", None),
    ('{"facts": [], "facts": []}', "stop", None),
    ('{"other": []}', "stop", None),
    ('{"facts": [{"statement": "OPENROUTER_API_KEY=sk-abcdefghijklmnop"}]}', "stop", None),
])
def test_bad_or_truncated_output_rejected(content, finish, refusal):
    client, sdk = client_with_metadata(); client.verify("test/flash")
    sdk.chat.completions.create.return_value = response(content, finish, refusal)
    with pytest.raises(LLMError):
        client.complete("test/flash", [], EXTRACTION_SCHEMA, name="facts")

def test_provider_errors_hide_keys():
    client, sdk = client_with_metadata(); client.verify("test/flash")
    sdk.chat.completions.create.side_effect = RuntimeError("OPENROUTER_API_KEY=sk-abcdefghijklmnop")
    with pytest.raises(LLMError) as error:
        client.complete("test/flash", [], EXTRACTION_SCHEMA, name="facts")
    assert "sk-" not in str(error.value)

def test_metadata_identity_must_match():
    client, sdk = client_with_metadata()
    sdk.get.return_value["data"]["id"] = "different/model"
    with pytest.raises(LLMError): client.verify("test/flash")

def test_sdk_http_transport_is_mocked():
    import httpx
    from openai import OpenAI
    requests = []
    def handler(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"data": {"id": "test/flash", "endpoints": [
                {"provider_name": "MockProvider", "supported_parameters": ["response_format", "structured_outputs"]}]}})
        return httpx.Response(200, json={"id": "mock", "object": "chat.completion", "created": 1, "model": "test/flash",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": '{"facts": []}'}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
    sdk = OpenAI(api_key="test-credential", base_url="https://mock.invalid/api/v1",
                 http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    client = OpenRouterClient(config(), sdk=sdk)
    client.verify("test/flash")
    assert client.complete("test/flash", [{"role": "user", "content": "x"}], EXTRACTION_SCHEMA, name="facts").value == {"facts": []}
    assert requests[0].url.path == "/api/v1/models/test/flash/endpoints"
    assert json.loads(requests[1].content)["provider"]["require_parameters"] is True
    sdk.close()
