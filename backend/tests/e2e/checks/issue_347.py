"""Exercise evaluation identity through a separate HTTP client (#347)."""

import os
import sys
import time
import uuid
from pathlib import Path

import httpx

sys.path.append(str(Path(__file__).resolve().parent))
from _conversation import create_provider, parse_sse
from issue_234 import Receiver, _configure


def check_eval_traffic_isolation(ctx):
    from semantica_dataagent.db import Turn, sessions
    from semantica_dataagent.evolution_runner import _filter_and_scrub_conversations
    from semantica_dataagent.plugins import TraceConversation, TraceMessage
    from semantica_dataagent.telemetry import trace_id

    os.environ["DATAAGENT_DB_PATH"] = str(ctx.runtime.db_path)
    suffix = uuid.uuid4().hex[:8]
    receiver = Receiver().start()
    token = None
    try:
        _configure(ctx, receiver, capture="true")
        provider, model = create_provider(
            ctx, name=f"i347_{suffix}", secret="i347_test_secret"
        )
        with httpx.Client(
            base_url=ctx.runtime.base_url,
            headers={"Authorization": f"Bearer {ctx.token}"},
            timeout=600,
            trust_env=False,
        ) as remote:
            issued = remote.post("/api/v1/eval-tokens", json={"project_id": "default"})
            ctx.require(issued.status_code == 201, issued.text[:300])
            token = issued.json()["token"]
            payload = {
                "question": "i347_请先澄清统计口径，询问我按月还是按年，不要猜测。",
                "agent": "pi",
                "provider_id": provider,
                "model": model,
            }
            headers = {"X-Dataagent-Eval-Token": token}

            def ask(body, extra):
                response = remote.post(
                    "/api/v1/conversations/ask", json=body, headers=extra
                )
                ctx.require(response.status_code == 200, response.text[:300])
                events = parse_sse(response.text)
                ctx.require(bool(events), "missing SSE events")
                return events

            first_events = ask(payload, headers)
            ctx.require(
                any(name == "clarification" for name, _ in first_events),
                "expected clarification",
            )
            first = first_events[0][1]
            second = ask(
                {
                    **payload,
                    "question": "i347_按月",
                    "conversation_id": first["conversation_id"],
                },
                headers,
            )[0][1]
            revoked = remote.post("/api/v1/eval-tokens/revoke", json={"token": token})
            ctx.require(revoked.status_code == 204, f"revoke: {revoked.status_code}")
            token = None
            third = ask(payload, headers)[0][1]
            user = remote.post(
                "/api/v1/tokens", json={"name": f"i347_user_{suffix}", "role": "user"}
            )
            ctx.require(user.status_code == 201, f"user token: {user.status_code}")
            fourth = ask(
                {**payload, "source": "eval"},
                {"Authorization": f"Bearer {user.json()['token']}", "X-Source": "eval"},
            )[0][1]
            turns = [first, second, third, fourth]
            with sessions()() as db:
                sources = [db.get(Turn, item["turn_id"]).source for item in turns]
            ctx.require(
                sources == ["eval", "eval", "online", "online"], f"sources: {sources}"
            )
            for item in (first, second):
                result = remote.post(
                    f"/api/v1/conversations/{item['conversation_id']}/turns/{item['turn_id']}/feedback",
                    json={},
                )
                ctx.require(
                    result.status_code == 409, f"feedback: {result.status_code}"
                )
            expected = [
                trace_id(item["conversation_id"], item["turn_id"]) for item in turns
            ]
            deadline = time.monotonic() + 20
            by_id = {}
            while time.monotonic() < deadline:
                by_id = {
                    entry.get("id"): entry
                    for entry in receiver.spans("online-conversation")
                }
                if all(item in by_id for item in expected):
                    break
                time.sleep(0.1)
            ctx.require(all(item in by_id for item in expected), "missing trace")
            metadata = [by_id[item].get("metadata", {}) for item in expected]
            ctx.require(
                [item.get("source") for item in metadata] == sources,
                "trace source mismatch",
            )
            candidates = [
                TraceConversation(
                    trace_id=tid,
                    project_id="default",
                    messages=[TraceMessage(role="user", content="i347_question")],
                    metadata=meta,
                )
                for tid, meta in zip(expected, metadata, strict=True)
            ]
            accepted = _filter_and_scrub_conversations(
                candidates, project_id="default", secrets={}
            )
            ctx.require(
                {row.trace_id for row in accepted} == set(expected[2:]),
                "evaluation traces entered evolution",
            )
            return {"sources": sources}
    finally:
        if token:
            ctx.rest.post("/api/v1/eval-tokens/revoke", json={"token": token})
        ctx.rest.put("/api/v1/settings", json={"values": {"langfuse_enabled": "false"}})
        receiver.close()
