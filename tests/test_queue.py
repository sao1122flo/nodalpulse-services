"""Smoke test: pg_queue module imports cleanly."""

from nodalpulse.queue import pg_queue


def test_module_importable() -> None:
    assert hasattr(pg_queue, "enqueue")
    assert hasattr(pg_queue, "dequeue")
    assert hasattr(pg_queue, "run_worker")
