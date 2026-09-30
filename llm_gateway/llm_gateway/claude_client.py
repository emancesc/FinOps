from __future__ import annotations
import json
import os
import re
from pydantic import BaseModel, ValidationError
import anthropic

from .base import LLMClient, LLMMessage, LLMResponse

_MAX_RETRIES = 2
_STREAMING_THRESHOLD = 16000
_JSON_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def _strip_fence(text: str) -> str:
    m = _JSON_FENCE.search(text)
    return m.group(1).strip() if m else text.strip()


class ClaudeClient(LLMClient):
    def __init__(self) -> None:
        api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY non impostata")
        if not api_key.isascii() or " " in api_key or "#" in api_key:
            # tipico di un segnaposto con commento nel .env (es. "...  # <- inserire la chiave")
            raise RuntimeError("ANTHROPIC_API_KEY non valida: sembra un segnaposto o contiene un commento; "
                               "impostare la chiave reale nel file .env")
        self._model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
        # Chiavi di organizzazione non legate a un workspace: l'API richiede l'header
        # anthropic-workspace-id con l'ID del workspace da usare (ANTHROPIC_WORKSPACE_ID).
        workspace_id = (os.environ.get("ANTHROPIC_WORKSPACE_ID") or "").strip()
        headers = {"anthropic-workspace-id": workspace_id} if workspace_id else None
        self._client = anthropic.AsyncAnthropic(api_key=api_key, default_headers=headers)

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

            # System prompt in cache: le chiamate ripetute (es. una per batch di
            # risorse) condividono lo stesso prefisso con le regole della strategy.
            request = dict(
                model=self._model,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=api_messages,
            )
            if max_tokens > _STREAMING_THRESHOLD:
                # Output lunghi: streaming per non incorrere nei timeout HTTP
                async with self._client.messages.stream(**request) as stream:
                    resp = await stream.get_final_message()
            else:
                resp = await self._client.messages.create(**request)
            # Solo i blocchi di testo (eventuali blocchi thinking non hanno "text")
            last_content = "".join(b.text for b in resp.content if isinstance(getattr(b, "text", None), str))

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
