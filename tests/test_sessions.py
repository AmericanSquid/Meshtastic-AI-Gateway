from mesh_ai_gateway.sessions import SessionStore


def test_sessions_are_per_sender():
    store = SessionStore(2)
    store.append("!a", "user", "one")
    store.append("!b", "user", "two")
    assert store.get("!a")[0]["content"] == "one"
    assert store.get("!b")[0]["content"] == "two"
