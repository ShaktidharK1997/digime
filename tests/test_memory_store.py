"""Tests for the SQLite memory store, against a temp database."""

import time

import pytest

from tools import memory_store


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    memory_store.close()
    monkeypatch.setattr(memory_store, "DB_PATH", tmp_path / "memory.db")
    memory_store.init_db()
    yield
    memory_store.close()


def test_conversation_roundtrip():
    conv = memory_store.get_or_create_conversation("111.222")
    assert conv["status"] == "active"
    # Second call returns the existing row, not a new one
    again = memory_store.get_or_create_conversation("111.222")
    assert again["created_at"] == conv["created_at"]


def test_message_gists_and_detail():
    memory_store.get_or_create_conversation("c1")
    detail = [{"role": "user", "content": "hi"}]
    memory_store.save_message("c1", "m1", "asked hi", detail)
    memory_store.save_message("c1", "m2", "asked again", detail)

    gists = memory_store.get_message_gists("c1")
    assert [g["message_id"] for g in gists] == ["m1", "m2"]
    assert memory_store.get_message_detail("m1") == detail
    assert memory_store.get_message_detail("missing") is None


def test_recent_gists_newest_first():
    for conv_id in ("a", "b"):
        memory_store.get_or_create_conversation(conv_id)
    memory_store.complete_conversation("a", "gist a")
    time.sleep(0.02)
    memory_store.complete_conversation("b", "gist b")

    recent = memory_store.get_recent_conversation_gists()
    assert [c["conv_id"] for c in recent] == ["b", "a"]


def test_stale_conversation_query():
    memory_store.get_or_create_conversation("old")
    cutoff = time.time() + 60  # everything is older than one minute from now
    assert memory_store.get_stale_active_conversations(cutoff) == ["old"]

    memory_store.complete_conversation("old", "done")
    assert memory_store.get_stale_active_conversations(cutoff) == []


def test_pending_action_lifecycle():
    key = memory_store.create_pending_action("create_task", {"title": "x"}, "c1")

    record = memory_store.get_pending_action(key)
    assert record["tool"] == "create_task"
    assert record["input"] == {"title": "x"}
    assert record["status"] == "pending"

    memory_store.set_pending_action_status(key, "executed")
    assert memory_store.get_pending_action(key)["status"] == "executed"

    assert memory_store.get_pending_action("nope") is None


def test_pending_action_expiry():
    memory_store.create_pending_action("create_task", {"title": "old"}, None)
    expired = memory_store.expire_old_pending_actions(time.time() + 1)
    assert expired == 1


def test_meta_roundtrip():
    assert memory_store.get_meta("last_digest_date") is None
    memory_store.set_meta("last_digest_date", "2026-07-04")
    assert memory_store.get_meta("last_digest_date") == "2026-07-04"
    memory_store.set_meta("last_digest_date", "2026-07-05")
    assert memory_store.get_meta("last_digest_date") == "2026-07-05"
