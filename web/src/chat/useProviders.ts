import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { parseAgentProfiles, type AgentProfiles } from "../agentProfiles";
import type { Messages } from "../i18n";
import {
  allowedEffortLevels,
  defaultEffort,
  modelEffortLevels,
} from "./effort";
import type {
  Agent,
  AgentModelPair,
  AssistantCatalogView,
  EffortLevel,
  Provider,
  PublishedAgentView,
} from "./types";
import { readErrorDetail } from "../httpErrors";

export const ASSISTANT_CATALOG_CHANGE_EVENT =
  "dataagent:assistant-catalog-changed";

export function notifyAssistantCatalogChanged() {
  window.dispatchEvent(new CustomEvent(ASSISTANT_CATALOG_CHANGE_EVENT));
}

export function isPublishedPair(
  profile: PublishedAgentView | undefined,
  providerId: string,
  model: string,
): boolean {
  if (!profile || !providerId || !model) return false;
  return profile.providers.some(
    (p) => p.id === providerId && p.models.includes(model),
  );
}

function toProviderView(
  published: PublishedAgentView["providers"][number],
): Provider {
  return {
    id: published.id,
    name: published.name,
    type: published.type,
    base_url: "",
    models: published.models,
    model_capabilities: published.model_capabilities,
    catalog_models: published.models,
    catalog_fetched_at: null,
    catalog_status: "ok",
    catalog_error: null,
    has_api_key: true,
    created_at: "",
    updated_at: "",
  };
}

function resolveCatalogSelection(
  profile: PublishedAgentView | undefined,
  personalDefaults: Record<string, AgentModelPair>,
): {
  providerId: string;
  model: string;
  missingDefaultPrompt: boolean;
} {
  if (!profile) {
    return { providerId: "", model: "", missingDefaultPrompt: true };
  }
  const personal = personalDefaults[profile.id];
  if (
    personal &&
    isPublishedPair(profile, personal.provider_id, personal.model)
  ) {
    return {
      providerId: personal.provider_id,
      model: personal.model,
      missingDefaultPrompt: false,
    };
  }
  if (
    profile.default_provider_id &&
    profile.default_model &&
    isPublishedPair(profile, profile.default_provider_id, profile.default_model)
  ) {
    return {
      providerId: profile.default_provider_id,
      model: profile.default_model,
      missingDefaultPrompt: false,
    };
  }
  return {
    providerId: profile.providers[0]?.id ?? "",
    model: "",
    missingDefaultPrompt: true,
  };
}

export function useProviders({
  token,
  messages,
  onError,
  refreshKey = 0,
  active = true,
  hasBoundConversation = false,
}: {
  token: string;
  messages: Messages;
  onError: (message: string) => void;
  refreshKey?: number;
  active?: boolean;
  hasBoundConversation?: boolean;
}) {
  const t = messages;
  const selectionTouched = useRef(false);

  // Catalog mode state (#329/#331)
  const [catalogMode, setCatalogMode] = useState(false);
  const [catalogLoaded, setCatalogLoaded] = useState(false);
  const [catalog, setCatalog] = useState<AssistantCatalogView | null>(null);
  const [missingDefaultPrompt, setMissingDefaultPrompt] = useState(false);
  const [personalDefaultNotice, setPersonalDefaultNotice] = useState("");
  const [personalDefaultError, setPersonalDefaultError] = useState("");
  const [savingPersonalDefault, setSavingPersonalDefault] = useState(false);

  // Legacy fallback state (for backward-compatible tests/environments)
  const [legacyAgents, setLegacyAgents] = useState<Agent[]>([]);
  const [legacyProfiles, setLegacyProfiles] = useState<AgentProfiles>({});
  const [legacyDefaults, setLegacyDefaults] = useState({
    agent: "pi",
    provider: "",
    model: "",
  });
  const [legacyProviders, setLegacyProviders] = useState<Provider[]>([]);

  const [agentId, setAgentId] = useState("pi");
  const [providerId, setProviderId] = useState("");
  const [model, setModel] = useState("");
  const [effort, setEffort] = useState<EffortLevel | "">("");

  const selectionRef = useRef({
    agentId: "pi",
    providerId: "",
    model: "",
    hasBoundConversation: false,
  });
  selectionRef.current = {
    agentId,
    providerId,
    model,
    hasBoundConversation,
  };

  const applyCatalogSnapshot = useCallback(
    (data: AssistantCatalogView, preserveSelection: boolean) => {
      setCatalog(data);
      setCatalogMode(true);
      setCatalogLoaded(true);

      if (preserveSelection) {
        const current = selectionRef.current;
        if (current.hasBoundConversation) return;
        const currentProfile = data.agents.find(
          (a) => a.id === current.agentId,
        );
        if (
          current.agentId &&
          current.providerId &&
          current.model &&
          isPublishedPair(currentProfile, current.providerId, current.model)
        ) {
          return;
        }
      }

      const targetProfile =
        (data.system_default_profile_id
          ? data.agents.find((a) => a.id === data.system_default_profile_id)
          : undefined) ??
        data.agents.find((a) => a.is_system_default) ??
        data.agents[0];

      if (!targetProfile) {
        setAgentId("");
        setProviderId("");
        setModel("");
        setMissingDefaultPrompt(false);
        return;
      }

      const resolved = resolveCatalogSelection(
        targetProfile,
        data.personal_defaults,
      );
      setAgentId(targetProfile.id);
      setProviderId(resolved.providerId);
      setModel(resolved.model);
      setMissingDefaultPrompt(resolved.missingDefaultPrompt);
    },
    [],
  );

  const loadLegacy = useCallback(
    async (signal: AbortSignal) => {
      try {
        const [agentsResponse, providersResponse] = await Promise.all([
          fetch("/api/v1/agents", {
            headers: { Authorization: `Bearer ${token}` },
            signal,
          }),
          fetch("/api/v1/providers", {
            headers: { Authorization: `Bearer ${token}` },
            signal,
          }),
        ]);
        if (!agentsResponse.ok || !providersResponse.ok) throw new Error();
        const agentRows = (await agentsResponse.json()) as Agent[];
        const providerRows = (await providersResponse.json()) as Provider[];
        if (!Array.isArray(agentRows) || !Array.isArray(providerRows)) {
          throw new Error("Invalid agent or provider list");
        }
        setLegacyAgents(agentRows);
        const available = agentRows.find((agent) => agent.available);
        if (available && !selectionTouched.current) {
          setAgentId((current) =>
            agentRows.some((agent) => agent.id === current && agent.available)
              ? current
              : available.id,
          );
        }
        setLegacyProviders(providerRows);
        setProviderId((current) => current || providerRows[0]?.id || "");
        setModel((current) => current || providerRows[0]?.models[0] || "");
      } catch {
        if (!signal.aborted) onError(t.askError);
      }

      try {
        const defaultsResponse = await fetch("/api/v1/assistant-defaults", {
          headers: { Authorization: `Bearer ${token}` },
          signal,
        });
        if (!defaultsResponse.ok) return;
        const data = (await defaultsResponse.json()) as {
          assistant_agent?: string | null;
          assistant_provider_id?: string | null;
          assistant_model?: string | null;
          assistant_agent_profiles?: string | null;
        };
        const parsedProfiles = parseAgentProfiles(
          data.assistant_agent_profiles,
        );
        setLegacyProfiles(parsedProfiles);
        const agent = data.assistant_agent || "pi";
        const profile = parsedProfiles[agent];
        const next = {
          agent,
          provider:
            profile?.default_provider || data.assistant_provider_id || "",
          model: profile?.default_model || data.assistant_model || "",
        };
        setLegacyDefaults(next);
        if (!selectionTouched.current) {
          setAgentId(next.agent);
          if (next.provider) setProviderId(next.provider);
          if (next.model) setModel(next.model);
        }
      } catch {
        /* Optional defaults endpoint */
      }
    },
    [token, t.askError, onError],
  );

  const refreshCatalog = useCallback(
    async (signal?: AbortSignal) => {
      try {
        const res = await fetch("/api/v1/assistant-catalog", {
          headers: { Authorization: `Bearer ${token}` },
          signal,
        });
        if (res.ok) {
          const data = (await res.json()) as Partial<AssistantCatalogView>;
          if (data && Array.isArray(data.agents)) {
            applyCatalogSnapshot(
              {
                system_default_profile_id:
                  data.system_default_profile_id ?? null,
                agents: data.agents,
                personal_defaults: data.personal_defaults ?? {},
              },
              selectionTouched.current,
            );
            return;
          }
        }
      } catch {
        if (signal?.aborted) return;
      }
      if (!signal?.aborted) {
        const controller = new AbortController();
        await loadLegacy(signal ?? controller.signal);
      }
    },
    [token, applyCatalogSnapshot, loadLegacy],
  );

  useEffect(() => {
    const controller = new AbortController();
    void refreshCatalog(controller.signal);
    return () => controller.abort();
  }, [refreshCatalog]);

  const prevActiveRef = useRef(active);
  const prevRefreshKeyRef = useRef(refreshKey);
  useEffect(() => {
    const becameActive = !prevActiveRef.current && active;
    const refreshChanged = prevRefreshKeyRef.current !== refreshKey;
    prevActiveRef.current = active;
    prevRefreshKeyRef.current = refreshKey;
    if (!becameActive && !refreshChanged) return;
    const controller = new AbortController();
    void refreshCatalog(controller.signal);
    return () => controller.abort();
  }, [active, refreshKey, refreshCatalog]);

  useEffect(() => {
    let controller: AbortController | null = null;
    const onCatalogChanged = () => {
      controller?.abort();
      controller = new AbortController();
      void refreshCatalog(controller.signal);
    };
    window.addEventListener(ASSISTANT_CATALOG_CHANGE_EVENT, onCatalogChanged);
    return () => {
      controller?.abort();
      window.removeEventListener(
        ASSISTANT_CATALOG_CHANGE_EVENT,
        onCatalogChanged,
      );
    };
  }, [refreshCatalog]);

  useEffect(() => {
    if (catalogMode || selectionTouched.current || !legacyProviders.length) {
      return;
    }
    const profile = legacyProfiles[legacyDefaults.agent];
    const options = legacyProviders.filter(
      (provider) =>
        !profile?.providers.length || profile.providers.includes(provider.id),
    );
    const chosen =
      options.find((provider) => provider.id === legacyDefaults.provider) ??
      options[0];
    setProviderId(chosen?.id || "");
    setModel(
      chosen?.models.includes(legacyDefaults.model)
        ? legacyDefaults.model
        : chosen?.models[0] || "",
    );
  }, [catalogMode, legacyProviders, legacyProfiles, legacyDefaults]);

  const catalogAgents = useMemo(() => catalog?.agents ?? [], [catalog]);
  const personalDefaults = useMemo(
    () => catalog?.personal_defaults ?? {},
    [catalog],
  );
  const activeCatalogAgent = useMemo(
    () => catalogAgents.find((item) => item.id === agentId),
    [catalogAgents, agentId],
  );

  const agents: Agent[] = useMemo(() => {
    if (!catalogMode) return legacyAgents;
    return catalogAgents.map((profile) => ({
      id: profile.id,
      name: profile.name,
      available: true,
      effort_levels: profile.effort_levels ?? [],
      supports_skills: profile.supports_skills ?? false,
    }));
  }, [catalogMode, catalogAgents, legacyAgents]);

  const profiles: AgentProfiles = useMemo(() => {
    if (!catalogMode) return legacyProfiles;
    return Object.fromEntries(
      catalogAgents.map((profile) => [
        profile.id,
        {
          providers: profile.providers.map((p) => p.id),
          default_provider: profile.default_provider_id ?? "",
          default_model: profile.default_model ?? "",
        },
      ]),
    );
  }, [catalogMode, catalogAgents, legacyProfiles]);

  const providers: Provider[] = useMemo(() => {
    if (!catalogMode) return legacyProviders;
    if (!activeCatalogAgent) return [];
    return activeCatalogAgent.providers.map(toProviderView);
  }, [catalogMode, activeCatalogAgent, legacyProviders]);

  const defaults = useMemo(() => {
    if (!catalogMode || !catalog) return legacyDefaults;
    const defaultProfile =
      (catalog.system_default_profile_id
        ? catalog.agents.find((a) => a.id === catalog.system_default_profile_id)
        : undefined) ??
      catalog.agents.find((a) => a.is_system_default) ??
      catalog.agents[0];
    if (!defaultProfile) {
      return { agent: "", provider: "", model: "" };
    }
    const resolved = resolveCatalogSelection(
      defaultProfile,
      catalog.personal_defaults,
    );
    return {
      agent: defaultProfile.id,
      provider: resolved.providerId,
      model: resolved.model,
    };
  }, [catalogMode, catalog, legacyDefaults]);

  const activeProvider = providers.find((item) => item.id === providerId);
  const effortLevels = modelEffortLevels(activeProvider, model);
  const allowedEfforts = allowedEffortLevels(
    agents,
    agentId,
    activeProvider,
    model,
  );
  const effortKey = `${effortLevels.join(",")}|${allowedEfforts.join(",")}`;

  useEffect(() => {
    setEffort((current) =>
      allowedEfforts.includes(current as EffortLevel)
        ? current
        : defaultEffort(allowedEfforts),
    );
  }, [effortKey]);

  function selectAgent(value: string) {
    selectionTouched.current = true;
    setPersonalDefaultNotice("");
    setPersonalDefaultError("");
    setAgentId(value);

    if (catalogMode && catalog) {
      const targetProfile = catalog.agents.find((a) => a.id === value);
      const resolved = resolveCatalogSelection(
        targetProfile,
        catalog.personal_defaults,
      );
      setProviderId(resolved.providerId);
      setModel(resolved.model);
      setMissingDefaultPrompt(resolved.missingDefaultPrompt);
      return;
    }

    const profile = legacyProfiles[value];
    const options = legacyProviders.filter(
      (provider) =>
        !profile?.providers.length || profile.providers.includes(provider.id),
    );
    const chosen =
      options.find((provider) => provider.id === profile?.default_provider) ??
      options[0];
    setProviderId(chosen?.id || "");
    setModel(
      profile?.default_model && chosen?.models.includes(profile.default_model)
        ? profile.default_model
        : chosen?.models[0] || "",
    );
  }

  function selectProvider(value: string) {
    selectionTouched.current = true;
    setPersonalDefaultNotice("");
    setPersonalDefaultError("");
    setProviderId(value);
    const provider = providers.find((item) => item.id === value);
    setModel(provider?.models[0] || "");
  }

  function selectModel(value: string) {
    selectionTouched.current = true;
    setPersonalDefaultNotice("");
    setPersonalDefaultError("");
    setModel(value);
    if (value) setMissingDefaultPrompt(false);
  }

  function selectEffort(value: EffortLevel | "") {
    setEffort(value);
  }

  function resetToNewConversationDefaults() {
    selectionTouched.current = true;
    setPersonalDefaultNotice("");
    setPersonalDefaultError("");
    if (catalogMode && catalog) {
      const targetProfile =
        (catalog.system_default_profile_id
          ? catalog.agents.find(
              (a) => a.id === catalog.system_default_profile_id,
            )
          : undefined) ??
        catalog.agents.find((a) => a.is_system_default) ??
        catalog.agents[0];
      if (!targetProfile) {
        setAgentId("");
        setProviderId("");
        setModel("");
        setMissingDefaultPrompt(false);
        return;
      }
      const resolved = resolveCatalogSelection(
        targetProfile,
        catalog.personal_defaults,
      );
      setAgentId(targetProfile.id);
      setProviderId(resolved.providerId);
      setModel(resolved.model);
      setMissingDefaultPrompt(resolved.missingDefaultPrompt);
      return;
    }

    setAgentId(legacyDefaults.agent);
    const profile = legacyProfiles[legacyDefaults.agent];
    const options = legacyProviders.filter(
      (provider) =>
        !profile?.providers.length || profile.providers.includes(provider.id),
    );
    const chosen =
      options.find((provider) => provider.id === legacyDefaults.provider) ??
      options[0];
    setProviderId(chosen?.id || "");
    setModel(
      chosen?.models.includes(legacyDefaults.model)
        ? legacyDefaults.model
        : chosen?.models[0] || "",
    );
  }

  async function savePersonalDefault() {
    if (!catalogMode || !agentId || !providerId || !model) return;
    setSavingPersonalDefault(true);
    setPersonalDefaultNotice("");
    setPersonalDefaultError("");
    try {
      const response = await fetch(
        `/api/v1/me/agent-defaults/${encodeURIComponent(agentId)}`,
        {
          method: "PUT",
          headers: {
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ provider_id: providerId, model }),
        },
      );
      if (!response.ok) {
        const detail = await readErrorDetail(
          response,
          t.chatPersonalDefaultError,
        );
        setPersonalDefaultError(detail);
        return;
      }
      setCatalog((prev) =>
        prev
          ? {
              ...prev,
              personal_defaults: {
                ...prev.personal_defaults,
                [agentId]: { provider_id: providerId, model },
              },
            }
          : prev,
      );
      setPersonalDefaultNotice(t.chatPersonalDefaultSaved);
    } catch {
      setPersonalDefaultError(t.chatPersonalDefaultError);
    } finally {
      setSavingPersonalDefault(false);
    }
  }

  async function resetPersonalDefault(isExistingConversation: boolean) {
    if (!catalogMode || !agentId) return;
    setSavingPersonalDefault(true);
    setPersonalDefaultNotice("");
    setPersonalDefaultError("");
    try {
      const response = await fetch(
        `/api/v1/me/agent-defaults/${encodeURIComponent(agentId)}`,
        {
          method: "DELETE",
          headers: { Authorization: `Bearer ${token}` },
        },
      );
      if (!response.ok) {
        const detail = await readErrorDetail(
          response,
          t.chatPersonalDefaultError,
        );
        setPersonalDefaultError(detail);
        return;
      }
      setCatalog((prev) => {
        if (!prev) return prev;
        const nextPersonal = { ...prev.personal_defaults };
        delete nextPersonal[agentId];
        return { ...prev, personal_defaults: nextPersonal };
      });
      if (!isExistingConversation && activeCatalogAgent) {
        const hasCompanyDefault =
          activeCatalogAgent.default_provider_id &&
          activeCatalogAgent.default_model &&
          isPublishedPair(
            activeCatalogAgent,
            activeCatalogAgent.default_provider_id,
            activeCatalogAgent.default_model,
          );
        if (hasCompanyDefault) {
          setProviderId(activeCatalogAgent.default_provider_id!);
          setModel(activeCatalogAgent.default_model!);
          setMissingDefaultPrompt(false);
        } else {
          setProviderId(activeCatalogAgent.providers[0]?.id ?? "");
          setModel("");
          setMissingDefaultPrompt(true);
        }
      }
      setPersonalDefaultNotice(t.chatPersonalDefaultReset);
    } catch {
      setPersonalDefaultError(t.chatPersonalDefaultError);
    } finally {
      setSavingPersonalDefault(false);
    }
  }

  const noPublishedAgents =
    catalogMode && catalogLoaded && catalogAgents.length === 0;

  return {
    catalogMode,
    catalogLoaded,
    catalog,
    catalogAgents,
    personalDefaults,
    activeCatalogAgent,
    noPublishedAgents,
    missingDefaultPrompt,
    personalDefaultNotice,
    personalDefaultError,
    savingPersonalDefault,
    agents,
    profiles,
    defaults,
    providers,
    agentId,
    providerId,
    model,
    effort,
    effortLevels,
    allowedEfforts,
    selectionTouched,
    activeProvider,
    setAgentId,
    setProviderId,
    setModel,
    setMissingDefaultPrompt,
    selectAgent,
    selectProvider,
    selectModel,
    selectEffort,
    refreshCatalog,
    resetToNewConversationDefaults,
    savePersonalDefault,
    resetPersonalDefault,
  };
}
