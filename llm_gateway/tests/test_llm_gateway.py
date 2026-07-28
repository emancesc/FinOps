"""
Test unitari per llm_gateway.
Usa provider mockati — nessuna chiamata a API reali.
"""
from __future__ import annotations
import json
import pytest
from pydantic import BaseModel
from unittest.mock import AsyncMock, MagicMock, patch

from llm_gateway.base import LLMClient, LLMMessage, LLMResponse
from llm_gateway.claude_client import ClaudeClient, _strip_fence
from llm_gateway.azure_openai_client import AzureOpenAIClient
from llm_gateway.factory import get_llm_client


# ---------------------------------------------------------------------------
# Schema di test
# ---------------------------------------------------------------------------

class TagProposal(BaseModel):
    tag_key: str
    tag_value: str
    confidence: float


# ---------------------------------------------------------------------------
# MockLLMClient — implementazione minimale per testare la logica base
# ---------------------------------------------------------------------------

class MockLLMClient(LLMClient):
    """Client mock con lista di risposte configurabili."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = iter(responses)
        self.call_count = 0
        self.last_messages: list[LLMMessage] = []

    async def complete(
        self,
        system: str,
        messages: list[LLMMessage],
        response_format=None,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        self.call_count += 1
        self.last_messages = messages
        content = next(self._responses)
        if response_format is not None:
            parsed = json.loads(content)
            response_format.model_validate(parsed)
        return LLMResponse(content=content, model="mock", input_tokens=10, output_tokens=10)


# ---------------------------------------------------------------------------
# Test MockLLMClient
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mock_valid_json():
    """Risposta JSON valida → parsata senza retry."""
    payload = json.dumps({"tag_key": "env", "tag_value": "prod", "confidence": 0.95})
    client = MockLLMClient([payload])
    resp = await client.complete("system", [LLMMessage(role="user", content="hello")], TagProposal)
    assert resp.content == payload
    assert client.call_count == 1


@pytest.mark.asyncio
async def test_mock_invalid_json_raises():
    """Risposta malformata → ValidationError nel mock (nessun retry nel MockClient)."""
    client = MockLLMClient(["not json at all"])
    with pytest.raises(Exception):
        await client.complete("system", [LLMMessage(role="user", content="hello")], TagProposal)


# ---------------------------------------------------------------------------
# Test _strip_fence
# ---------------------------------------------------------------------------

def test_strip_fence_no_fence():
    assert _strip_fence('{"a": 1}') == '{"a": 1}'


def test_strip_fence_with_json_block():
    text = '```json\n{"a": 1}\n```'
    assert _strip_fence(text) == '{"a": 1}'


def test_strip_fence_plain_block():
    text = '```\n{"a": 1}\n```'
    assert _strip_fence(text) == '{"a": 1}'


# ---------------------------------------------------------------------------
# Test ClaudeClient — retry con AsyncMock
# ---------------------------------------------------------------------------

def _make_claude_response(text: str) -> MagicMock:
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    msg.usage = MagicMock(input_tokens=50, output_tokens=20)
    return msg


@pytest.mark.asyncio
async def test_claude_valid_json_no_retry():
    """Risposta JSON valida al primo tentativo → nessun retry."""
    payload = json.dumps({"tag_key": "env", "tag_value": "prod", "confidence": 0.9})

    with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
        client = ClaudeClient()
        client._client = AsyncMock()
        client._client.messages.create = AsyncMock(
            return_value=_make_claude_response(payload)
        )

        resp = await client.complete("system", [LLMMessage(role="user", content="q")], TagProposal)

    assert json.loads(resp.content)["tag_key"] == "env"
    assert client._client.messages.create.call_count == 1


@pytest.mark.asyncio
async def test_claude_malformed_then_valid_retries():
    """Prima risposta malformata → retry con messaggio di correzione → seconda risposta valida."""
    bad = "questa non e' json"
    good = json.dumps({"tag_key": "cost-center", "tag_value": "CC-100", "confidence": 0.8})

    with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
        client = ClaudeClient()
        client._client = AsyncMock()
        client._client.messages.create = AsyncMock(
            side_effect=[_make_claude_response(bad), _make_claude_response(good)]
        )

        resp = await client.complete("system", [LLMMessage(role="user", content="q")], TagProposal)

    assert client._client.messages.create.call_count == 2
    # Al secondo tentativo i messaggi devono includere la correzione
    second_call_messages = client._client.messages.create.call_args_list[1].kwargs["messages"]
    assert any("non era JSON valido" in str(m.get("content", "")) for m in second_call_messages)
    assert json.loads(resp.content)["tag_key"] == "cost-center"


@pytest.mark.asyncio
async def test_claude_always_malformed_raises():
    """Tutte le risposte malformate → ValueError dopo i retry."""
    bad = "not json"

    with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
        client = ClaudeClient()
        client._client = AsyncMock()
        client._client.messages.create = AsyncMock(return_value=_make_claude_response(bad))

        with pytest.raises(ValueError, match="tentativi"):
            await client.complete("system", [LLMMessage(role="user", content="q")], TagProposal)

    # Deve aver tentato _MAX_RETRIES + 1 volte (3 totali)
    assert client._client.messages.create.call_count == 3


@pytest.mark.asyncio
async def test_claude_json_in_fence_accepted():
    """JSON avvolto in markdown fence → accettato grazie a _strip_fence."""
    inner = {"tag_key": "env", "tag_value": "staging", "confidence": 0.7}
    fenced = f"```json\n{json.dumps(inner)}\n```"

    with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
        client = ClaudeClient()
        client._client = AsyncMock()
        client._client.messages.create = AsyncMock(return_value=_make_claude_response(fenced))

        resp = await client.complete("system", [LLMMessage(role="user", content="q")], TagProposal)

    assert client._client.messages.create.call_count == 1
    assert "staging" in resp.content


# ---------------------------------------------------------------------------
# Test AzureOpenAIClient — retry con AsyncMock
# ---------------------------------------------------------------------------

def _make_openai_response(text: str) -> MagicMock:
    choice = MagicMock()
    choice.message.content = text
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = MagicMock(prompt_tokens=50, completion_tokens=20)
    return resp


@pytest.mark.asyncio
async def test_azure_malformed_then_valid_retries():
    """AzureOpenAI: prima risposta malformata → retry → seconda risposta valida."""
    bad = "non json"
    good = json.dumps({"tag_key": "team", "tag_value": "platform", "confidence": 0.85})

    env = {
        "AZURE_OPENAI_API_KEY": "test",
        "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com",
        "AZURE_OPENAI_DEPLOYMENT": "gpt-4o",
    }
    with patch.dict("os.environ", env):
        client = AzureOpenAIClient()
        client._client = AsyncMock()
        client._client.chat.completions.create = AsyncMock(
            side_effect=[_make_openai_response(bad), _make_openai_response(good)]
        )

        resp = await client.complete("system", [LLMMessage(role="user", content="q")], TagProposal)

    assert client._client.chat.completions.create.call_count == 2
    assert json.loads(resp.content)["tag_key"] == "team"


# ---------------------------------------------------------------------------
# Test factory
# ---------------------------------------------------------------------------

def test_factory_claude():
    with patch.dict("os.environ", {"LLM_PROVIDER": "claude", "ANTHROPIC_API_KEY": "k"}):
        client = get_llm_client()
    assert isinstance(client, ClaudeClient)


def test_factory_azure():
    env = {
        "LLM_PROVIDER": "azure_openai",
        "AZURE_OPENAI_API_KEY": "k",
        "AZURE_OPENAI_ENDPOINT": "https://x.openai.azure.com",
        "AZURE_OPENAI_DEPLOYMENT": "gpt-4o",
    }
    with patch.dict("os.environ", env):
        client = get_llm_client()
    assert isinstance(client, AzureOpenAIClient)


def test_factory_invalid_provider():
    with patch.dict("os.environ", {"LLM_PROVIDER": "gemini"}):
        with pytest.raises(ValueError, match="non supportato"):
            get_llm_client()
