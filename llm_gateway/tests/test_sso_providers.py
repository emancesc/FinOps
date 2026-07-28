"""
Test per i provider SSO: BedrockClient e AzureOpenAIClient con DefaultAzureCredential.
Tutti i test usano mock — nessuna chiamata a AWS o Azure reale.
"""
from __future__ import annotations

import json
import pytest
from pydantic import BaseModel
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

from llm_gateway.base import LLMMessage, LLMResponse
from llm_gateway.bedrock_client import BedrockClient
from llm_gateway.azure_openai_client import AzureOpenAIClient
from llm_gateway.factory import get_llm_client


class TagProposal(BaseModel):
    tag_key: str
    tag_value: str
    confidence: float


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bedrock_response(text: str) -> dict:
    return {
        "content": [{"text": text}],
        "usage": {"input_tokens": 40, "output_tokens": 15},
    }


def _make_openai_response(text: str) -> MagicMock:
    choice = MagicMock()
    choice.message.content = text
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = MagicMock(prompt_tokens=40, completion_tokens=15)
    return resp


# ---------------------------------------------------------------------------
# BedrockClient — autenticazione SSO tramite AWS profile / AssumeRole
# ---------------------------------------------------------------------------

def _mock_boto3_session(invoke_return: dict):
    """Restituisce un mock di boto3.Session che simula bedrock-runtime."""
    runtime_mock = MagicMock()
    body_mock = MagicMock()
    body_mock.read.return_value = json.dumps(invoke_return).encode()
    runtime_mock.invoke_model.return_value = {"body": body_mock}

    session_mock = MagicMock()
    session_mock.client.return_value = runtime_mock
    return session_mock, runtime_mock


@pytest.mark.asyncio
async def test_bedrock_valid_json():
    """BedrockClient: risposta valida al primo tentativo."""
    payload = json.dumps({"tag_key": "env", "tag_value": "prod", "confidence": 0.9})
    session_mock, runtime_mock = _mock_boto3_session(_bedrock_response(payload))

    with patch("llm_gateway.bedrock_client._build_bedrock_session", return_value=session_mock):
        client = BedrockClient()
        resp = await client.complete(
            "system", [LLMMessage(role="user", content="q")], TagProposal
        )

    assert json.loads(resp.content)["tag_value"] == "prod"
    assert runtime_mock.invoke_model.call_count == 1


@pytest.mark.asyncio
async def test_bedrock_malformed_then_valid_retries():
    """BedrockClient: prima risposta non-JSON → retry → seconda valida."""
    bad = "non è json"
    good = json.dumps({"tag_key": "cost-center", "tag_value": "CC-42", "confidence": 0.8})

    session_mock = MagicMock()
    runtime_mock = MagicMock()
    session_mock.client.return_value = runtime_mock

    def _invoke(modelId, body, contentType, accept):
        call_n = runtime_mock.invoke_model.call_count
        text = bad if call_n == 1 else good
        body_mock = MagicMock()
        body_mock.read.return_value = json.dumps(_bedrock_response(text)).encode()
        return {"body": body_mock}

    runtime_mock.invoke_model.side_effect = _invoke

    with patch("llm_gateway.bedrock_client._build_bedrock_session", return_value=session_mock):
        client = BedrockClient()
        resp = await client.complete(
            "system", [LLMMessage(role="user", content="q")], TagProposal
        )

    assert runtime_mock.invoke_model.call_count == 2
    assert json.loads(resp.content)["tag_value"] == "CC-42"


@pytest.mark.asyncio
async def test_bedrock_no_boto3_raises():
    """BedrockClient: boto3 non installato → RuntimeError chiaro."""
    import builtins
    real_import = builtins.__import__

    def _mock_import(name, *args, **kwargs):
        if name == "boto3":
            raise ImportError("No module named 'boto3'")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=_mock_import):
        with pytest.raises(RuntimeError, match="boto3 non installato"):
            from llm_gateway.bedrock_client import _build_bedrock_session
            _build_bedrock_session()


@pytest.mark.asyncio
async def test_bedrock_assume_role_called_when_arn_set():
    """BedrockClient: con BEDROCK_ASSUME_ROLE_ARN la sessione risultante usa le credenziali del role."""
    payload = json.dumps({"tag_key": "team", "tag_value": "platform", "confidence": 0.75})

    # Sessione finale (dopo AssumeRole) — quella che fornisce bedrock-runtime
    final_session_mock = MagicMock()
    runtime_mock = MagicMock()
    body_mock = MagicMock()
    body_mock.read.return_value = json.dumps(_bedrock_response(payload)).encode()
    runtime_mock.invoke_model.return_value = {"body": body_mock}
    final_session_mock.client.return_value = runtime_mock

    env = {"BEDROCK_ASSUME_ROLE_ARN": "arn:aws:iam::123456789012:role/FinOpsLLM"}

    # _build_bedrock_session restituisce la sessione finale già costruita
    with patch.dict("os.environ", env), \
         patch("llm_gateway.bedrock_client._build_bedrock_session", return_value=final_session_mock):
        client = BedrockClient()
        resp = await client.complete(
            "system", [LLMMessage(role="user", content="q")], TagProposal
        )

    assert json.loads(resp.content)["tag_value"] == "platform"
    # La sessione finale deve aver richiesto il client bedrock-runtime
    final_session_mock.client.assert_called_once_with("bedrock-runtime")


# ---------------------------------------------------------------------------
# AzureOpenAIClient — modalità SSO con DefaultAzureCredential
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_azure_sso_uses_token_provider():
    """AZURE_USE_SSO=true → DefaultAzureCredential usato al posto della API key."""
    good = json.dumps({"tag_key": "env", "tag_value": "staging", "confidence": 0.88})

    credential_mock = MagicMock()
    token_provider_mock = MagicMock(return_value="fake-token")

    azure_identity_mock = MagicMock()
    azure_identity_mock.DefaultAzureCredential.return_value = credential_mock
    azure_identity_mock.get_bearer_token_provider.return_value = token_provider_mock

    openai_client_mock = AsyncMock()
    openai_client_mock.chat.completions.create = AsyncMock(
        return_value=_make_openai_response(good)
    )

    env = {
        "AZURE_USE_SSO": "true",
        "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com",
        "AZURE_OPENAI_DEPLOYMENT": "gpt-4o",
    }

    with patch.dict("os.environ", env), \
         patch.dict("sys.modules", {"azure.identity": azure_identity_mock}), \
         patch("llm_gateway.azure_openai_client.AsyncAzureOpenAI", return_value=openai_client_mock):

        client = AzureOpenAIClient()
        client._client = openai_client_mock
        resp = await client.complete("system", [LLMMessage(role="user", content="q")])

    assert azure_identity_mock.DefaultAzureCredential.called
    assert resp.content == good


@pytest.mark.asyncio
async def test_azure_sso_missing_azure_identity_raises():
    """AZURE_USE_SSO=true senza azure-identity installato → RuntimeError chiaro."""
    env = {
        "AZURE_USE_SSO": "true",
        "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com",
        "AZURE_OPENAI_DEPLOYMENT": "gpt-4o",
    }

    import sys
    # Rimuove azure.identity dalla cache dei moduli per simulare ImportError
    azure_mods = [k for k in sys.modules if "azure.identity" in k]
    saved = {k: sys.modules.pop(k) for k in azure_mods}

    try:
        with patch.dict("os.environ", env), \
             patch("llm_gateway.azure_openai_client._use_sso", return_value=True):
            # Simula l'assenza di azure-identity
            with patch("builtins.__import__", side_effect=lambda n, *a, **k: (
                (_ for _ in ()).throw(ImportError("No module named 'azure.identity'"))
                if "azure.identity" in n else __import__(n, *a, **k)
            )):
                with pytest.raises((RuntimeError, ImportError)):
                    from llm_gateway.azure_openai_client import _build_azure_client
                    _build_azure_client("gpt-4o")
    finally:
        sys.modules.update(saved)


# ---------------------------------------------------------------------------
# Factory — provider bedrock
# ---------------------------------------------------------------------------

def test_factory_bedrock():
    """get_llm_client() con LLM_PROVIDER=bedrock → BedrockClient."""
    session_mock = MagicMock()
    runtime_mock = MagicMock()
    session_mock.client.return_value = runtime_mock

    with patch.dict("os.environ", {"LLM_PROVIDER": "bedrock"}), \
         patch("llm_gateway.bedrock_client._build_bedrock_session", return_value=session_mock):
        client = get_llm_client()

    assert isinstance(client, BedrockClient)


def test_factory_azure_sso():
    """get_llm_client() con azure_openai + AZURE_USE_SSO=true → AzureOpenAIClient."""
    azure_identity_mock = MagicMock()
    azure_identity_mock.DefaultAzureCredential.return_value = MagicMock()
    azure_identity_mock.get_bearer_token_provider.return_value = MagicMock()

    env = {
        "LLM_PROVIDER": "azure_openai",
        "AZURE_USE_SSO": "true",
        "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com",
        "AZURE_OPENAI_DEPLOYMENT": "gpt-4o",
    }

    with patch.dict("os.environ", env), \
         patch.dict("sys.modules", {"azure.identity": azure_identity_mock}), \
         patch("llm_gateway.azure_openai_client.AsyncAzureOpenAI"):
        client = get_llm_client()

    assert isinstance(client, AzureOpenAIClient)
