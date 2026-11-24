import { useEffect, useState } from "react";
import { Alert, Button, Drawer, Input, Radio, Select, Space, Tag } from "antd";
import type { components } from "../api";
import { DiffViewer } from "../DiffViewer";
import { readErrorDetail } from "../httpErrors";
import type { Messages } from "../i18n";

type DraftResponse = components["schemas"]["DraftCorrectionResponse"];
type Provider = components["schemas"]["ProviderView"];

type TargetMode = "auto" | "manual";

export function CorrectionDraftDrawer({
  open,
  token,
  project,
  messages: t,
  conversationId,
  turnId,
  target,
  targetLabel,
  sourceGenerated = false,
  defaultCategory = "caliber",
  initialDescription = "",
  knowledgeGapId,
  allowDirectEdit = false,
  providers: initialProviders,
  onClose,
  onSubmitted,
}: {
  open: boolean;
  token: string;
  project: string;
  messages: Messages;
  conversationId?: string;
  turnId?: number;
  target?: string;
  targetLabel?: string;
  sourceGenerated?: boolean;
  defaultCategory?: string;
  initialDescription?: string;
  knowledgeGapId?: string;
  allowDirectEdit?: boolean;
  providers?: Provider[];
  onClose: () => void;
  onSubmitted?: (proposalId?: string) => void;
}) {
  const [targetMode, setTargetMode] = useState<TargetMode>(
    target ? "manual" : "auto",
  );
  const [category, setCategory] = useState(defaultCategory);
  const [targetInput, setTargetInput] = useState(target || "");
  const [description, setDescription] = useState("");
  const [providers, setProviders] = useState<Provider[]>(
    initialProviders ?? [],
  );
  const [providerId, setProviderId] = useState("");
  const [model, setModel] = useState("");
  const [draft, setDraft] = useState<DraftResponse | null>(null);
  const [patch, setPatch] = useState("");
  const [reason, setReason] = useState("");
  const [drafting, setDrafting] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [llmUnavailableDetail, setLlmUnavailableDetail] = useState("");
  const [manualFallbackActive, setManualFallbackActive] = useState(false);

  useEffect(() => {
    if (!open) return;
    setTargetMode(target ? "manual" : "auto");
    setCategory(defaultCategory || "caliber");
    setTargetInput(target || "");
    setDescription(initialDescription);
    setProviderId("");
    setModel("");
    setDraft(null);
    setPatch("");
    setReason("");
    setError("");
    setLlmUnavailableDetail("");
    setManualFallbackActive(false);
  }, [open, project, target, turnId, defaultCategory, initialDescription]);

  useEffect(() => {
    if (initialProviders) {
      setProviders(initialProviders);
      return;
    }
    if (!open) return;
    const controller = new AbortController();
    fetch("/api/v1/providers", {
      headers: { Authorization: `Bearer ${token}` },
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) return;
        const data = (await response.json()) as unknown;
        if (!controller.signal.aborted && Array.isArray(data)) {
          setProviders(data as Provider[]);
        }
      })
      .catch(() => {
        // Provider list is optional for drawer fallback
      });
    return () => controller.abort();
  }, [open, token, initialProviders]);

  const selectedProvider = providers.find((item) => item.id === providerId);
  const availableModels = selectedProvider?.models ?? [];

  async function generateDraft() {
    if (!description.trim()) return;
    setDrafting(true);
    setError("");
    setLlmUnavailableDetail("");
    try {
      const response = await fetch("/api/v1/corrections/draft", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          project,
          conversation_id: conversationId || undefined,
          turn_id: turnId,
          target:
            targetMode === "manual" && targetInput.trim()
              ? targetInput.trim()
              : undefined,
          category,
          description: description.trim(),
          provider_id: providerId || undefined,
          model: model || undefined,
        }),
      });
      if (response.status === 503) {
        let detail = "";
        let isLlmUnavailable = true;
        try {
          const payload = (await response.json()) as {
            detail?: string;
            code?: string;
            fallback?: string;
          };
          if (typeof payload.detail === "string" && payload.detail.trim()) {
            detail = payload.detail;
          }
          if (
            payload.code &&
            payload.code !== "llm_unavailable" &&
            payload.fallback !== "manual_edit"
          ) {
            isLlmUnavailable = false;
          }
        } catch {
          // Use fallback message when body is not JSON
        }
        if (isLlmUnavailable) {
          setDraft(null);
          setLlmUnavailableDetail(detail || t.draftLlmUnavailableTitle);
          return;
        }
        throw new Error(detail || t.draftError);
      }
      if (!response.ok) {
        throw new Error(await readErrorDetail(response, t.draftError));
      }
      const next = (await response.json()) as DraftResponse;
      setDraft(next);
      setPatch(next.patch);
      setReason(next.reason);
      setManualFallbackActive(false);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : t.draftError);
    } finally {
      setDrafting(false);
    }
  }

  function enterManualEdit() {
    setDraft(null);
    setTargetMode("manual");
    setManualFallbackActive(true);
    setError("");
  }

  async function submitCorrection() {
    setSubmitting(true);
    setError("");
    try {
      const resolvedTarget = draft?.target || targetInput.trim();
      const resolvedKind =
        draft?.target_kind ||
        (resolvedTarget.startsWith("document:") ? "document" : "override");
      const response = await fetch(
        `/api/v1/corrections?project=${encodeURIComponent(project)}`,
        {
          method: "POST",
          headers: {
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            target: resolvedTarget,
            target_kind: resolvedKind,
            patch: resolvedKind === "document" || !draft ? patch : draft.patch,
            structured_patch: draft?.structured_patch ?? undefined,
            reason: reason.trim(),
            conversation_id: conversationId || undefined,
            turn_id: turnId,
            knowledge_gap_id: knowledgeGapId,
            caliber: draft?.caliber
              ? knowledgeGapId
                ? Array.from(draft.caliber).slice(0, 5000).join("")
                : draft.caliber
              : undefined,
            reference_sql: draft?.reference_sql || undefined,
          }),
        },
      );
      if (!response.ok) {
        throw new Error(await readErrorDetail(response, t.error));
      }
      const saved = (await response.json()) as { id?: string };
      onSubmitted?.(saved.id);
      onClose();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : t.error);
    } finally {
      setSubmitting(false);
    }
  }

  const isStructuredOnly = Boolean(
    sourceGenerated ||
    (targetMode === "manual" && targetInput.trim().startsWith("relationship:")),
  );
  const directEditAllowed = allowDirectEdit || manualFallbackActive;
  const showDirectFields =
    (directEditAllowed && !isStructuredOnly) || draft !== null;
  const gatePassed = draft ? Boolean(draft.gate_report?.passed) : true;
  const canSubmit = draft
    ? gatePassed && Boolean(reason.trim())
    : Boolean(
        directEditAllowed &&
        !isStructuredOnly &&
        targetMode === "manual" &&
        targetInput.trim() &&
        patch.trim() &&
        reason.trim(),
      );

  return (
    <Drawer
      open={open}
      onClose={onClose}
      title={
        targetLabel
          ? `${t.draftDrawerTitle} · ${targetLabel}`
          : t.draftDrawerTitle
      }
      size="large"
    >
      <div className="stack">
        {error && <Alert type="error" title={error} />}
        {llmUnavailableDetail && (
          <Alert
            type="warning"
            showIcon
            title={t.draftLlmUnavailableTitle}
            description={llmUnavailableDetail}
            action={
              <Button
                size="small"
                autoInsertSpace={false}
                onClick={enterManualEdit}
              >
                {t.draftSwitchManualEdit}
              </Button>
            }
          />
        )}
        {sourceGenerated && (
          <Alert type="info" showIcon title={t.draftGeneratedHint} />
        )}
        <div className="field">
          <span className="field-label">{t.draftCategoryLabel}</span>
          <Select
            aria-label={t.draftCategoryLabel}
            value={category}
            onChange={setCategory}
            options={[
              { value: "caliber", label: t.draftCategoryCaliber },
              { value: "metadata", label: t.draftCategoryMetadata },
              { value: "relationship", label: t.draftCategoryRelationship },
              { value: "metric", label: t.draftCategoryMetric },
            ]}
          />
        </div>
        <div className="field">
          <span className="field-label">{t.draftTargetModeLabel}</span>
          <Radio.Group
            value={targetMode}
            onChange={(event) =>
              setTargetMode(event.target.value as TargetMode)
            }
            optionType="button"
            options={[
              { value: "auto", label: t.draftTargetAuto },
              { value: "manual", label: t.draftTargetManual },
            ]}
          />
          {targetMode === "auto" && (
            <p className="field-hint">{t.draftTargetAutoHint}</p>
          )}
        </div>
        {targetMode === "manual" && (
          <div className="field">
            <label className="field-label" htmlFor="draft-target-input">
              {t.draftTargetLabel}
            </label>
            <Input
              id="draft-target-input"
              aria-label={t.draftTargetLabel}
              value={targetInput}
              onChange={(e) => setTargetInput(e.target.value)}
              placeholder="table:db.tbl / field:db.tbl.col / document:category/doc.md"
            />
          </div>
        )}
        {providers.length > 0 && (
          <Space wrap style={{ width: "100%" }}>
            <div className="field" style={{ minWidth: 200 }}>
              <label className="field-label" htmlFor="draft-provider-select">
                {t.draftProviderLabel}
              </label>
              <Select
                id="draft-provider-select"
                aria-label={t.draftProviderLabel}
                value={providerId}
                onChange={(next) => {
                  setProviderId(next || "");
                  setModel("");
                }}
                options={[
                  { value: "", label: t.draftModelDefault },
                  ...providers.map((item) => ({
                    value: item.id,
                    label: item.name,
                  })),
                ]}
              />
            </div>
            <div className="field" style={{ minWidth: 200 }}>
              <label className="field-label" htmlFor="draft-model-select">
                {t.draftModelLabel}
              </label>
              <Select
                id="draft-model-select"
                aria-label={t.draftModelLabel}
                value={model}
                onChange={(next) => setModel(next || "")}
                options={[
                  { value: "", label: t.draftModelDefault },
                  ...availableModels.map((item) => ({
                    value: item,
                    label: item,
                  })),
                ]}
              />
            </div>
          </Space>
        )}
        <div className="field">
          <label className="field-label" htmlFor="draft-description-input">
            {t.draftDescriptionLabel}
          </label>
          <Input.TextArea
            id="draft-description-input"
            aria-label={t.draftDescriptionLabel}
            rows={3}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </div>
        <Space wrap>
          <Button
            type="default"
            autoInsertSpace={false}
            disabled={!description.trim() || drafting}
            onClick={() => void generateDraft()}
          >
            {t.draftGenerateBtn}
          </Button>
          {!manualFallbackActive && !isStructuredOnly && (
            <Button
              type="link"
              autoInsertSpace={false}
              onClick={enterManualEdit}
            >
              {t.draftSwitchManualEdit}
            </Button>
          )}
        </Space>

        {draft && (
          <div className="stack">
            <Space wrap>
              <Tag>{draft.target}</Tag>
              <Tag color="blue">{draft.target_kind}</Tag>
              {draft.is_new_file && (
                <Tag color="purple">{t.draftNewFileBadge}</Tag>
              )}
              {draft.used_llm && <Tag color="green">{t.draftLlmUsedBadge}</Tag>}
              {(draft.source_generated || sourceGenerated) && (
                <Tag color="cyan">{t.draftGeneratedRouted}</Tag>
              )}
              <span className="mono muted">
                {t.draftPreviewPath}: {draft.resolved_path}
              </span>
            </Space>
            {draft.is_duplicate && (
              <Alert
                type="warning"
                showIcon
                title={t.draftDuplicateWarning}
                description={
                  draft.duplicate_reason || draft.duplicate_of || undefined
                }
              />
            )}
            <Alert
              type={gatePassed ? "success" : "error"}
              showIcon
              title={gatePassed ? t.gatePassed : t.gateFailed}
              description={
                Array.isArray(draft.gate_report?.errors) &&
                draft.gate_report.errors.length > 0 ? (
                  <ul className="stale-overrides-list">
                    {(draft.gate_report.errors as string[]).map((err, idx) => (
                      <li key={idx}>{err}</li>
                    ))}
                  </ul>
                ) : undefined
              }
            />
            {draft.diff && (
              <DiffViewer
                diff={draft.diff}
                messages={t}
                title={draft.resolved_path}
                defaultMode="unified"
              />
            )}
          </div>
        )}

        {showDirectFields && (
          <div className="stack">
            {manualFallbackActive && !draft && (
              <Alert type="info" showIcon title={t.draftManualFallbackHint} />
            )}
            {(draft?.target_kind === "document" || !draft) && (
              <div className="field">
                <label className="field-label" htmlFor="correction-patch">
                  {t.correctionReplacement}
                </label>
                <Input.TextArea
                  id="correction-patch"
                  aria-label={t.correctionReplacement}
                  rows={5}
                  value={patch}
                  onChange={(e) => setPatch(e.target.value)}
                />
                <p className="field-hint">{t.correctionPatchHint}</p>
              </div>
            )}
            <div className="field">
              <label className="field-label" htmlFor="correction-reason">
                {t.correctionReason}
              </label>
              <Input
                id="correction-reason"
                aria-label={t.correctionReason}
                value={reason}
                onChange={(e) => setReason(e.target.value)}
              />
              <p className="field-hint">{t.correctionReasonHint}</p>
            </div>
          </div>
        )}

        <Space wrap>
          <Button
            type="primary"
            autoInsertSpace={false}
            disabled={!canSubmit || submitting}
            onClick={() => void submitCorrection()}
          >
            {t.proposeCorrection}
          </Button>
          <Button autoInsertSpace={false} onClick={onClose}>
            {t.cancel}
          </Button>
        </Space>
      </div>
    </Drawer>
  );
}
