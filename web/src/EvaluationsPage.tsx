import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Checkbox,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tag,
} from "antd";
import type { components } from "./api";
import { readErrorDetail } from "./httpErrors";
import type { Messages } from "./i18n";
import { ProjectSelector, storedProject } from "./ProjectSelector";
import { TableScroll } from "./TableScroll";

type EvalQuestion = components["schemas"]["EvalQuestionView"];

export function EvaluationsPage({
  token,
  messages: t,
  onNavigateDiscovery,
}: {
  token: string;
  messages: Messages;
  onNavigateDiscovery?: () => void;
}) {
  const [project, setProject] = useState(storedProject);
  const currentProject = useRef(project);
  const requestGeneration = useRef(0);
  if (currentProject.current !== project) {
    requestGeneration.current++;
  }
  currentProject.current = project;

  const [evalQuestions, setEvalQuestions] = useState<EvalQuestion[]>([]);
  const [evalSplitFilter, setEvalSplitFilter] = useState<
    "all" | "dev" | "holdout"
  >("all");
  const [evalConfirmedFilter, setEvalConfirmedFilter] = useState<
    "all" | "confirmed" | "unconfirmed"
  >("all");
  const [evalError, setEvalError] = useState("");
  const [evalModalOpen, setEvalModalOpen] = useState(false);
  const [editingEval, setEditingEval] = useState<EvalQuestion | null>(null);
  const [eqQuestion, setEqQuestion] = useState("");
  const [eqCaliber, setEqCaliber] = useState("");
  const [eqSql, setEqSql] = useState("");
  const [eqCategory, setEqCategory] = useState("单表聚合");
  const [eqDialect, setEqDialect] = useState<"hive" | "spark" | "doris">(
    "hive",
  );
  const [eqSplit, setEqSplit] = useState<"dev" | "holdout">("dev");
  const [eqConfirmed, setEqConfirmed] = useState(true);
  const [eqSaving, setEqSaving] = useState(false);

  const projectQuery = `project=${encodeURIComponent(project)}`;

  const loadEvalQuestions = useCallback(async () => {
    const generation = ++requestGeneration.current;
    try {
      const response = await fetch(`/api/v1/eval-questions?${projectQuery}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) {
        throw new Error(await readErrorDetail(response, t.evalLoadError));
      }
      const data = (await response.json()) as EvalQuestion[];
      if (
        currentProject.current !== project ||
        generation !== requestGeneration.current
      ) {
        return;
      }
      setEvalQuestions(Array.isArray(data) ? data : []);
      setEvalError("");
    } catch (failure) {
      if (
        currentProject.current === project &&
        generation === requestGeneration.current
      ) {
        setEvalError(
          failure instanceof Error ? failure.message : t.evalLoadError,
        );
      }
    }
  }, [project, projectQuery, token, t.evalLoadError]);

  useEffect(() => {
    void loadEvalQuestions();
  }, [loadEvalQuestions]);

  function openCreateEvalModal() {
    setEditingEval(null);
    setEqQuestion("");
    setEqCaliber("");
    setEqSql("");
    setEqCategory("单表聚合");
    setEqDialect("hive");
    setEqSplit("dev");
    setEqConfirmed(true);
    setEvalModalOpen(true);
  }

  function openEditEvalModal(row: EvalQuestion) {
    setEditingEval(row);
    setEqQuestion(row.question);
    setEqCaliber(row.caliber || "");
    setEqSql(row.reference_sql || "");
    setEqCategory(row.category || "单表聚合");
    setEqDialect(
      row.dialect === "spark" || row.dialect === "doris" ? row.dialect : "hive",
    );
    setEqSplit(row.split === "holdout" ? "holdout" : "dev");
    setEqConfirmed(row.answer_confirmed);
    setEvalModalOpen(true);
  }

  async function saveEvalQuestion() {
    setEqSaving(true);
    setEvalError("");
    try {
      const url = editingEval
        ? `/api/v1/eval-questions/${encodeURIComponent(editingEval.id)}?${projectQuery}`
        : `/api/v1/eval-questions?${projectQuery}`;
      const response = await fetch(url, {
        method: editingEval ? "PATCH" : "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          question: eqQuestion,
          caliber: eqCaliber,
          reference_sql: eqSql,
          category: eqCategory,
          dialect: eqDialect,
          split: eqSplit,
          answer_confirmed: eqConfirmed,
        }),
      });
      if (!response.ok) {
        throw new Error(await readErrorDetail(response, t.evalSaveError));
      }
      setEvalModalOpen(false);
      await loadEvalQuestions();
    } catch (failure) {
      setEvalError(
        failure instanceof Error ? failure.message : t.evalSaveError,
      );
    } finally {
      setEqSaving(false);
    }
  }

  async function confirmEvalQuestion(row: EvalQuestion) {
    setEvalError("");
    try {
      const response = await fetch(
        `/api/v1/eval-questions/${encodeURIComponent(row.id)}?${projectQuery}`,
        {
          method: "PATCH",
          headers: {
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ answer_confirmed: true }),
        },
      );
      if (!response.ok) {
        throw new Error(await readErrorDetail(response, t.evalSaveError));
      }
      await loadEvalQuestions();
    } catch (failure) {
      setEvalError(
        failure instanceof Error ? failure.message : t.evalSaveError,
      );
    }
  }

  async function deleteEvalQuestion(row: EvalQuestion) {
    setEvalError("");
    try {
      const response = await fetch(
        `/api/v1/eval-questions/${encodeURIComponent(row.id)}?${projectQuery}`,
        {
          method: "DELETE",
          headers: { Authorization: `Bearer ${token}` },
        },
      );
      if (!response.ok) {
        throw new Error(await readErrorDetail(response, t.evalSaveError));
      }
      await loadEvalQuestions();
    } catch (failure) {
      setEvalError(
        failure instanceof Error ? failure.message : t.evalSaveError,
      );
    }
  }

  const unconfirmedCount = evalQuestions.filter(
    (row) => !row.answer_confirmed,
  ).length;
  const confirmedCount = evalQuestions.length - unconfirmedCount;
  const devCount = evalQuestions.filter((row) => row.split === "dev").length;
  const holdoutCount = evalQuestions.filter(
    (row) => row.split === "holdout",
  ).length;

  const filteredEvalQuestions = evalQuestions.filter((row) => {
    if (evalSplitFilter !== "all" && row.split !== evalSplitFilter)
      return false;
    if (evalConfirmedFilter === "confirmed" && !row.answer_confirmed)
      return false;
    if (evalConfirmedFilter === "unconfirmed" && row.answer_confirmed)
      return false;
    return true;
  });

  return (
    <section className="stack admin-page">
      <div className="page-header">
        <div>
          <h1 className="page-title">{t.evalQuestionsTab}</h1>
          <p className="page-subtitle">{t.evalPageSubtitle}</p>
        </div>
        <Button
          autoInsertSpace={false}
          onClick={() => void loadEvalQuestions()}
        >
          {t.refresh}
        </Button>
        <ProjectSelector
          token={token}
          value={project}
          onChange={setProject}
          messages={t}
        />
      </div>

      <section className="card gov-metrics-card">
        <div className="gov-metrics">
          <div className="gov-metric">
            <span className="gov-metric-label">{t.evalMetricUnconfirmed}</span>
            <strong>{unconfirmedCount}</strong>
          </div>
          <div className="gov-metric">
            <span className="gov-metric-label">{t.evalMetricConfirmed}</span>
            <strong>{confirmedCount}</strong>
          </div>
          <div className="gov-metric">
            <span className="gov-metric-label">{t.evalMetricDev}</span>
            <strong>{devCount}</strong>
          </div>
          <div className="gov-metric">
            <span className="gov-metric-label">{t.evalMetricHoldout}</span>
            <strong>{holdoutCount}</strong>
          </div>
        </div>
      </section>

      <section className="card section-card">
        <div className="card-title-row">
          <h2 className="card-title">{t.evalQuestionsTab}</h2>
          <Space wrap>
            <Select
              aria-label="按划分筛选"
              value={evalSplitFilter}
              onChange={setEvalSplitFilter}
              style={{ minWidth: 130 }}
              options={[
                { value: "all", label: t.filterAll },
                { value: "dev", label: t.evalSplitDev },
                { value: "holdout", label: t.evalSplitHoldout },
              ]}
            />
            <Select
              aria-label="按确认状态筛选"
              value={evalConfirmedFilter}
              onChange={setEvalConfirmedFilter}
              style={{ minWidth: 130 }}
              options={[
                { value: "all", label: t.filterAll },
                { value: "confirmed", label: t.evalConfirmed },
                { value: "unconfirmed", label: t.evalUnconfirmed },
              ]}
            />
            <Button
              autoInsertSpace={false}
              type="primary"
              onClick={openCreateEvalModal}
            >
              {t.evalAddQuestion}
            </Button>
          </Space>
        </div>
        {evalError && (
          <Alert type="error" title={evalError} style={{ marginBottom: 12 }} />
        )}
        <TableScroll label={t.evalQuestionsTab}>
          <Table
            rowKey="id"
            pagination={false}
            dataSource={filteredEvalQuestions}
            locale={{ emptyText: t.emptyContext }}
            columns={[
              { title: t.evalQuestionLabel, dataIndex: "question" },
              { title: t.evalCategoryLabel, dataIndex: "category" },
              {
                title: t.evalSplitLabel,
                render: (_, row) => (
                  <Tag color={row.split === "holdout" ? "purple" : "blue"}>
                    {row.split}
                  </Tag>
                ),
              },
              {
                title: t.buildStatus,
                render: (_, row) => (
                  <Space size={4}>
                    <Tag color={row.answer_confirmed ? "green" : "orange"}>
                      {row.answer_confirmed
                        ? t.evalConfirmed
                        : t.evalUnconfirmed}
                    </Tag>
                    {row.redacted && <Tag>{t.evalRedacted}</Tag>}
                  </Space>
                ),
              },
              {
                title: t.evalSqlLabel,
                render: (_, row) =>
                  row.reference_sql ? (
                    <code className="mono">{row.reference_sql}</code>
                  ) : (
                    "—"
                  ),
              },
              {
                title: t.actions,
                render: (_, row) => (
                  <Space size={6}>
                    {!row.answer_confirmed && (
                      <Button
                        size="small"
                        type="primary"
                        autoInsertSpace={false}
                        onClick={() => void confirmEvalQuestion(row)}
                      >
                        {t.evalConfirm}
                      </Button>
                    )}
                    {!row.redacted && (
                      <Button
                        size="small"
                        autoInsertSpace={false}
                        onClick={() => openEditEvalModal(row)}
                      >
                        {t.evalEditQuestion}
                      </Button>
                    )}
                    <Button
                      size="small"
                      danger
                      autoInsertSpace={false}
                      onClick={() => void deleteEvalQuestion(row)}
                    >
                      {t.deleteToken}
                    </Button>
                  </Space>
                ),
              },
            ]}
          />
        </TableScroll>
        {onNavigateDiscovery && (
          <div className="gov-info-banner">
            <span>{t.evalPageSubtitle}</span>
            <Button type="link" size="small" onClick={onNavigateDiscovery}>
              {t.evalViewDiscoveryLink}
            </Button>
          </div>
        )}
        <Modal
          open={evalModalOpen}
          title={editingEval ? t.evalEditQuestion : t.evalAddQuestion}
          okText={t.evalSaveQuestion}
          okButtonProps={{ autoInsertSpace: false }}
          cancelText={t.cancel}
          confirmLoading={eqSaving}
          onOk={() => void saveEvalQuestion()}
          onCancel={() => setEvalModalOpen(false)}
        >
          <div className="stack">
            <div className="field">
              <label className="field-label" htmlFor="eval-q-input">
                {t.evalQuestionLabel}
              </label>
              <Input
                id="eval-q-input"
                aria-label={t.evalQuestionLabel}
                value={eqQuestion}
                onChange={(e) => setEqQuestion(e.target.value)}
              />
            </div>
            <div className="field">
              <label className="field-label" htmlFor="eval-caliber-input">
                {t.evalCaliberLabel}
              </label>
              <Input
                id="eval-caliber-input"
                aria-label={t.evalCaliberLabel}
                value={eqCaliber}
                onChange={(e) => setEqCaliber(e.target.value)}
              />
            </div>
            <div className="field">
              <label className="field-label" htmlFor="eval-sql-input">
                {t.evalSqlLabel}
              </label>
              <Input.TextArea
                id="eval-sql-input"
                aria-label={t.evalSqlLabel}
                rows={3}
                value={eqSql}
                onChange={(e) => setEqSql(e.target.value)}
              />
            </div>
            <Space wrap>
              <div className="field">
                <label className="field-label" htmlFor="eval-cat-input">
                  {t.evalCategoryLabel}
                </label>
                <Input
                  id="eval-cat-input"
                  aria-label={t.evalCategoryLabel}
                  value={eqCategory}
                  onChange={(e) => setEqCategory(e.target.value)}
                />
              </div>
              <div className="field">
                <span className="field-label">{t.evalDialectLabel}</span>
                <Select
                  aria-label={t.evalDialectLabel}
                  value={eqDialect}
                  onChange={setEqDialect}
                  style={{ minWidth: 120 }}
                  options={[
                    { value: "hive", label: "hive" },
                    { value: "spark", label: "spark" },
                    { value: "doris", label: "doris" },
                  ]}
                />
              </div>
              <div className="field">
                <span className="field-label">{t.evalSplitLabel}</span>
                <Select
                  aria-label={t.evalSplitLabel}
                  value={eqSplit}
                  onChange={setEqSplit}
                  style={{ minWidth: 130 }}
                  options={[
                    { value: "dev", label: t.evalSplitDev },
                    { value: "holdout", label: t.evalSplitHoldout },
                  ]}
                />
              </div>
            </Space>
            <Checkbox
              checked={eqConfirmed}
              onChange={(e) => setEqConfirmed(e.target.checked)}
            >
              {t.evalConfirmed}
            </Checkbox>
          </div>
        </Modal>
      </section>
    </section>
  );
}
