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


class AzureOpenAIClient(LLMClient):
    def __init__(self) -> None:
        api_key = os.environ.get("AZURE_OPENAI_API_KEY")
        endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
        self._deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT")
        if not all([api_key, endpoint, self._deployment]):
            raise RuntimeError(
                "AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT e AZURE_OPENAI_DEPLOYMENT devono essere impostate"
            )
        self._client = AsyncAzureOpenAI(
            api_key=api_key,
            azure_endpoint=endpoint,
            api_version="2024-08-01-preview",
        )

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
