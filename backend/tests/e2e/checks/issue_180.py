"""Provider model capabilities through the real REST API.

#180 keeps ``models`` as a string list and adds ``model_capabilities`` keyed by
model name. This check creates a legacy-shaped provider (no capabilities) and a
provider with explicit capabilities, then verifies reads, validation, and the
update rule that removed models lose their capabilities.
"""

from __future__ import annotations

DEFAULT_CAPABILITY = {"description": "", "vision": False, "effort_levels": []}
PROVIDER = {
    "type": "anthropic-messages",
    "base_url": "https://gateway.example.test",
}


def _post(ctx, name, **body):
    response = ctx.rest.post(
        "/api/v1/providers", json={"name": name, **PROVIDER, **body}
    )
    return response


def check_provider_model_capabilities(ctx):
    legacy = _post(ctx, "legacy provider", models=["legacy-model"])
    ctx.require(
        legacy.status_code == 201,
        f"creating a provider without capabilities failed: {legacy.status_code} {legacy.text}",
    )
    legacy_body = legacy.json()
    ctx.require(
        legacy_body["models"] == ["legacy-model"],
        "models changed for a provider without capabilities",
    )
    ctx.require(
        legacy_body["model_capabilities"] == {"legacy-model": DEFAULT_CAPABILITY},
        f"legacy provider did not read as defaults: {legacy_body['model_capabilities']}",
    )

    configured = _post(
        ctx,
        "capability provider",
        models=["text-model", "vision-model"],
        model_capabilities={
            "vision-model": {
                "description": "看图问答",
                "vision": True,
                "effort_levels": ["max", "low", "max"],
            }
        },
    )
    ctx.require(
        configured.status_code == 201,
        f"creating a provider with capabilities failed: {configured.status_code} {configured.text}",
    )
    configured_body = configured.json()
    capabilities = configured_body["model_capabilities"]
    ctx.require(
        capabilities["text-model"] == DEFAULT_CAPABILITY,
        f"unlisted model did not receive defaults: {capabilities['text-model']}",
    )
    ctx.require(
        capabilities["vision-model"]
        == {
            "description": "看图问答",
            "vision": True,
            "effort_levels": ["low", "max"],
        },
        f"capabilities did not round-trip in canonical order: {capabilities['vision-model']}",
    )

    listed = ctx.rest.get("/api/v1/providers")
    ctx.require(listed.status_code == 200, f"listing providers failed: {listed.text}")
    matching = [row for row in listed.json() if row["id"] == configured_body["id"]]
    ctx.require(len(matching) == 1, "created provider is missing from the list")
    ctx.require(
        matching[0]["model_capabilities"] == capabilities,
        "provider list did not return the saved capabilities",
    )

    bad_level = _post(
        ctx,
        "bad level",
        models=["model-a"],
        model_capabilities={"model-a": {"effort_levels": ["ultra"]}},
    )
    ctx.require(
        bad_level.status_code == 422,
        f"unknown effort level was accepted: {bad_level.status_code}",
    )
    bad_model = _post(
        ctx,
        "bad model",
        models=["model-a"],
        model_capabilities={"model-b": {"description": "not listed"}},
    )
    ctx.require(
        bad_model.status_code == 422,
        f"capability for a missing model was accepted: {bad_model.status_code}",
    )
    long_description = _post(
        ctx,
        "long description",
        models=["model-a"],
        model_capabilities={"model-a": {"description": "x" * 201}},
    )
    ctx.require(
        long_description.status_code == 422,
        f"overlong description was accepted: {long_description.status_code}",
    )

    removed = ctx.rest.put(
        f"/api/v1/providers/{configured_body['id']}",
        json={"models": ["text-model", "new-model"]},
    )
    ctx.require(
        removed.status_code == 200,
        f"updating models failed: {removed.status_code} {removed.text}",
    )
    removed_capabilities = removed.json()["model_capabilities"]
    ctx.require(
        removed_capabilities
        == {
            "text-model": DEFAULT_CAPABILITY,
            "new-model": DEFAULT_CAPABILITY,
        },
        f"removed model kept its capabilities or new model missed defaults: {removed_capabilities}",
    )

    moved = ctx.rest.put(
        f"/api/v1/providers/{configured_body['id']}",
        json={
            "model_capabilities": {
                "new-model": {
                    "description": "新模型",
                    "vision": False,
                    "effort_levels": ["medium", "extra"],
                }
            }
        },
    )
    ctx.require(
        moved.status_code == 200,
        f"updating capabilities failed: {moved.status_code} {moved.text}",
    )
    ctx.require(
        moved.json()["model_capabilities"]
        == {
            "text-model": DEFAULT_CAPABILITY,
            "new-model": {
                "description": "新模型",
                "vision": False,
                "effort_levels": ["medium", "extra"],
            },
        },
        f"explicit capability mapping was not replaced: {moved.json()['model_capabilities']}",
    )

    return {
        "ok": True,
        "info": {
            "legacy_models": legacy_body["models"],
            "capability_models": list(capabilities),
            "validated_422": 3,
        },
    }
