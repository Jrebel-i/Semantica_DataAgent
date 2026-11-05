"""Privacy and failure isolation at the telemetry boundary."""

import importlib.util


def telemetry():
    spec = importlib.util.find_spec("semantica_dataagent.telemetry")
    assert spec is not None, "telemetry reporting boundary is missing"
    return __import__("semantica_dataagent.telemetry", fromlist=["report"])


def test_report_discards_fields_outside_the_allowlist():
    module = telemetry()
    sent = []
    module.report(
        True,
        {
            "name": "validate_sql",
            "token": "secret-token",
            "api_key": "secret-key",
            "extra": "private",
        },
        sent.append,
    )
    assert sent == [{"name": "validate_sql"}]
    assert "secret" not in str(sent)


def test_disabled_reporting_does_not_call_sender():
    module = telemetry()
    sent = []
    module.report(False, {"name": "validate_sql"}, sent.append)
    assert sent == []


def test_reporting_failure_does_not_interrupt_caller():
    module = telemetry()

    def failed(_payload):
        raise TimeoutError("telemetry unavailable")

    def answer():
        module.report(True, {"name": "validate_sql"}, failed)
        return "answer"

    assert answer() == "answer"


def test_filter_removes_nested_fields_and_known_secrets():
    module = telemetry()
    filtered = module._filtered(
        {
            "input": {
                "query": "lookup private-credential",
                "token": "private-credential",
                "extra": {"sql": "hidden"},
            },
            "output": "private-credential",
        },
        ("private-credential",),
    )
    assert filtered == {"input": {"query": "lookup [REDACTED]"}, "output": "[REDACTED]"}


def test_application_secrets_and_common_credentials_are_redacted():
    module = telemetry()
    text = "Bearer opaque sk-abc123 password=hunter2 api_key=private token=hidden neo4j-secret ordinary"
    result = module._filtered(
        {
            "text": text,
            "Authorization": "header-secret",
            "Cookie": "cookie-secret",
            "session_file": "contents",
        },
        ("neo4j-secret",),
    )
    assert result == {
        "text": "Bearer [REDACTED] [REDACTED] password=[REDACTED] api_key=[REDACTED] token=[REDACTED] [REDACTED] ordinary"
    }
