"""Verify the harness defaults reach the running application's settings."""


def check_default_embedding_settings(ctx):
    response = ctx.rest.get("/api/v1/settings")
    response.raise_for_status()
    settings = response.json()
    ctx.require(
        settings.get("embedding_url") == "http://172.16.242.124:31613",
        "expected the local small-model endpoint",
    )
    ctx.require(
        settings.get("embedding_model") == "model/bge-small-zh-v1.5",
        "expected the small embedding model",
    )
    return {
        "embedding_url": settings["embedding_url"],
        "embedding_model": settings["embedding_model"],
    }
