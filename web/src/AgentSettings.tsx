import { useCallback, useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";
import { Alert, Button, Checkbox, Input, Tag } from "antd";
import {
  CheckOutlined,
  CopyOutlined,
  ExclamationCircleOutlined,
  WarningOutlined,
} from "@ant-design/icons";
import { modelProtocol } from "./agentProfiles";
import type { Messages } from "./i18n";
import { notifyAssistantCatalogChanged } from "./chat/useProviders";
import type {
  AgentProfileCreate,
  AgentProfileUpdate,
  AgentProfileView,
  AgentRunnerView,
  Provider,
} from "./chat/types";

type AffectedDefault = {
  kind: string;
  profile_id: string;
  profile_name: string;
  provider_id: string | null;
  model: string | null;
};

type ConflictState =
  | { kind: "version"; currentVersion?: number; message: string }
  | { kind: "affected_defaults"; items: AffectedDefault[]; message: string };

type CatalogRow = {
  providerId: string;
  providerName: string;
  modelId: string;
  checked: boolean;
  inCatalog: boolean;
  missing: boolean;
  pending: boolean;
};

function protocolLabel(modelId: string, t: Messages): string {
  const proto = modelProtocol(modelId);
  return proto === "unknown" ? t.agentProfileProtocolUnknown : proto;
}

function parseConflictDetail(
  detail: unknown,
  fallback: string,
): { message: string; conflict?: ConflictState } {
  if (typeof detail === "string" && detail.trim()) {
    return { message: detail };
  }
  if (detail && typeof detail === "object") {
    const obj = detail as {
      message?: unknown;
      current_version?: unknown;
      affected_defaults?: unknown;
    };
    const msg =
      typeof obj.message === "string" && obj.message.trim()
        ? obj.message
        : fallback;
    if (typeof obj.current_version === "number") {
      return {
        message: msg,
        conflict: {
          kind: "version",
          currentVersion: obj.current_version,
          message: msg,
        },
      };
    }
    if (Array.isArray(obj.affected_defaults)) {
      const items = obj.affected_defaults.filter(
        (item): item is AffectedDefault =>
          Boolean(item && typeof item === "object"),
      );
      return {
        message: msg,
        conflict: {
          kind: "affected_defaults",
          items,
          message: msg,
        },
      };
    }
    return { message: msg };
  }
  return { message: fallback };
}

export function AgentProfilesPage({
  token,
  messages: t,
}: {
  token: string;
  messages: Messages;
}) {
  const [profiles, setProfiles] = useState<AgentProfileView[]>([]);
  const [runners, setRunners] = useState<AgentRunnerView[]>([]);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [notice, setNotice] = useState("");
  const [errorMsg, setErrorMsg] = useState("");
  const [conflict, setConflict] = useState<ConflictState | null>(null);
  const [deletingId, setDeletingId] = useState("");
  const [confirmDeleteId, setConfirmDeleteId] = useState("");

  // Editor state: null = list view, "create" = new profile, profile.id = editing
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [runnerId, setRunnerId] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [isSystemDefault, setIsSystemDefault] = useState(false);
  const [providerModels, setProviderModels] = useState<
    Record<string, string[]>
  >({});
  const [defaultProviderId, setDefaultProviderId] = useState("");
  const [defaultModel, setDefaultModel] = useState("");
  const [version, setVersion] = useState(1);
  const [saving, setSaving] = useState(false);

  // Model selector state
  const [providerFilter, setProviderFilter] = useState("__all__");
  const [pickedOnly, setPickedOnly] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [copiedModelKey, setCopiedModelKey] = useState("");

  const loadAll = useCallback(async () => {
    setLoading(true);
    setLoadError("");
    const headers = { Authorization: `Bearer ${token}` };
    try {
      const [profilesRes, runnersRes, providersRes] = await Promise.all([
        fetch("/api/v1/agent-profiles", { headers }),
        fetch("/api/v1/agent-runners", { headers }),
        fetch("/api/v1/providers", { headers }),
      ]);
      if (!profilesRes.ok) throw new Error();
      const loadedProfiles = (await profilesRes.json()) as AgentProfileView[];
      const loadedRunners = runnersRes.ok
        ? ((await runnersRes.json()) as AgentRunnerView[])
        : [];
      const loadedProviders = providersRes.ok
        ? ((await providersRes.json()) as Provider[])
        : [];
      setProfiles(Array.isArray(loadedProfiles) ? loadedProfiles : []);
      setRunners(Array.isArray(loadedRunners) ? loadedRunners : []);
      setProviders(Array.isArray(loadedProviders) ? loadedProviders : []);
    } catch {
      setLoadError(t.agentProfileLoadError);
    } finally {
      setLoading(false);
    }
  }, [token, t.agentProfileLoadError]);

  useEffect(() => {
    void loadAll();
  }, [loadAll]);

  const providersById = useMemo(
    () => new Map(providers.map((p) => [p.id, p])),
    [providers],
  );

  const runnersById = useMemo(
    () => new Map(runners.map((r) => [r.id, r])),
    [runners],
  );

  const editingProfile = useMemo(
    () =>
      editingKey && editingKey !== "create"
        ? (profiles.find((p) => p.id === editingKey) ?? null)
        : null,
    [editingKey, profiles],
  );

  function openCreate() {
    const firstRunner =
      runners.find((r) => r.available)?.id ?? runners[0]?.id ?? "pi";
    setEditingKey("create");
    setName("");
    setRunnerId(firstRunner);
    setEnabled(true);
    setIsSystemDefault(false);
    setProviderModels({});
    setDefaultProviderId("");
    setDefaultModel("");
    setVersion(1);
    setProviderFilter(providers.length === 1 ? providers[0].id : "__all__");
    setPickedOnly(false);
    setSearchQuery("");
    setErrorMsg("");
    setConflict(null);
    setNotice("");
  }

  function openEdit(profile: AgentProfileView) {
    setEditingKey(profile.id);
    setName(profile.name);
    setRunnerId(profile.runner_id);
    setEnabled(profile.enabled);
    setIsSystemDefault(profile.is_system_default);
    const clonedPm: Record<string, string[]> = {};
    for (const [pid, models] of Object.entries(profile.provider_models)) {
      clonedPm[pid] = [...models];
    }
    setProviderModels(clonedPm);
    setDefaultProviderId(profile.default_provider_id ?? "");
    setDefaultModel(profile.default_model ?? "");
    setVersion(profile.version);
    setProviderFilter(providers.length === 1 ? providers[0].id : "__all__");
    setPickedOnly(false);
    setSearchQuery("");
    setErrorMsg("");
    setConflict(null);
    setNotice("");
  }

  function closeEditor() {
    setEditingKey(null);
    setErrorMsg("");
    setConflict(null);
  }

  // Build catalog rows across selected/all providers
  const allCatalogRows: CatalogRow[] = useMemo(() => {
    const rows: CatalogRow[] = [];
    const seenProviders = new Set<string>();

    const appendProviderRows = (provider: Provider) => {
      seenProviders.add(provider.id);
      const selected = providerModels[provider.id] ?? [];
      const selectedSet = new Set(selected);
      const catalog = provider.catalog_models ?? [];
      const hasFetchedCatalog = provider.catalog_fetched_at != null;
      const catalogSet = new Set(catalog);
      const profileMissingSet = new Set(
        editingProfile?.missing_models?.[provider.id] ?? [],
      );
      const profilePendingSet = new Set(
        editingProfile?.pending_verification?.[provider.id] ?? [],
      );

      // Show catalog models first, then any selected models missing/pending from catalog
      const orderedIds: string[] = [...catalog];
      for (const m of selected) {
        if (!catalogSet.has(m)) orderedIds.push(m);
      }

      for (const modelId of orderedIds) {
        const checked = selectedSet.has(modelId);
        const inCatalog = catalogSet.has(modelId);
        const missing =
          profileMissingSet.has(modelId) ||
          (checked && hasFetchedCatalog && !inCatalog);
        const pending =
          profilePendingSet.has(modelId) ||
          (checked && !hasFetchedCatalog && !inCatalog);
        rows.push({
          providerId: provider.id,
          providerName: provider.name,
          modelId,
          checked,
          inCatalog,
          missing,
          pending,
        });
      }
    };

    for (const provider of providers) {
      appendProviderRows(provider);
    }
    // Include orphaned provider selections (provider deleted after selection)
    for (const [pid, selected] of Object.entries(providerModels)) {
      if (seenProviders.has(pid)) continue;
      for (const modelId of selected) {
        rows.push({
          providerId: pid,
          providerName: pid,
          modelId,
          checked: true,
          inCatalog: false,
          missing: true,
          pending: false,
        });
      }
    }
    return rows;
  }, [providers, providerModels, editingProfile]);

  const filteredRows = useMemo(() => {
    const query = searchQuery.trim().toLowerCase();
    return allCatalogRows.filter((row) => {
      if (providerFilter !== "__all__" && row.providerId !== providerFilter) {
        return false;
      }
      if (pickedOnly && !row.checked) return false;
      if (query && !row.modelId.toLowerCase().includes(query)) return false;
      return true;
    });
  }, [allCatalogRows, providerFilter, pickedOnly, searchQuery]);

  const totalSelectedCount = useMemo(
    () =>
      Object.values(providerModels).reduce((sum, list) => sum + list.length, 0),
    [providerModels],
  );

  // Valid checked models per provider for the default dropdowns
  const validCheckedByProvider = useMemo(() => {
    const map = new Map<string, string[]>();
    for (const row of allCatalogRows) {
      if (!row.checked || row.missing) continue;
      // When enabled, backend requires default_model to be in fetched catalog_models
      if (enabled && !row.inCatalog) continue;
      const list = map.get(row.providerId) ?? [];
      list.push(row.modelId);
      map.set(row.providerId, list);
    }
    return map;
  }, [allCatalogRows, enabled]);

  const defaultProviderOptions = useMemo(() => {
    const opts: { id: string; name: string }[] = [];
    for (const pid of validCheckedByProvider.keys()) {
      opts.push({ id: pid, name: providersById.get(pid)?.name ?? pid });
    }
    return opts;
  }, [validCheckedByProvider, providersById]);

  const defaultModelOptions = useMemo(
    () => validCheckedByProvider.get(defaultProviderId) ?? [],
    [validCheckedByProvider, defaultProviderId],
  );

  // Keep defaultProviderId / defaultModel coherent when selection changes
  function syncDefaultsAfterSelection(nextPm: Record<string, string[]>) {
    const nextRows = new Map<string, string[]>();
    for (const [pid, list] of Object.entries(nextPm)) {
      const provider = providersById.get(pid);
      const catalogSet = new Set(provider?.catalog_models ?? []);
      const valid = list.filter((m) => (enabled ? catalogSet.has(m) : true));
      if (valid.length > 0) nextRows.set(pid, valid);
    }
    if (defaultProviderId && nextRows.get(defaultProviderId)?.length) {
      const models = nextRows.get(defaultProviderId) ?? [];
      if (!models.includes(defaultModel)) {
        setDefaultModel("");
      }
      return;
    }
    const firstPid = nextRows.keys().next().value ?? "";
    setDefaultProviderId(firstPid);
    setDefaultModel("");
  }

  function toggleModel(row: CatalogRow, checked: boolean) {
    if (checked && !row.inCatalog) return;
    setProviderModels((prev) => {
      const current = prev[row.providerId] ?? [];
      const nextList = checked
        ? current.includes(row.modelId)
          ? current
          : [...current, row.modelId]
        : current.filter((m) => m !== row.modelId);
      const next = { ...prev };
      if (nextList.length > 0) next[row.providerId] = nextList;
      else delete next[row.providerId];
      syncDefaultsAfterSelection(next);
      return next;
    });
  }

  function selectFiltered() {
    setProviderModels((prev) => {
      const next = { ...prev };
      for (const row of filteredRows) {
        if (!row.inCatalog) continue;
        const current = next[row.providerId] ?? [];
        if (!current.includes(row.modelId)) {
          next[row.providerId] = [...current, row.modelId];
        }
      }
      syncDefaultsAfterSelection(next);
      return next;
    });
  }

  function clearFiltered() {
    setProviderModels((prev) => {
      const next = { ...prev };
      const toRemove = new Map<string, Set<string>>();
      for (const row of filteredRows) {
        const set = toRemove.get(row.providerId) ?? new Set<string>();
        set.add(row.modelId);
        toRemove.set(row.providerId, set);
      }
      for (const [pid, removeSet] of toRemove.entries()) {
        const remaining = (next[pid] ?? []).filter((m) => !removeSet.has(m));
        if (remaining.length > 0) next[pid] = remaining;
        else delete next[pid];
      }
      syncDefaultsAfterSelection(next);
      return next;
    });
  }

  const allFilteredSelectableChecked = useMemo(() => {
    const selectable = filteredRows.filter((r) => r.inCatalog);
    return selectable.length === 0 || selectable.every((r) => r.checked);
  }, [filteredRows]);

  const noFilteredChecked = useMemo(
    () => filteredRows.every((r) => !r.checked),
    [filteredRows],
  );

  async function copyModelId(row: CatalogRow) {
    const key = `${row.providerId}:${row.modelId}`;
    try {
      await navigator.clipboard?.writeText(row.modelId);
      setCopiedModelKey(key);
      window.setTimeout(() => {
        setCopiedModelKey((curr) => (curr === key ? "" : curr));
      }, 1500);
    } catch {
      /* Clipboard unavailable */
    }
  }

  async function refreshEditingProfile() {
    if (!editingProfile) return;
    setErrorMsg("");
    setConflict(null);
    await loadAll();
    const res = await fetch(
      `/api/v1/agent-profiles/${encodeURIComponent(editingProfile.id)}`,
      { headers: { Authorization: `Bearer ${token}` } },
    );
    if (res.ok) {
      const fresh = (await res.json()) as AgentProfileView;
      openEdit(fresh);
    }
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setErrorMsg("");
    setConflict(null);
    setNotice("");

    const trimmedName = name.trim();
    if (!trimmedName) {
      setErrorMsg(t.agentProfileNameRequired);
      return;
    }

    const effectiveDefaultProvider = defaultProviderId.trim() || null;
    const effectiveDefaultModel = defaultModel.trim() || null;
    const activeDefaultRemoved = Boolean(
      editingProfile?.default_provider_id &&
      editingProfile?.default_model &&
      !(providerModels[editingProfile.default_provider_id] ?? []).includes(
        editingProfile.default_model,
      ),
    );

    if (
      enabled &&
      (!effectiveDefaultProvider || !effectiveDefaultModel) &&
      !activeDefaultRemoved
    ) {
      setErrorMsg(t.agentProfileDefaultRequired);
      return;
    }

    setSaving(true);
    try {
      const headers = {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      };
      let response: Response;
      if (editingProfile) {
        const body: AgentProfileUpdate = {
          name: trimmedName,
          enabled,
          is_system_default: isSystemDefault,
          default_provider_id: effectiveDefaultProvider,
          default_model: effectiveDefaultModel,
          provider_models: providerModels,
          version,
        };
        response = await fetch(
          `/api/v1/agent-profiles/${encodeURIComponent(editingProfile.id)}`,
          {
            method: "PUT",
            headers,
            body: JSON.stringify(body),
          },
        );
      } else {
        const body: AgentProfileCreate = {
          name: trimmedName,
          runner_id: runnerId,
          enabled,
          is_system_default: isSystemDefault,
          default_provider_id: effectiveDefaultProvider,
          default_model: effectiveDefaultModel,
          provider_models: providerModels,
        };
        response = await fetch("/api/v1/agent-profiles", {
          method: "POST",
          headers,
          body: JSON.stringify(body),
        });
      }

      if (!response.ok) {
        const payload = (await response.json().catch(() => null)) as {
          detail?: unknown;
        } | null;
        const parsed = parseConflictDetail(payload?.detail, t.operationFailed);
        if (response.status === 409 && parsed.conflict) {
          setConflict(parsed.conflict);
        }
        setErrorMsg(parsed.message);
        return;
      }

      setNotice(t.agentProfileSaved);
      setEditingKey(null);
      notifyAssistantCatalogChanged();
      await loadAll();
    } catch {
      setErrorMsg(t.operationFailed);
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete(profile: AgentProfileView) {
    setDeletingId(profile.id);
    setErrorMsg("");
    setNotice("");
    try {
      const response = await fetch(
        `/api/v1/agent-profiles/${encodeURIComponent(profile.id)}`,
        {
          method: "DELETE",
          headers: { Authorization: `Bearer ${token}` },
        },
      );
      if (!response.ok) {
        const payload = (await response.json().catch(() => null)) as {
          detail?: unknown;
        } | null;
        const parsed = parseConflictDetail(payload?.detail, t.operationFailed);
        setErrorMsg(parsed.message);
        return;
      }
      setConfirmDeleteId("");
      setNotice(t.agentProfileDeleted);
      notifyAssistantCatalogChanged();
      await loadAll();
    } catch {
      setErrorMsg(t.operationFailed);
    } finally {
      setDeletingId("");
    }
  }

  const selectedRunnerObj = runnersById.get(runnerId);
  const selectedRunnerUnavailable =
    selectedRunnerObj != null && !selectedRunnerObj.available;
  const currentDefaultMissing =
    Boolean(
      editingProfile &&
      defaultProviderId &&
      defaultModel &&
      (editingProfile.missing_models?.[defaultProviderId]?.includes(
        defaultModel,
      ) ??
        false),
    ) ||
    Boolean(
      defaultProviderId &&
      defaultModel &&
      !defaultModelOptions.includes(defaultModel),
    );

  return (
    <section className="config-section admin-page">
      <div className="page-header">
        <div>
          <h1 className="page-title">{t.agentSettings}</h1>
          <p className="page-subtitle">{t.agentSettingsHint}</p>
        </div>
        <div className="page-actions">
          {editingKey === null ? (
            <Button type="primary" onClick={openCreate}>
              + {t.agentProfileAdd}
            </Button>
          ) : (
            <Button onClick={closeEditor}>{t.agentProfileBackToList}</Button>
          )}
        </div>
      </div>

      {loadError && <Alert type="error" title={loadError} />}
      {notice && <Alert type="success" title={notice} />}
      {errorMsg && !conflict && <Alert type="error" title={errorMsg} />}

      {conflict?.kind === "version" && (
        <Alert
          type="warning"
          showIcon
          title={t.agentProfileConflictTitle}
          description={
            <div className="row">
              <span>
                {conflict.message}
                {conflict.currentVersion != null
                  ? ` (v${conflict.currentVersion})`
                  : ""}
              </span>
              <Button
                size="small"
                type="primary"
                onClick={() => void refreshEditingProfile()}
              >
                {t.agentProfileConflictRefresh}
              </Button>
            </div>
          }
        />
      )}

      {conflict?.kind === "affected_defaults" && (
        <Alert
          type="warning"
          showIcon
          title={t.agentProfileAffectedDefaultsTitle}
          description={
            <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
              {conflict.items.map((item, idx) => (
                <li key={`${item.profile_id}-${item.kind}-${idx}`}>
                  <strong>
                    {item.kind === "system_default"
                      ? t.agentProfileAffectedSystemDefault
                      : t.agentProfileAffectedProfileDefault}
                  </strong>
                  : {item.profile_name}
                  {item.model ? (
                    <>
                      {" "}
                      — <code>{item.model}</code>
                    </>
                  ) : null}
                </li>
              ))}
            </ul>
          }
        />
      )}

      {editingKey === null ? (
        <div className="card section-card token-table-card">
          {loading ? (
            <div style={{ padding: 24 }} className="muted">
              {t.skillsLoading}
            </div>
          ) : profiles.length === 0 ? (
            <div className="empty-state">{t.agentProfileEmpty}</div>
          ) : (
            <div className="model-catalog-table-wrap" style={{ border: 0 }}>
              <table className="model-catalog-table">
                <thead>
                  <tr>
                    <th scope="col">{t.agentProfileColumnName}</th>
                    <th scope="col">{t.agentProfileColumnRunner}</th>
                    <th scope="col">{t.agentProfileColumnDefault}</th>
                    <th scope="col">{t.agentProfileColumnStatus}</th>
                    <th scope="col">{t.agentProfileColumnActions}</th>
                  </tr>
                </thead>
                <tbody>
                  {profiles.map((profile) => {
                    const runnerName =
                      runnersById.get(profile.runner_id)?.display_name ??
                      profile.runner_id;
                    const providerName = profile.default_provider_id
                      ? (providersById.get(profile.default_provider_id)?.name ??
                        profile.default_provider_id)
                      : "";
                    const defaultMissing = Boolean(
                      profile.default_provider_id &&
                      profile.default_model &&
                      profile.missing_models?.[
                        profile.default_provider_id
                      ]?.includes(profile.default_model),
                    );
                    const hasAnyMissing =
                      Object.keys(profile.missing_models ?? {}).length > 0;
                    const hasAnyPending =
                      Object.keys(profile.pending_verification ?? {}).length >
                      0;
                    return (
                      <tr key={profile.id}>
                        <td>
                          <strong>{profile.name}</strong>
                        </td>
                        <td>
                          <Tag>{runnerName}</Tag>
                          {!profile.runner_available && (
                            <Tag color="error">{t.runnerUnavailable}</Tag>
                          )}
                        </td>
                        <td>
                          {profile.default_model ? (
                            <span>
                              {providerName} /{" "}
                              <code className="model-id-wrap">
                                {profile.default_model}
                              </code>
                              {defaultMissing && (
                                <>
                                  {" "}
                                  <Tag color="warning">
                                    {t.agentProfileModelMissingBadge}
                                  </Tag>
                                </>
                              )}
                            </span>
                          ) : (
                            <span className="muted">—</span>
                          )}
                        </td>
                        <td>
                          {profile.is_system_default ? (
                            <Tag color="blue">
                              {t.agentProfileStatusSystemDefault}
                            </Tag>
                          ) : profile.enabled ? (
                            <Tag color="green">
                              {t.agentProfileStatusEnabled}
                            </Tag>
                          ) : (
                            <Tag>{t.agentProfileStatusDisabled}</Tag>
                          )}
                          {hasAnyMissing && !defaultMissing && (
                            <Tag color="warning">
                              {t.agentProfileModelMissingBadge}
                            </Tag>
                          )}
                          {hasAnyPending && (
                            <Tag color="gold">
                              {t.agentProfileModelPendingBadge}
                            </Tag>
                          )}
                        </td>
                        <td>
                          <div className="row">
                            <Button
                              type="link"
                              size="small"
                              onClick={() => openEdit(profile)}
                            >
                              {t.providerEdit}
                            </Button>
                            {!profile.is_system_default &&
                              (confirmDeleteId === profile.id ? (
                                <>
                                  <Button
                                    type="link"
                                    danger
                                    size="small"
                                    loading={deletingId === profile.id}
                                    onClick={() => void handleDelete(profile)}
                                  >
                                    {t.agentProfileConfirmDelete}
                                  </Button>
                                  <Button
                                    type="link"
                                    size="small"
                                    onClick={() => setConfirmDeleteId("")}
                                  >
                                    {t.cancel}
                                  </Button>
                                </>
                              ) : (
                                <Button
                                  type="link"
                                  danger
                                  size="small"
                                  onClick={() => setConfirmDeleteId(profile.id)}
                                >
                                  {t.agentProfileDelete}
                                </Button>
                              ))}
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      ) : (
        <form
          className="card section-card stack"
          onSubmit={(event) => void handleSubmit(event)}
        >
          <div className="card-title-row">
            <h2 className="card-title">
              {editingProfile
                ? `${t.agentProfileEditTitle} · ${editingProfile.name}`
                : t.agentProfileCreateTitle}
            </h2>
            {editingProfile && (
              <span className="muted mono">v{editingProfile.version}</span>
            )}
          </div>

          <div className="grid-2">
            <div className="field">
              <label className="field-label" htmlFor="agent-profile-name">
                {t.agentProfileName}
              </label>
              <Input
                id="agent-profile-name"
                aria-label={t.agentProfileName}
                placeholder={t.agentProfileNamePlaceholder}
                maxLength={200}
                value={name}
                onChange={(event) => setName(event.target.value)}
                required
              />
            </div>
            <div className="field">
              <label className="field-label" htmlFor="agent-profile-runner">
                {t.agentProfileRunner}
              </label>
              <select
                id="agent-profile-runner"
                aria-label={t.agentProfileRunner}
                value={runnerId}
                disabled={Boolean(editingProfile)}
                onChange={(event) => setRunnerId(event.target.value)}
              >
                {runners.length === 0 && (
                  <option value={runnerId || "pi"}>{runnerId || "pi"}</option>
                )}
                {runners.map((runner) => (
                  <option key={runner.id} value={runner.id}>
                    {runner.display_name}
                    {runner.available ? "" : ` (${t.runnerUnavailable})`}
                  </option>
                ))}
              </select>
              {editingProfile && (
                <span className="field-hint">
                  {t.agentProfileRunnerLockedHint}
                </span>
              )}
              {selectedRunnerUnavailable && (
                <span className="field-hint" style={{ color: "var(--red)" }}>
                  {t.runnerUnavailable}
                </span>
              )}
            </div>
          </div>

          <div className="row">
            <Checkbox
              checked={enabled}
              onChange={(event) => {
                const nextEnabled = event.target.checked;
                setEnabled(nextEnabled);
                if (!nextEnabled && isSystemDefault && !editingProfile) {
                  setIsSystemDefault(false);
                }
              }}
            >
              {t.agentProfileEnabled}
            </Checkbox>
            <Checkbox
              checked={isSystemDefault}
              disabled={Boolean(editingProfile?.is_system_default) || !enabled}
              onChange={(event) => setIsSystemDefault(event.target.checked)}
            >
              {t.agentProfileSystemDefault}
            </Checkbox>
            {editingProfile?.is_system_default && (
              <span className="field-hint">
                {t.agentProfileSystemDefaultLockedHint}
              </span>
            )}
          </div>

          <div className="stack" style={{ marginTop: 8 }}>
            <div>
              <h3 className="card-title">{t.agentProfileAvailableModels}</h3>
              <p className="field-hint">{t.agentProfileAvailableModelsHint}</p>
            </div>

            {providers.length === 0 ? (
              <div className="empty-state">{t.noProviders}</div>
            ) : (
              <>
                <div className="model-toolbar">
                  <div
                    className="model-filter-tabs"
                    role="group"
                    aria-label={t.agentProfileAvailableModels}
                  >
                    <button
                      type="button"
                      className="model-filter-tab"
                      aria-pressed={!pickedOnly}
                      onClick={() => setPickedOnly(false)}
                    >
                      {t.agentProfileFilterAll}
                    </button>
                    <button
                      type="button"
                      className="model-filter-tab"
                      aria-pressed={pickedOnly}
                      onClick={() => setPickedOnly(true)}
                    >
                      {t.agentProfileFilterSelected}
                    </button>
                  </div>

                  {providers.length > 1 && (
                    <select
                      aria-label={t.agentProfileProviderFilter}
                      value={providerFilter}
                      onChange={(event) =>
                        setProviderFilter(event.target.value)
                      }
                    >
                      <option value="__all__">
                        {t.agentProfileAllProviders}
                      </option>
                      {providers.map((provider) => (
                        <option key={provider.id} value={provider.id}>
                          {provider.name} (
                          {(providerModels[provider.id] ?? []).length})
                        </option>
                      ))}
                    </select>
                  )}

                  <input
                    type="search"
                    className="model-toolbar-search"
                    aria-label={t.agentProfileSearchModels}
                    placeholder={t.agentProfileSearchModels}
                    value={searchQuery}
                    onChange={(event) => setSearchQuery(event.target.value)}
                  />
                </div>

                {providerFilter !== "__all__" &&
                  providersById.get(providerFilter)?.catalog_fetched_at ==
                    null && (
                    <Alert type="info" title={t.agentProfileNoCatalogYet} />
                  )}

                <div className="model-bulk-bar">
                  <span className="field-hint" aria-live="polite">
                    {t.agentProfileSelectedCount
                      .replace("{selected}", String(totalSelectedCount))
                      .replace("{matched}", String(filteredRows.length))}
                  </span>
                  <div className="row">
                    <Button
                      size="small"
                      disabled={allFilteredSelectableChecked}
                      onClick={selectFiltered}
                    >
                      {t.agentProfileSelectFiltered}
                    </Button>
                    <Button
                      size="small"
                      disabled={noFilteredChecked}
                      onClick={clearFiltered}
                    >
                      {t.agentProfileClearFiltered}
                    </Button>
                  </div>
                </div>

                <div className="model-catalog-table-wrap">
                  <table className="model-catalog-table">
                    <thead>
                      <tr>
                        <th scope="col">{t.agentProfileColumnCheck}</th>
                        {providers.length > 1 && (
                          <th scope="col">{t.agentProfileColumnProvider}</th>
                        )}
                        <th scope="col">{t.agentProfileColumnModelId}</th>
                        <th scope="col">{t.agentProfileColumnProtocol}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {filteredRows.length === 0 ? (
                        <tr>
                          <td
                            colSpan={providers.length > 1 ? 4 : 3}
                            className="muted"
                          >
                            {t.agentProfileNoModelsMatch}
                          </td>
                        </tr>
                      ) : (
                        filteredRows.map((row) => {
                          const rowKey = `${row.providerId}:${row.modelId}`;
                          const isCopied = copiedModelKey === rowKey;
                          return (
                            <tr
                              key={rowKey}
                              className={row.checked ? "selected" : undefined}
                            >
                              <td>
                                <input
                                  type="checkbox"
                                  aria-label={`${row.providerName} ${row.modelId}`}
                                  checked={row.checked}
                                  disabled={!row.checked && !row.inCatalog}
                                  onChange={(event) =>
                                    toggleModel(row, event.target.checked)
                                  }
                                />
                              </td>
                              {providers.length > 1 && (
                                <td>{row.providerName}</td>
                              )}
                              <td>
                                <div className="model-id-cell">
                                  <code className="model-id-wrap">
                                    {row.modelId}
                                  </code>
                                  <Button
                                    size="small"
                                    type="text"
                                    className="model-copy-btn"
                                    icon={
                                      isCopied ? (
                                        <CheckOutlined />
                                      ) : (
                                        <CopyOutlined />
                                      )
                                    }
                                    aria-label={`${t.agentProfileCopyId} ${row.modelId}`}
                                    onClick={() => void copyModelId(row)}
                                  >
                                    {isCopied
                                      ? t.agentProfileCopied
                                      : t.agentProfileCopyId}
                                  </Button>
                                  {row.missing && (
                                    <Tag
                                      color="warning"
                                      icon={<WarningOutlined />}
                                    >
                                      {t.agentProfileModelMissingBadge}
                                    </Tag>
                                  )}
                                  {row.pending && (
                                    <Tag
                                      color="gold"
                                      icon={<ExclamationCircleOutlined />}
                                    >
                                      {t.agentProfileModelPendingBadge}
                                    </Tag>
                                  )}
                                </div>
                              </td>
                              <td>
                                <Tag>{protocolLabel(row.modelId, t)}</Tag>
                              </td>
                            </tr>
                          );
                        })
                      )}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </div>

          {currentDefaultMissing && (
            <Alert type="warning" title={t.agentProfileDefaultRequired} />
          )}

          <div className="grid-2">
            <div className="field">
              <label
                className="field-label"
                htmlFor="agent-profile-default-provider"
              >
                {t.agentProfileDefaultProvider}
              </label>
              <select
                id="agent-profile-default-provider"
                aria-label={t.agentProfileDefaultProvider}
                value={
                  defaultProviderOptions.some((o) => o.id === defaultProviderId)
                    ? defaultProviderId
                    : ""
                }
                onChange={(event) => {
                  setDefaultProviderId(event.target.value);
                  setDefaultModel("");
                }}
              >
                <option value="">{t.agentProfileSelectModelPlaceholder}</option>
                {defaultProviderOptions.map((opt) => (
                  <option key={opt.id} value={opt.id}>
                    {opt.name}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label
                className="field-label"
                htmlFor="agent-profile-default-model"
              >
                {t.agentProfileDefaultModel}
              </label>
              <select
                id="agent-profile-default-model"
                aria-label={t.agentProfileDefaultModel}
                value={
                  defaultModelOptions.includes(defaultModel) ? defaultModel : ""
                }
                onChange={(event) => setDefaultModel(event.target.value)}
              >
                <option value="">{t.agentProfileSelectModelPlaceholder}</option>
                {defaultModelOptions.map((modelId) => (
                  <option key={modelId} value={modelId}>
                    {modelId}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="save-bar">
            <Button htmlType="submit" type="primary" disabled={saving}>
              {t.agentProfileSave}
            </Button>
            <Button onClick={closeEditor}>{t.cancel}</Button>
          </div>
        </form>
      )}
    </section>
  );
}

export const AgentSettings = AgentProfilesPage;
