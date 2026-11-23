import { useEffect, useState } from "react";
import { Alert, Button, Drawer, Input, Select, Space, Tag } from "antd";
import type { components } from "../api";
import { DiffViewer } from "../DiffViewer";
import { readErrorDetail } from "../httpErrors";
import type { Messages } from "../i18n";

type DraftResponse = components["schemas"]["DraftCorrectionResponse"];

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
  allowDirectEdit = false,
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
  allowDirectEdit?: boolean;
  onClose: () => void;
  onSubmitted?: (proposalId?: string) => void;
}) {
  const [category, setCategory] = useState(defaultCategory);
  const [targetInput, setTargetInput] = useState(target || "");
  const [description, setDescription] = useState("");
  const [draft, setDraft] = useState<DraftResponse | null>(null);
  const [patch, setPatch] = useState("");
  const [reason, setReason] = useState("");
  const [drafting, setDrafting] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) return;
    setCategory(defaultCategory || "caliber");
    setTargetInput(target || "");
    setDescription("");
    setDraft(null);
    setPatch("");
    setReason("");
    setError("");
  }, [open, project, target, turnId, defaultCategory]);

  async function generateDraft() {
    if (!description.trim()) return;
    setDrafting(true);
    setError("");
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
          target: targetInput.trim() || undefined,
          category,
          description: description.trim(),
        }),
      });
      if (!response.ok) {
        throw new Error(await readErrorDetail(response, t.draftError));
      }
      const next = (await response.json()) as DraftResponse;
      setDraft(next);
      setPatch(next.patch);
      setReason(next.reason);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : t.draftError);
    } finally {
      setDrafting(false);
    }
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
            caliber: draft?.caliber || undefined,
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
    sourceGenerated || targetInput.trim().startsWith("relationship:"),
  );
  const showDirectFields =
    (allowDirectEdit && !isStructuredOnly) || draft !== null;
  const gatePassed = draft ? Boolean(draft.gate_report?.passed) : true;
  const canSubmit = draft
    ? gatePassed && Boolean(reason.trim())
    : Boolean(
        allowDirectEdit &&
        !isStructuredOnly &&
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
          <label className="field-label" htmlFor="draft-target-input">
            {t.draftTargetLabel}
          </label>
          <Input
            id="draft-target-input"
            aria-label={t.draftTargetLabel}
            value={targetInput}
            onChange={(e) => setTargetInput(e.target.value)}
            placeholder="table:db.tbl / field:db.tbl.col / document:ref"
          />
        </div>
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
        </Space>

        {draft && (
          <div className="stack">
            <Space wrap>
              <Tag>{draft.target}</Tag>
              <Tag color="blue">{draft.target_kind}</Tag>
              {(draft.source_generated || sourceGenerated) && (
                <Tag color="cyan">{t.draftGeneratedRouted}</Tag>
              )}
              <span className="mono muted">
                {t.draftPreviewPath}: {draft.resolved_path}
              </span>
            </Space>
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
