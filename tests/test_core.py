import os
import time

os.environ["PATINHO_DB"] = "/tmp/patinho-test.db"
os.environ.pop("PATINHO_LLM_KEY", None)   # tests run the deterministic engine

import pytest

from app import agent, core


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    monkeypatch.setenv("PATINHO_DB", db)
    monkeypatch.setattr(core, "DB_PATH", db)
    core.init()
    core.seed()
    yield


def test_seed_tenants():
    assert core.tenant("barbearia-demo")["name"].startswith("Barbearia")
    assert [s["name"] for s in core.services(core.tenant_id("barbearia-demo"))][0] == "Corte"


def test_booking_and_conflict():
    tid = core.tenant_id("barbearia-demo")
    day = time.mktime(time.strptime("2026-12-01", "%Y-%m-%d"))
    slots = core.free_slots(tid, day)
    assert slots, "deveria haver horários livres num dia útil"
    start = slots[3]
    aid = core.book(tid, "Corte", "Carlos", start, start + 1800)
    assert aid
    assert core.book(tid, "Corte", "Outro", start, start + 1800) is None, "não pode aceitar overbooking"
    assert start not in core.free_slots(tid, day)


def test_rule_engine_books_full_flow():
    tid = core.tenant_id("barbearia-demo")
    t = core.tenant("barbearia-demo")
    cid = core.conversation(tid)
    turns = [
        "oi, quero cortar cabelo amanhã às 15h",
        "meu nome é Carlos",
    ]
    replies = []
    for text in turns:
        core.add_message(cid, "user", text)
        reply, engine, _meta = agent.turn(tid, t, core.history(cid))
        core.add_message(cid, "assistant", reply)
        replies.append((reply, engine))
    assert replies[0][1] == "rule", "sem chave, o motor determinístico responde"
    assert "nome" in replies[0][0].lower()
    assert "reservado" in replies[1][0].lower() or "pronto" in replies[1][0].lower()
    appts = core.appointments(tid)
    assert any(a["client_name"] == "Carlos" for a in appts)


def test_usage_counters_and_period():
    tid = core.tenant_id("salao-demo")
    assert core.usage(tid) == 0
    core.bump_usage(tid)
    core.bump_usage(tid)
    assert core.usage(tid) == 2
    assert len(core.period()) == 7


def test_tenant_isolation():
    a = core.tenant_id("barbearia-demo")
    b = core.tenant_id("salao-demo")
    day = time.mktime(time.strptime("2026-12-02", "%Y-%m-%d"))
    s = core.free_slots(a, day)[0]
    core.book(a, "Corte", "Ana", s, s + 1800)
    assert core.appointments(b) == [], "agenda de um cliente não vaza para o outro"


def test_lgpd_purge_respects_retention():
    tid = core.tenant_id("barbearia-demo")
    old = core.conversation(tid)
    core.add_message(old, "user", "conversa antiga")
    with core.connect() as con:
        con.execute("update conversations set created_at=? where id=?", (int(time.time()) - 400 * 86400, old))
    assert core.purge(tid, 180) == 1
    assert core.history(old) == []


def test_lgpd_delete_and_export():
    tid = core.tenant_id("barbearia-demo")
    cid = core.conversation(tid)
    core.add_message(cid, "user", "oi")
    data = core.subject_data(tid, cid[-6:])
    assert data["messages"], "export deve devolver a conversa"
    out = core.delete_subject(tid, cid[-6:])
    assert out["messages_deleted"] == 1
    assert core.history(cid) == []


def test_own_booking_is_not_offered_as_busy():
    """BUG-R1: depois de marcar, o proprio horario nao pode voltar como ocupado."""
    tid = core.tenant_id("barbearia-demo")
    day = time.mktime(time.strptime("2026-12-03", "%Y-%m-%d"))
    cid = core.conversation(tid)
    start = core.free_slots(tid, day)[0]
    aid = core.book(tid, "Barba", "Ana", start, start + 1800, conversation_id=cid)
    assert aid
    # o proprio horario continua livre para a conversa que o marcou...
    assert start in core.free_slots(tid, day, ignore_conversation=cid)
    # ...e segue ocupado para qualquer outra conversa
    assert start not in core.free_slots(tid, day, ignore_conversation="outra-conversa")
    mine = core.conversation_appointments(tid, cid)
    assert [a["id"] for a in mine] == [aid]
    # o prompt passa a afirmar o agendamento, para o modelo nao negar o que marcou
    prompt = agent._system(tid, core.tenant("barbearia-demo"), cid)
    assert "Agendamentos desta conversa" in prompt and f"#{aid}" in prompt


def test_rule_engine_records_conversation():
    tid = core.tenant_id("barbearia-demo")
    t = core.tenant("barbearia-demo")
    cid = core.conversation(tid)
    for text in ["quero barba amanhã às 10h", "meu nome é Ana"]:
        core.add_message(cid, "user", text)
        reply, engine, _ = agent.turn(tid, t, core.history(cid), cid)
        core.add_message(cid, "assistant", reply)
    assert engine == "rule"
    mine = core.conversation_appointments(tid, cid)
    assert len(mine) == 1 and mine[0]["client_name"] == "Ana"
