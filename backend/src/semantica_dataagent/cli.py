import argparse
import asyncio
import os
import secrets
import tempfile
from pathlib import Path

from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet

from .db import Token, digest, sessions


def initialize() -> str:
    Fernet(os.environ["DATAAGENT_ENCRYPTION_KEY"].encode())
    config = Config()
    config.set_main_option(
        "script_location", os.path.join(os.path.dirname(__file__), "migrations")
    )
    config.set_main_option(
        "sqlalchemy.url",
        "sqlite:///" + os.environ["DATAAGENT_DB_PATH"].replace("%", "%%"),
    )
    command.upgrade(config, "head")
    with sessions().begin() as db:
        if db.query(Token).first():
            raise RuntimeError("Database already has a token")
        token = secrets.token_urlsafe(32)
        db.add(Token(digest=digest(token), name="initial admin", role="admin"))
    return token


def _serve() -> None:
    import uvicorn

    host, port = os.environ.get("DATAAGENT_LISTEN", "127.0.0.1:8000").rsplit(":", 1)
    uvicorn.run("semantica_dataagent.app:app", host=host, port=int(port))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="semantica-dataagent")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create the database and initial admin token")
    commands.add_parser("serve", help="serve the REST and MCP application")
    evaluate = commands.add_parser(
        "eval", help="evaluate a YAML question set in retrieval or agent mode"
    )
    evaluate.add_argument("--questions", required=True, type=Path)
    evaluate.add_argument(
        "--report",
        required=True,
        type=Path,
        help="report prefix; writes <prefix>.md and <prefix>.json",
    )
    evaluate.add_argument(
        "--agent",
        choices=("pi",),
        help="run the online agent instead of the retrieval-only evaluation",
    )
    evaluate.add_argument("--model", help="provider model id; required with --agent")
    evaluate.add_argument(
        "--top-k", type=int, default=5, help="number of retrieval hits to score"
    )
    evaluate.add_argument(
        "--provider-id",
        help="provider id from the application database (agent mode)",
    )
    evaluate.add_argument(
        "--provider-url",
        help="provider base URL (agent mode; default env DATAAGENT_EVAL_PROVIDER_URL "
        "or TEST_PI_BASE_URL)",
    )
    evaluate.add_argument(
        "--provider-type",
        help="provider API type (agent mode; default anthropic-messages)",
    )
    evaluate.add_argument("--mcp-url", help="MCP streamable HTTP URL")
    evaluate.add_argument(
        "--workspace",
        type=Path,
        help="agent workspace directory (agent mode); a temporary directory is used "
        "when omitted",
    )
    evaluate.add_argument(
        "--agent-timeout",
        type=float,
        default=600.0,
        help="per-question agent timeout in seconds",
    )
    return parser


def _provider(args, model: str):
    from .eval import default_mcp_url, provider_from_environment
    from .provider_api import provider_config

    if args.provider_id:
        from .db import Provider, sessions

        with sessions()() as db:
            row = db.get(Provider, args.provider_id)
            if row is None:
                raise SystemExit(f"provider not found: {args.provider_id}")
            provider = provider_config(row)
    else:
        provider = provider_from_environment(model)
    if args.provider_url:
        provider.base_url = args.provider_url.rstrip("/")
    if args.provider_type:
        provider.type = args.provider_type
    return provider, default_mcp_url()


def _run_eval(args: argparse.Namespace) -> int:
    from .eval import (
        cleanup_workspace,
        collect_agent_answers,
        load_questions,
        remove_token,
        run_evaluation,
        temporary_token,
        write_reports,
    )

    questions = load_questions(args.questions)
    if args.top_k < 1:
        raise SystemExit("--top-k must be positive")
    if args.agent and not args.model:
        raise SystemExit("--model is required with --agent")

    if not args.agent:
        from .sql_validation import validate_sql
        from .table_api import search_knowledge

        report = run_evaluation(
            questions,
            retrieve=lambda text: search_knowledge(text, "tables", args.top_k, None),
            validate=validate_sql,
            top_k=args.top_k,
            mode="retrieval",
            source=args.questions,
        )
    else:
        provider, mcp_url = _provider(args, args.model)
        if args.mcp_url:
            mcp_url = args.mcp_url
        token = os.environ.get("DATAAGENT_EVAL_MCP_TOKEN")
        token_id = None
        if not token:
            if not os.environ.get("DATAAGENT_DB_PATH"):
                raise SystemExit(
                    "DATAAGENT_DB_PATH is required to create a temporary MCP token"
                )
            token, token_id = temporary_token()
        created_workspace = args.workspace is None
        workspace = args.workspace or Path(
            tempfile.mkdtemp(prefix="dataagent-eval-", dir=os.environ.get("TMPDIR"))
        )
        try:
            answers, texts, errors, clarifications = asyncio.run(
                collect_agent_answers(
                    questions,
                    agent=args.agent,
                    model=args.model,
                    provider=provider,
                    mcp_url=mcp_url,
                    mcp_token=token,
                    workspace=workspace,
                    timeout=args.agent_timeout,
                )
            )
            report = run_evaluation(
                questions,
                retrieve=lambda text: search_knowledge(
                    text, "tables", args.top_k, None
                ),
                validate=validate_sql,
                top_k=args.top_k,
                mode="agent",
                answers=lambda question: answers.get(question.id, []),
                answer_texts=lambda question: (
                    texts.get(question.id, "")
                    + (" 需要澄清" if clarifications.get(question.id) else "")
                ),
                answer_errors=lambda question: errors.get(question.id),
                source=args.questions,
            )
        finally:
            if token_id is not None:
                remove_token(token_id)
            if created_workspace:
                cleanup_workspace(workspace)

    markdown, payload = write_reports(report, args.report)
    metrics = report["metrics"]
    print(
        "eval "
        f"mode={report['mode']} questions={metrics['questions']} "
        f"table_recall@5={metrics['table_recall_at_5']:.3f} "
        f"validate_sql_pass_rate={metrics['validate_sql_pass_rate']:.3f}"
    )
    print(f"report markdown: {markdown}")
    print(f"report json: {payload}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "init":
        print(initialize())
        return 0
    if args.command == "serve":
        _serve()
        return 0
    return _run_eval(args)
