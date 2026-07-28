"""
AzureOpenAIClient — supporta sia API key che Azure AD SSO.

Modalità di autenticazione selezionata da AZURE_USE_SSO:
  AZURE_USE_SSO=false (default): autenticazione con AZURE_OPENAI_API_KEY
  AZURE_USE_SSO=true:            DefaultAzureCredential (Azure CLI, MSAL, Managed Identity,
                                 Workload Identity Federation) — nessuna chiave statica
"""
from __future__ import annotations

import json
import os
import re
from pydantic import BaseModel, ValidationError
from openai import AsyncAzureOpenAI

from .base import LLMClient, LLMMessage, LLMResponse

_MAX_RETRIES = 2
_JSON_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def _strip_fence(text: str) -> str:
    m = _JSON_FENCE.search(text)
    return m.group(1).strip() if m else text.strip()


def _use_sso() -> bool:
    return os.environ.get("AZURE_USE_SSO", "false").lower() in ("1", "true", "yes")


def _build_azure_client(deployment: str) -> AsyncAzureOpenAI:
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    if not endpoint or not deployment:
        raise RuntimeError(
            "AZURE_OPENAI_ENDPOINT e AZURE_OPENAI_DEPLOYMENT devono essere impostate"
        )

    if _use_sso():
        try:
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        except ImportError as e:
            raise RuntimeError(
                "azure-identity non installato. Eseguire: pip install azure-identity"
            ) from e
        # DefaultAzureCredential prova in ordine: env vars, workload identity,
        # managed identity, Azure CLI ("az login"), Azure Developer CLI, VS Code.
        credential = DefaultAzureCredential()
        token_provider = get_bearer_token_provider(
            credential, "https://cognitiveservices.azure.com/.default"
        )
        return AsyncAzureOpenAI(
            azure_endpoint=endpoint,
            azure_ad_token_provider=token_provider,
            api_version="2024-08-01-preview",
        )
    else:
        api_key = os.environ.get("AZURE_OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "AZURE_OPENAI_API_KEY non impostata. "
                "Impostare la chiave oppure abilitare AZURE_USE_SSO=true per l'autenticazione Azure AD."
            )
        return AsyncAzureOpenAI(
            api_key=api_key,
            azure_endpoint=endpoint,
            api_version="2024-08-01-preview",
        )


class AzureOpenAIClient(LLMClient):
    def __init__(self) -> None:
        self._deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "")
        self._client = _build_azure_client(self._deployment)

    async def complete(
        self,
        system: str,
        messages: list[LLMMessage],
        response_format: type[BaseModel] | None = None,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        api_messages: list[dict] = [{"role": "system", "content": system}]
        api_messages += [{"role": m.role, "content": m.content} for m in messages]
        last_content: str | None = None
        last_error: str | None = None

        for attempt in range(_MAX_RETRIES + 1):
            if last_content is not None and attempt > 0:
                api_messages.append({"role": "assistant", "content": last_content})
                api_messages.append({
                    "role": "user",
                    "content": (
                        "La risposta precedente non era JSON valido o non rispettava lo schema. "
                        f"Errore: {last_error}. Rispondi SOLO con JSON valido, senza markdown."
                    ),
                })

            resp = await self._client.chat.completions.create(
                model=self._deployment,
                messages=api_messages,
                max_tokens=max_tokens,
            )
            last_content = resp.choices[0].message.content or ""

            if response_format is None:
                return LLMResponse(
                    content=last_content,
                    model=self._deployment,
                    input_tokens=resp.usage.prompt_tokens,
                    output_tokens=resp.usage.completion_tokens,
                )

            try:
                parsed = json.loads(_strip_fence(last_content))
                response_format.model_validate(parsed)
                return LLMResponse(
                    content=last_content,
                    model=self._deployment,
                    input_tokens=resp.usage.prompt_tokens,
                    output_tokens=resp.usage.completion_tokens,
                )
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = str(exc)

        raise ValueError(
            f"Impossibile ottenere JSON valido dopo {_MAX_RETRIES + 1} tentativi: {last_error}"
        )
