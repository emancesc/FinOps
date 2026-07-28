from abc import ABC, abstractmethod
from typing import Any
from pydantic import BaseModel


class LLMMessage(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class LLMResponse(BaseModel):
    content: str
    model: str
    input_tokens: int
    output_tokens: int


class LLMClient(ABC):
    """Interfaccia comune per tutti i provider LLM."""

    @abstractmethod
    async def complete(
        self,
        system: str,
        messages: list[LLMMessage],
        response_format: type[BaseModel] | None = None,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        """
        Invia una richiesta al modello LLM.
        Se response_format è fornito, il contenuto JSON viene validato e ri-tentato in caso di errore.
        """
        ...
