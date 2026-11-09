import faulthandler
import os
import signal
import sys

import pytest

os.environ.setdefault("TEST_EMBEDDING_URL", "http://172.16.242.124:31613")
os.environ.setdefault("TEST_EMBEDDING_MODEL", "model/bge-small-zh-v1.5")

DEFAULT_TEST_TIMEOUT_SECONDS = 300


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "extended_timeout(seconds): allow a known-slow test more than the "
        "default 300-second hang guard",
    )


@pytest.fixture(autouse=True)
def _hang_guard(request):
    """Fail a test that runs past its limit, dumping every thread's stack first."""
    if not hasattr(signal, "SIGALRM"):
        yield
        return
    marker = request.node.get_closest_marker("extended_timeout")
    seconds = marker.args[0] if marker and marker.args else DEFAULT_TEST_TIMEOUT_SECONDS

    def expire(_signum, _frame):
        faulthandler.dump_traceback(file=sys.__stderr__, all_threads=True)
        raise TimeoutError(f"{request.node.nodeid} exceeded {seconds} seconds")

    previous = signal.signal(signal.SIGALRM, expire)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def cleanup_test_generations(driver, vectors, configured_generation):
    """Delete and verify only generation names recorded in this test's app DB."""
    import sqlite3

    names = {configured_generation}
    with sqlite3.connect(os.environ["DATAAGENT_DB_PATH"]) as db:
        names.update(
            row[0]
            for row in db.execute(
                "SELECT generation FROM builds WHERE generation IS NOT NULL"
            )
        )
        names.update(
            row[0] for row in db.execute("SELECT name FROM knowledge_generations")
        )
    for name in names:
        driver.execute_query(
            "MATCH (n:SDAEntity {generation:$generation}) DETACH DELETE n",
            generation=name,
        )
        rows, _, _ = driver.execute_query(
            "MATCH (n:SDAEntity {generation:$generation}) RETURN count(n) AS count",
            generation=name,
        )
        assert rows[0]["count"] == 0, f"Neo4j generation remains: {name}"
        collection = "sda_" + name
        if vectors.has_collection(collection):
            vectors.drop_collection(collection)
        assert not vectors.has_collection(collection), (
            f"Milvus collection remains: {collection}"
        )
