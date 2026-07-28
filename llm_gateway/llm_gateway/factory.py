import os
from .base import LLMClient
from .claude_client import ClaudeClient
from .azure_openai_client import AzureOpenAIClient
from .bedrock_client import BedrockClient


def get_llm_client() -> LLMClient:
    """
    Restituisce il client LLM selezionato da LLM_PROVIDER.

    Provider supportati:
      claude        — Anthropic API diretta (richiede ANTHROPIC_API_KEY)
      azure_openai  — Azure OpenAI con API key oppure Azure AD SSO (AZURE_USE_SSO=true)
      bedrock       — Amazon Bedrock, autenticazione via AWS credential chain / SSO
                      (nessuna chiave statica, usare AWS_PROFILE o AssumeRole)
    """
    provider = os.environ.get("LLM_PROVIDER", "claude").lower()
    if provider == "claude":
        return ClaudeClient()
    if provider == "azure_openai":
        return AzureOpenAIClient()
    if provider == "bedrock":
        return BedrockClient()
    raise ValueError(
        f"LLM_PROVIDER non supportato: {provider!r}. "
        "Valori validi: 'claude', 'azure_openai', 'bedrock'."
    )
