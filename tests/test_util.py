from mesh_ai_gateway.util import split_utf8, truncate_utf8


def test_split_utf8_respects_limit():
    chunks = split_utf8("🦑" * 200, 80)
    assert len(chunks) > 1
    assert all(len(chunk.encode("utf-8")) <= 80 for chunk in chunks)


def test_truncate_utf8_respects_limit():
    value = truncate_utf8("Baltimore 🦑 " * 100, 100)
    assert len(value.encode("utf-8")) <= 100
    assert value.endswith("…")
