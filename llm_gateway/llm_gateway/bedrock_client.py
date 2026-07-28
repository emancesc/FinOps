"""
BedrockClient — Claude via Amazon Bedrock con autenticazione AWS SSO.

Autenticazione (nessuna chiave statica):
  1. AWS SSO profile:  AWS_PROFILE=my-sso-profile  (dopo "aws configure sso" + "aws sso login")
  2. AssumeRole:       AWS_ASSUME_ROLE_ARN=arn:aws:iam::...:role/FinOpsLLM
  3. Instance profile / ECS task role (ambienti cloud)
  4. Env vars AWS standard (AWS_ACCESS_KEY_ID / SECRET) come fallback di emergenza
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from pydantic import BaseModel, ValidationError

from .base import LLMClient, LLMMessage, LLMResponse

_MAX_RETRIES = 2
_JSON_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)

# Model ID di default — sostituire con il modello disponibile nella propria region
_DEFAULT_MODEL = "us.anthropic.claude-3-5-sonnet-20241022-v2:0"


def _strip_fence(text: str) -> str:
    m = _JSON_FENCE.search(text)
    return m.group(1).strip() if m else text.strip()


def _build_bedrock_session():
    """Costruisce la sessione boto3 rispettando la policy di autenticazione SSO/AssumeRole."""
    try:
        import boto3
    except ImportError as e:
        raise RuntimeError(
            "boto3 non installato. Eseguire: pip install boto3"
        ) from e

    profile = os.environ.get("AWS_PROFILE")
    region = os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    role_arn = os.environ.get("BEDROCK_ASSUME_ROLE_ARN") or os.environ.get("AWS_ASSUME_ROLE_ARN")

    session = boto3.Session(profile_name=profile, region_name=region)

    if role_arn:
        sts = session.client("sts")
        assumed = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName="finops-llm-gateway",
        )["Credentials"]
        session = boto3.Session(
            aws_access_key_id=assumed["AccessKeyId"],
            aws_secret_access_key=assumed["SecretAccessKey"],
            aws_session_token=assumed["SessionToken"],
            region_name=region,
        )

    return session


class BedrockClient(LLMClient):
    """
    Client LLM che chiama Claude su Amazon Bedrock.
    Non richiede ANTHROPIC_API_KEY — si autentica tramite AWS credential chain.
    """

    def __init__(self) -> None:
        session = _build_bedrock_session()
        self._runtime = session.client("bedrock-runtime")
        self._model = os.environ.get("BEDROCK_MODEL_ID", _DEFAULT_MODEL)

    def _invoke(self, body: dict) -> dict:
        """Chiamata sincrona a Bedrock (eseguita in executor per non bloccare l'event loop)."""
        response = self._runtime.invoke_model(
            modelId=self._model,
            body=json.dumps(body),
            contentType="application/json",
            accept="application/json",
        )
        return json.loads(response["body"].read())

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
        loop = asyncio.get_event_loop()

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

            body = {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": max_tokens,
                "system": system,
                "messages": api_messages,
            }

            result = await loop.run_in_executor(None, self._invoke, body)
            last_content = result["content"][0]["text"]
            usage = result.get("usage", {})

            if response_format is None:
                return LLMResponse(
                    content=last_content,
                    model=self._model,
                    input_tokens=usage.get("input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0),
                )

            try:
                parsed = json.loads(_strip_fence(last_content))
                response_format.model_validate(parsed)
                return LLMResponse(
                    content=last_content,
                    model=self._model,
                    input_tokens=usage.get("input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0),
                )
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = str(exc)

        raise ValueError(
            f"Impossibile ottenere JSON valido dopo {_MAX_RETRIES + 1} tentativi: {last_error}"
        )
