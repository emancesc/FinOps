from __future__ import annotations
import json
import os
import re
from pydantic import BaseModel, ValidationError
import anthropic

from .base import LLMClient, LLMMessage, LLMResponse

_MAX_RETRIES = 2
_JSON_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def _strip_fence(text: str) -> str:
    m = _JSON_FENCE.search(text)
    return m.group(1).strip() if m else text.strip()


class ClaudeClient(LLMClient):
    def __init__(self) -> None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY non impostata")
        self._model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    async def complete(
        self,
        system: str,
        messages: list[LLMMessage],
        response_format: type[BaseModel] | None = None,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        api_messages = [{"role": m.role, "content": m.content} for m in messages]
        last_content: str | None = None
        last_error: str | None = None

        for attempt in range(_MAX_RETRIES + 1):
            if last_content is not None and attempt > 0:
                # Mostra al modello la sua risposta errata e chiedi correzione
                api_messages.append({"role": "assistant", "content": last_content})
                api_messages.append({
                    "role": "user",
                    "content": (
                        "La risposta precedente non era JSON valido o non rispettava lo schema. "
                        f"Errore: {last_error}. Rispondi SOLO con JSON valido, senza markdown."
                    ),
                })

            resp = await self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=system,
                messages=api_messages,
            )
            last_content = resp.content[0].text

            if response_format is None:
                return LLMResponse(
                    content=last_content,
                    model=self._model,
                    input_tokens=resp.usage.input_tokens,
                    output_tokens=resp.usage.output_tokens,
                )

            try:
                parsed = json.loads(_strip_fence(last_content))
                response_format.model_validate(parsed)
                return LLMResponse(
                    content=last_content,
                    model=self._model,
                    input_tokens=resp.usage.input_tokens,
                    output_tokens=resp.usage.output_tokens,
                )
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = str(exc)

        raise ValueError(
            f"Impossibile ottenere JSON valido dopo {_MAX_RETRIES + 1} tentativi: {last_error}"
        )
