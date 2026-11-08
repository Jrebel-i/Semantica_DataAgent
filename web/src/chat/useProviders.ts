import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { parseAgentProfiles, type AgentProfiles } from "../agentProfiles";
import type { Messages } from "../i18n";
import {
  allowedEffortLevels,
  defaultEffort,
  modelEffortLevels,
} from "./effort";
import type { ProviderForm } from "./ProviderSettings";
import { EFFORT_LEVELS } from "./types";
import type {
  Agent,
  EffortLevel,
  Provider,
  ProviderProbeResult,
} from "./types";

export function useProviders({
  token,
  messages,
  onError,
}: {
  token: string;
  messages: Messages;
  onError: (message: string) => void;
}) {
  const t = messages;
  const [agents, setAgents] = useState<Agent[]>([]);
  const [profiles, setProfiles] = useState<AgentProfiles>({});
  const selectionTouched = useRef(false);
  const [defaults, setDefaults] = useState({
    agent: "pi",
    provider: "",
    model: "",
  });
  const [providers, setProviders] = useState<Provider[]>([]);
  const [agentId, setAgentId] = useState("pi");
  const [providerId, setProviderId] = useState("");
  const [model, setModel] = useState("");
  const [effort, setEffort] = useState<EffortLevel | "">("");
  const [providerForm, setProviderForm] = useState<ProviderForm>({
    name: "",
    type: "anthropic-messages",
    base_url: "",
    api_key: "",
    models: "",
    model_capabilities: {},
  });
  const [editingProviderId, setEditingProviderId] = useState("");
  const [providerError, setProviderError] = useState("");
  const [providerNotice, setProviderNotice] = useState("");
  const [probeResults, setProbeResults] = useState<
    Record<string, ProviderProbeResult>
  >({});
  const [probeLoadingModel, setProbeLoadingModel] = useState("");
  const [probeError, setProbeError] = useState("");

  const loadProviders = useCallback(async () => {
    try {
      const response = await fetch("/api/v1/providers", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) throw new Error();
      const data = (await response.json()) as Provider[];
      setProviders(data);
      setProviderId((current) => current || data[0]?.id || "");
      setModel((current) => current || data[0]?.models[0] || "");
    } catch {
      setProviderError(t.providerError);
    }
  }, [token, t.providerError]);

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      fetch("/api/v1/agents", {
        headers: { Authorization: `Bearer ${token}` },
        signal: controller.signal,
      }),
      fetch("/api/v1/providers", {
        headers: { Authorization: `Bearer ${token}` },
        signal: controller.signal,
      }),
    ])
      .then(async ([agentsResponse, providersResponse]) => {
        if (!agentsResponse.ok || !providersResponse.ok) throw new Error();
        const agentRows = (await agentsResponse.json()) as Agent[];
        const providerRows = (await providersResponse.json()) as Provider[];
        if (!Array.isArray(agentRows) || !Array.isArray(providerRows))
          throw new Error("Invalid agent or provider list");
        setAgents(agentRows);
        const available = agentRows.find((agent) => agent.available);
        if (available && !selectionTouched.current)
          setAgentId((current) =>
            agentRows.some((agent) => agent.id === current && agent.available)
              ? current
              : available.id,
          );
        setProviders(providerRows);
        setProviderId((current) => current || providerRows[0]?.id || "");
        setModel((current) => current || providerRows[0]?.models[0] || "");
      })
      .catch(() => {
        if (!controller.signal.aborted) onError(t.askError);
      });
    return () => controller.abort();
  }, [token, t.askError, onError]);

  useEffect(() => {
    void fetch("/api/v1/assistant-defaults", {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then(async (response) => {
        if (!response.ok) return;
        const data = (await response.json()) as {
          assistant_agent?: string | null;
          assistant_provider_id?: string | null;
          assistant_model?: string | null;
          assistant_agent_profiles?: string | null;
        };
        const parsedProfiles = parseAgentProfiles(
          data.assistant_agent_profiles,
        );
        setProfiles(parsedProfiles);
        const agent = data.assistant_agent || "pi";
        const profile = parsedProfiles[agent];
        const next = {
          agent,
          provider:
            profile?.default_provider || data.assistant_provider_id || "",
          model: profile?.default_model || data.assistant_model || "",
        };
        setDefaults(next);
        if (!selectionTouched.current) {
          setAgentId(next.agent);
          if (next.provider) setProviderId(next.provider);
          if (next.model) setModel(next.model);
        }
      })
      .catch(() => {});
  }, [token]);

  useEffect(() => {
    if (selectionTouched.current || !providers.length) return;
    const profile = profiles[defaults.agent];
    const options = providers.filter(
      (provider) =>
        !profile?.providers.length || profile.providers.includes(provider.id),
    );
    const chosen =
      options.find((provider) => provider.id === defaults.provider) ??
      options[0];
    setProviderId(chosen?.id || "");
    setModel(
      chosen?.models.includes(defaults.model)
        ? defaults.model
        : chosen?.models[0] || "",
    );
  }, [providers, profiles, defaults]);

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
    setAgentId(value);
    const profile = profiles[value];
    const options = providers.filter(
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
    setProviderId(value);
    const provider = providers.find((item) => item.id === value);
    setModel(provider?.models[0] || "");
  }

  function selectModel(value: string) {
    selectionTouched.current = true;
    setModel(value);
  }

  function selectEffort(value: EffortLevel | "") {
    setEffort(value);
  }

  function editProvider(provider: Provider) {
    setEditingProviderId(provider.id);
    setProviderForm({
      name: provider.name,
      type: provider.type,
      base_url: provider.base_url,
      api_key: "",
      models: provider.models.join(", "),
      model_capabilities: Object.fromEntries(
        provider.models.map((model) => {
          const capability = provider.model_capabilities?.[model];
          return [
            model,
            {
              description: capability?.description ?? "",
              vision: capability?.vision ?? false,
              effort_levels: [...(capability?.effort_levels ?? [])],
            },
          ];
        }),
      ),
    });
    setProviderError("");
    setProviderNotice("");
    setProbeResults({});
    setProbeLoadingModel("");
    setProbeError("");
  }

  function resetProviderForm() {
    setEditingProviderId("");
    setProviderForm({
      name: "",
      type: "anthropic-messages",
      base_url: "",
      api_key: "",
      models: "",
      model_capabilities: {},
    });
    setProbeResults({});
    setProbeLoadingModel("");
    setProbeError("");
  }

  async function probeProviderModel(providerId: string, model: string) {
    if (!providerId) return;
    setProbeError("");
    setProbeLoadingModel(model);
    try {
      const response = await fetch(`/api/v1/providers/${providerId}/probe`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ model }),
      });
      if (response.status === 409) {
        setProbeError(t.providerProbeBusy);
        return;
      }
      if (!response.ok) throw new Error();
      const data = (await response.json()) as ProviderProbeResult;
      setProbeResults((current) => ({ ...current, [model]: data }));
    } catch {
      setProbeError(t.providerProbeFailed);
    } finally {
      setProbeLoadingModel("");
    }
  }

  function applyProbeResult(model: string) {
    const result = probeResults[model];
    if (!result) return;
    setProviderForm((current) => ({
      ...current,
      model_capabilities: {
        ...current.model_capabilities,
        [model]: {
          ...(current.model_capabilities[model] ?? {
            description: "",
            vision: false,
            effort_levels: [],
          }),
          vision: result.vision === "yes",
          effort_levels: EFFORT_LEVELS.filter(
            (level) => result.effort[level] === "yes",
          ),
        },
      },
    }));
  }

  async function saveProvider(event: FormEvent) {
    event.preventDefault();
    setProviderError("");
    setProviderNotice("");
    const models = providerForm.models
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
    const payload: Record<string, unknown> = {
      name: providerForm.name.trim(),
      type: providerForm.type,
      base_url: providerForm.base_url.trim(),
      models,
      model_capabilities: Object.fromEntries(
        models.map((model) => {
          const capability = providerForm.model_capabilities[model];
          return [
            model,
            {
              description: capability?.description ?? "",
              vision: capability?.vision ?? false,
              effort_levels: capability?.effort_levels ?? [],
            },
          ];
        }),
      ),
    };
    if (providerForm.api_key.trim()) payload.api_key = providerForm.api_key;
    try {
      const response = await fetch(
        editingProviderId
          ? `/api/v1/providers/${editingProviderId}`
          : "/api/v1/providers",
        {
          method: editingProviderId ? "PUT" : "POST",
          headers: {
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify(payload),
        },
      );
      if (!response.ok) throw new Error();
      resetProviderForm();
      setProviderNotice(t.providerSaved);
      await loadProviders();
    } catch {
      setProviderError(t.providerError);
    }
  }

  async function deleteProvider(provider: Provider) {
    if (!window.confirm(t.providerDeleteConfirm)) return;
    setProviderError("");
    try {
      const response = await fetch(`/api/v1/providers/${provider.id}`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) throw new Error();
      if (providerId === provider.id) {
        setProviderId("");
        setModel("");
      }
      if (editingProviderId === provider.id) resetProviderForm();
      await loadProviders();
    } catch {
      setProviderError(t.providerError);
    }
  }

  return {
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
    providerForm,
    editingProviderId,
    providerError,
    providerNotice,
    probeResults,
    probeLoadingModel,
    probeError,
    activeProvider,
    setAgentId,
    setProviderId,
    setModel,
    setProviderForm,
    selectAgent,
    selectProvider,
    selectModel,
    selectEffort,
    loadProviders,
    editProvider,
    resetProviderForm,
    saveProvider,
    deleteProvider,
    probeProviderModel,
    applyProbeResult,
  };
}
