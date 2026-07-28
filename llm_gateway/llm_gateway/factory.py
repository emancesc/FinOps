import os
from .base import LLMClient
from .claude_client import ClaudeClient
from .azure_openai_client import AzureOpenAIClient


def get_llm_client() -> LLMClient:
    """Restituisce il client LLM selezionato da LLM_PROVIDER (claude | azure_openai)."""
    provider = os.environ.get("LLM_PROVIDER", "claude").lower()
    if provider == "claude":
        return ClaudeClient()
    if provider == "azure_openai":
        return AzureOpenAIClient()
    raise ValueError(f"LLM_PROVIDER non supportato: {provider!r}. Usa 'claude' o 'azure_openai'.")
