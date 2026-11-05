import os

os.environ.setdefault("TEST_EMBEDDING_URL", "http://127.0.0.1:17998")
os.environ.setdefault("TEST_EMBEDDING_MODEL", "model/bge-small-zh-v1.5")


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
