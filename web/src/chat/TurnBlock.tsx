import { Alert, Button, Space } from "antd";
import type { Messages } from "../i18n";
import { Markdown } from "../Markdown";
import {
  citationsFromRefs,
  citationsOf,
  normalizeSql,
  sqlBlocks,
  stripCitationRefs,
  stripMatchingSql,
} from "./citations";
import { CitationList } from "./CitationList";
import { ClarificationList } from "./ClarificationList";
import { QuestionText } from "./QuestionText";
import { ResultTable } from "./ResultTable";
import { SqlList } from "./SqlList";
import { ToolList } from "./ToolList";
import {
  clarificationsOfTurn,
  queryEvent,
  resultCountText,
  toolsOfTurn,
  turnFailed,
} from "./turnEvents";
import type { Clarification, QueryPlan, QueryResult, Turn } from "./types";

function hasLocalValidatedSql(turn: Turn, sqlCount: number): boolean {
  if (sqlCount === 0) return false;
  return (turn.events ?? []).some((event) => {
    if (event.type !== "tool_end" || !event.data) return false;
    const data = event.data as Record<string, unknown>;
    const name = String(data.name ?? "")
      .split("__")
      .pop();
    return (
      name === "validate_sql" &&
      !data.is_error &&
      String(data.summary ?? "").includes("status=pass")
    );
  });
}

export function TurnBlock({
  turn,
  messages,
  copied,
  onCopy,
  onOpen,
  onClarify,
  onExecute,
  onDownload,
  onCopyLink,
  executing,
  expiredPlans,
  onMarkAccurate,
  onOpenCorrection,
  markingAccurate = false,
  actionsDisabled = false,
}: {
  turn: Turn;
  messages: Messages;
  copied: string;
  onCopy: (sql: string) => void;
  onOpen: (ref: string) => void;
  onClarify: (
    question: Clarification,
    option: NonNullable<Clarification["options"]>[number],
  ) => void;
  onExecute: (turn: Turn, plan: QueryPlan) => void;
  onDownload: (id: string, format: string) => void;
  onCopyLink: (id: string) => void;
  executing: boolean;
  expiredPlans: string[];
  onMarkAccurate?: (turn: Turn) => void;
  onOpenCorrection?: (turn: Turn) => void;
  markingAccurate?: boolean;
  actionsDisabled?: boolean;
}) {
  const plan = queryEvent<QueryPlan>(turn.events ?? [], "query_plan");
  const result = queryEvent<QueryResult>(turn.events ?? [], "query_result");
  const awaitingPlan =
    plan !== undefined && turn.status === "awaiting_confirmation";
  const strippedAnswer = stripCitationRefs(turn.answer ?? "");
  const answer = awaitingPlan
    ? stripMatchingSql(strippedAnswer.text, plan?.sql)
    : strippedAnswer.text;
  const citations = [
    ...citationsOf(turn.citations),
    ...citationsFromRefs(strippedAnswer.refs),
  ];
  const tools = toolsOfTurn(turn.events ?? []);
  const clarifications = clarificationsOfTurn(turn.events ?? []);
  const planSql = plan ? normalizeSql(plan.sql) : "";
  const sql = sqlBlocks(turn.sql).filter(
    (block) => !awaitingPlan || normalizeSql(block.sql) !== planSql,
  );
  const planExpired = plan ? expiredPlans.includes(plan.id) : false;
  return (
    <article className="turn">
      <div className="message">
        <span className="avatar user" aria-hidden="true">
          我
        </span>
        <div className="message-body">
          <QuestionText text={turn.question} messages={messages} />
        </div>
      </div>
      <div className="message">
        <span className="avatar ai" aria-hidden="true">
          AI
        </span>
        <div className="message-body">
          <ToolList
            tools={tools}
            messages={messages}
            failed={turnFailed(turn)}
          />
          {awaitingPlan && plan && (
            <section className="card query-confirmation">
              <SqlList
                blocks={[{ sql: plan.sql }]}
                messages={messages}
                copied={copied}
                onCopy={onCopy}
              />
              <p className="query-confirmation-hint">
                {messages.confirmExecuteHint}
              </p>
              <Button
                type="primary"
                disabled={executing || planExpired}
                onClick={() => onExecute(turn, plan)}
              >
                {planExpired
                  ? messages.confirmExpired
                  : executing
                    ? messages.confirmExecuting
                    : messages.confirmExecute}
              </Button>
            </section>
          )}
          {result && (
            <div className="card query-result-preview">
              <ResultTable
                result={result}
                limit={20}
                label={messages.queryResults}
              />
              <p>{resultCountText(messages, result.row_count)}</p>
              {plan &&
                result.row_count >=
                  Math.min(
                    plan.row_limit,
                    plan.execution_limit ?? plan.row_limit,
                  ) && <p>结果可能被截断</p>}
              <Space wrap>
                {[
                  ["csv", "CSV"],
                  ["xlsx", "Excel"],
                  ["json", "JSON"],
                ].map(([fmt, label]) => (
                  <Button key={fmt} onClick={() => onDownload(result.id, fmt)}>
                    下载 {label}
                  </Button>
                ))}
                <Button onClick={() => onCopyLink(result.id)}>复制链接</Button>
              </Space>
              <p className="muted">
                {result.row_count > 20
                  ? messages.downloadLinkHint
                  : messages.downloadResultHint}
              </p>
            </div>
          )}
          <div className="answer">
            <Markdown text={answer} />
          </div>
          {turn.error && <Alert type="error" title={turn.error} />}
          <SqlList
            blocks={sql}
            messages={messages}
            copied={copied}
            onCopy={onCopy}
          />
          <CitationList
            citations={citations}
            messages={messages}
            onOpen={onOpen}
          />
          <ClarificationList
            groups={clarifications}
            messages={messages}
            disabled={false}
            onSelect={onClarify}
          />
          {turn.status !== "running" &&
            (onMarkAccurate || onOpenCorrection) && (
              <div className="turn-actions">
                {onMarkAccurate && (
                  <Button
                    size="small"
                    autoInsertSpace={false}
                    type={turn.feedback_accurate ? "primary" : "default"}
                    loading={markingAccurate}
                    disabled={
                      actionsDisabled ||
                      markingAccurate ||
                      Boolean(turn.feedback_accurate) ||
                      !(
                        turn.has_validated_sql ||
                        hasLocalValidatedSql(turn, sql.length)
                      )
                    }
                    title={
                      turn.has_validated_sql ||
                      hasLocalValidatedSql(turn, sql.length)
                        ? undefined
                        : messages.turnAccurateNeedsSql
                    }
                    onClick={() => onMarkAccurate(turn)}
                  >
                    {turn.feedback_accurate
                      ? messages.turnAccurateDone
                      : messages.turnAccurateBtn}
                  </Button>
                )}
                {onOpenCorrection && (
                  <Button
                    size="small"
                    autoInsertSpace={false}
                    disabled={actionsDisabled}
                    onClick={() => onOpenCorrection(turn)}
                  >
                    {messages.turnCorrectBtn}
                  </Button>
                )}
              </div>
            )}
        </div>
      </div>
    </article>
  );
}
