"""
Registro Tagging Strategy: upload, estrazione LLM a blocchi (con LLM finto), merge dei
valori ammessi tra blocchi, metadati, ripresa dopo errore, attivazione e revisione.
Richiede il PostgreSQL locale con le migrazioni applicate (scripts/apply_migrations.py).
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("DATABASE_URL", "postgresql://finops:changeme@localhost:5432/finops")

from llm_gateway.base import LLMClient, LLMResponse  # noqa: E402


class _ChunkLLM(LLMClient):
    """Risponde in base al numero di blocco; può fallire una volta su un blocco scelto."""

    def __init__(self, fail_on_block: int | None = None):
        self.calls = []
        self.fail_on_block = fail_on_block

    async def complete(self, system, messages, response_format=None, max_tokens=4096):
        text = messages[-1].content
        block = int(text.split("Blocco ")[1].split(" ")[0])
        self.calls.append(block)
        if self.fail_on_block == block:
            self.fail_on_block = None
            raise RuntimeError("errore temporaneo LLM")
        if block == 1:
            payload = {
                "meta": {"name": "Test Tagging Strategy", "revision": "9.9", "release_date": "2026-09-25",
                         "summary": "Strategia di test",
                         "changelog": [{"version": "9.9", "date": "2026-09-25", "changes": ["prima versione"]}]},
                "tags": [{"tag_key": "test:Customer", "category": "cost_allocation", "mandatory": True, "billing": True,
                          "multi_value": True, "separator": "+",
                          "allowed_values": [{"value": "UNIBO", "description": "Bologna", "business_unit": "UNIV"}]}],
                "rules": [{"rule_type": "value_constraint", "tag_keys": ["test:Customer"], "title": "Separatore +",
                           "description": "I valori multipli sono separati da +"}],
                "last_tag_in_progress": "test:Customer",
            }
        else:
            assert "Tag in corso dal blocco precedente: test:Customer" in text
            payload = {
                "tags": [{"tag_key": "test:Customer",
                          "allowed_values": [{"value": "UNIBO"}, {"value": "POLIMI", "business_unit": "UNIV"}]},
                         {"tag_key": "test:Role", "category": "operational"}],
                "rules": [{"rule_type": "value_constraint", "tag_keys": ["test:Customer"], "title": "separatore +",
                           "description": "duplicato della regola del blocco 1"},
                          {"rule_type": "resource_type", "tag_keys": ["test:Role"], "title": "Role vuoto per SQS",
                           "description": "Lasciare vuoto cineca:Role per SQS",
                           "condition": {"resource_types": ["AWS::SQS::Queue"]}, "resolution": {"test:Role": ""}}],
                "last_tag_in_progress": None,
            }
        return LLMResponse(content=json.dumps(payload), model="mock", input_tokens=100, output_tokens=50)


@pytest_asyncio.fixture
async def client():
    import app.main as main
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as c:
        # I test girano sul DB reale e attivare una strategy disattiva le altre:
        # a fine test si riattiva quella che era attiva prima.
        r = await c.get("/strategies/active")
        previous_active = r.json()["strategy_id"] if r.status_code == 200 else None
        try:
            yield c, main
        finally:
            if previous_active:
                await c.post(f"/strategies/{previous_active}/activate")
    main._llm_override = None


def _document(tmp_path):
    # 4 "pagine" da 3000 caratteri -> 2 blocchi (9000 + 3000)
    path = tmp_path / "strategy_test.txt"
    path.write_text("x" * 12000, encoding="utf-8")
    return path


async def _wait_extraction(main, strategy_id):
    task = main._extraction_tasks.get(strategy_id)
    if task:
        await asyncio.wait_for(task, timeout=30)


async def _cleanup(c, strategy_id):
    await c.delete(f"/strategies/{strategy_id}")


@pytest.mark.asyncio
async def test_upload_extract_merge_and_activate(client, tmp_path):
    c, main = client
    llm = _ChunkLLM()
    main._llm_override = llm
    path = _document(tmp_path)
    with open(path, "rb") as f:
        r = await c.post("/strategies", files={"file": ("strategy_test.txt", f, "text/plain")})
    assert r.status_code == 201, r.text
    sid = r.json()["strategy_id"]
    try:
        assert r.json()["revision"].startswith("bozza-")  # provvisoria finché l'LLM non legge il documento
        await _wait_extraction(main, sid)
        detail = (await c.get(f"/strategies/{sid}")).json()

        assert llm.calls == [1, 2]
        assert detail["status"] == "extracted" and detail["progress_pct"] == 100.0
        assert detail["name"] == "Test Tagging Strategy" and detail["revision"] == "9.9"
        assert detail["release_date"] == "2026-09-25"
        assert detail["changelog"][0]["version"] == "9.9"

        tags = {t["tag_key"]: t for t in detail["tags"]}
        customer = tags["test:Customer"]
        # righe della tabella dei valori ammessi unite tra i due blocchi, senza duplicati
        assert [v["value"] for v in customer["allowed_values"]] == ["UNIBO", "POLIMI"]
        assert customer["mandatory"] and customer["billing"] and customer["multi_value"]
        assert customer["category"] == "cost_allocation" and tags["test:Role"]["category"] == "operational"

        titles = [r["title"] for r in detail["rules"]]
        assert titles.count("Separatore +") == 1 and "separatore +" not in titles
        sqs = next(r for r in detail["rules"] if r["rule_type"] == "resource_type")
        assert sqs["condition"] == {"resource_types": ["AWS::SQS::Queue"]}

        listed = next(s for s in (await c.get("/strategies")).json() if s["strategy_id"] == sid)
        assert listed["tags_count"] == 2 and listed["rules_count"] == 2 and listed["allowed_values_count"] == 2

        r = await c.post(f"/strategies/{sid}/approve-all")
        assert r.json()["tags_approved"] == 2
        r = await c.patch(f"/strategies/{sid}/rules/{sqs['rule_id']}", json={"status": "rejected"})
        assert r.status_code == 200

        r = await c.post(f"/strategies/{sid}/activate")
        assert r.status_code == 200
        active = (await c.get("/strategies/active")).json()
        assert active["strategy_id"] == sid
        assert all(rule["status"] != "rejected" for rule in active["rules"])
        assert len(active["rules"]) == 1
    finally:
        await _cleanup(c, sid)


@pytest.mark.asyncio
async def test_extraction_error_then_resume(client, tmp_path):
    c, main = client
    llm = _ChunkLLM(fail_on_block=2)
    main._llm_override = llm
    path = _document(tmp_path)
    with open(path, "rb") as f:
        r = await c.post("/strategies", files={"file": ("strategy_test.txt", f, "text/plain")},
                         data={"name": "Resume Strategy", "revision": "0.1", "release_date": "2026-01-15"})
    sid = r.json()["strategy_id"]
    try:
        await _wait_extraction(main, sid)
        detail = (await c.get(f"/strategies/{sid}")).json()
        assert detail["status"] == "error" and detail["chunks_done"] == 1
        assert "blocco 2" in detail["error"]
        # attivazione negata finché l'estrazione non è completa
        assert (await c.post(f"/strategies/{sid}/activate")).status_code == 409

        r = await c.post(f"/strategies/{sid}/extract")
        assert r.status_code == 202
        await _wait_extraction(main, sid)
        detail = (await c.get(f"/strategies/{sid}")).json()
        assert detail["status"] == "extracted"
        assert llm.calls == [1, 2, 2]  # il blocco 1 non viene richiesto di nuovo
        # i metadati inseriti dall'utente prevalgono su quelli letti dal documento
        assert (detail["name"], detail["revision"], detail["release_date"]) == ("Resume Strategy", "0.1", "2026-01-15")
        values = [v["value"] for v in next(t for t in detail["tags"] if t["tag_key"] == "test:Customer")["allowed_values"]]
        assert values == ["UNIBO", "POLIMI"]
    finally:
        await _cleanup(c, sid)
