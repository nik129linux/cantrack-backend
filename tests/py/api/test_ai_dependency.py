"""The installed cantrack-ai must carry the decompression-bomb cap (uv.lock pins it by commit)."""

from cantrack_ai.embeddings import MAX_IMAGE_PIXELS


def test_the_locked_cantrack_ai_refuses_bombs():
    # Dog and check-in photos are decoded by cantrack_ai.preprocess inside a
    # 512 MB container; a lock pointing at an older commit would reopen the hole.
    assert MAX_IMAGE_PIXELS <= 25_000_000
