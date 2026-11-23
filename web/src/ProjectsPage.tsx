import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Checkbox,
  Input,
  Modal,
  Select,
  Space,
  Switch,
  Table,
  Tag,
} from "antd";
import type { components } from "./api";
import type { Messages } from "./i18n";
import { downloadOssieModel, exportErrorMessage } from "./ossieExport";
import { TableScroll } from "./TableScroll";

type ProjectRow = components["schemas"]["ProjectView"];
type EvolutionPolicy = components["schemas"]["EvolutionPolicyView"];
type PullInstance = components["schemas"]["PullInstanceView"];
type ProjectRole = "admin" | "member";

type MemberRow = {
  id: string;
  name: string;
  role: ProjectRole;
};

type UserRow = {
  id: string;
  name: string;
  role: "user" | "admin";
};

type ParamSchema = {
  type?: string;
  title?: string;
  format?: string;
  default?: unknown;
  enum?: (string | number | boolean)[];
  items?: ParamSchema;
  properties?: Record<string, ParamSchema>;
  required?: string[];
};

type AdapterRow = Omit<
  components["schemas"]["ProjectAdapterView"],
  "parameters_schema"
> & {
  parameters_schema: ParamSchema;
};

type PullerPlugin = Omit<
  components["schemas"]["PullerPluginView"],
  "parameters_schema"
> & {
  parameters_schema: ParamSchema;
};

type ProjectExecutor = Omit<
  components["schemas"]["ProjectExecutorView"],
  "parameters_schema"
> & {
  parameters_schema: ParamSchema;
};

const PROJECT_TYPE_LABELS: Record<string, string> = {
  warehouse: "数仓型",
  document: "文档型",
};

function isSecretField(
  name: string,
  field: ParamSchema,
  secretKeys?: string[],
) {
  return field.format === "password" || (secretKeys?.includes(name) ?? false);
}

function buildPullParametersPayload(
  schema: ParamSchema,
  values: Record<string, unknown>,
  secretKeys: string[],
  configuredSecrets: Set<string>,
): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  const properties = schema.properties || {};
  for (const [name, field] of Object.entries(properties)) {
    const raw = values[name];
    if (isSecretField(name, field, secretKeys)) {
      const wasConfigured = configuredSecrets.has(name);
      if (raw === "***") {
        if (wasConfigured) result[name] = "***";
      } else if (raw === "") {
        if (wasConfigured) result[name] = "";
      } else if (typeof raw === "string" && raw.length > 0) {
        result[name] = raw;
      }
      continue;
    }
    const effective = raw !== undefined ? raw : field.default;
    if (effective === undefined || effective === null || effective === "") {
      continue;
    }
    if (field.type === "integer" || field.type === "number") {
      const num = Number(effective);
      if (Number.isFinite(num)) result[name] = num;
    } else {
      result[name] = effective;
    }
  }
  return result;
}

function cleanExecutorParameters(
  schema: ParamSchema,
  values: Record<string, unknown>,
): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  const properties = schema.properties || {};
  for (const [name, field] of Object.entries(properties)) {
    const raw = values[name];
    if (raw === undefined || raw === null || raw === "") continue;
    if (field.type === "integer" || field.type === "number") {
      const num = Number(raw);
      if (Number.isFinite(num)) result[name] = num;
    } else {
      result[name] = raw;
    }
  }
  return result;
}

async function parseErrorDetail(
  response: Response,
  fallback: string,
): Promise<string> {
  try {
    const body = (await response.json()) as {
      detail?:
        | string
        | Array<{
            path?: (string | number)[];
            loc?: (string | number)[];
            message?: string;
            msg?: string;
          }>;
    };
    if (typeof body.detail === "string" && body.detail) return body.detail;
    if (Array.isArray(body.detail) && body.detail.length > 0) {
      const joined = body.detail
        .map((issue) => {
          const rawPath = issue.path ?? issue.loc;
          const path = rawPath?.length ? rawPath.join(".") : "";
          const message = issue.message ?? issue.msg ?? "";
          if (path && message) return `${path}: ${message}`;
          return message || path;
        })
        .filter((item) => item.length > 0)
        .join("; ");
      return joined || fallback;
    }
  } catch {
    // Ignore non-JSON bodies
  }
  return fallback;
}

function ParameterFields({
  schema,
  values,
  disabled,
  secretKeys,
  onChange,
}: {
  schema: ParamSchema;
  values: Record<string, unknown>;
  disabled: boolean;
  secretKeys?: string[];
  onChange: (name: string, value: unknown) => void;
}) {
  return (
    <>
      {Object.entries(schema.properties || {}).map(([name, field]) => {
        const value = values[name] ?? field.default;
        const secret = isSecretField(name, field, secretKeys);
        const isNumeric = field.type === "integer" || field.type === "number";
        return (
          <label className="field" key={name}>
            <span className="field-label">
              {field.title || name}
              {schema.required?.includes(name) ? " *" : ""}
            </span>
            {field.enum ? (
              <Select
                aria-label={name}
                disabled={disabled}
                value={typeof value === "string" ? value : undefined}
                onChange={(next) => onChange(name, next)}
                options={field.enum.map((item) => ({
                  value: item,
                  label: String(item),
                }))}
              />
            ) : field.type === "boolean" ? (
              <Checkbox
                aria-label={name}
                disabled={disabled}
                checked={value === true}
                onChange={(event) => onChange(name, event.target.checked)}
              >
                {field.title || name}
              </Checkbox>
            ) : field.type === "array" ? (
              <Input.TextArea
                aria-label={name}
                disabled={disabled}
                value={Array.isArray(value) ? value.join("\n") : ""}
                onChange={(event) =>
                  onChange(
                    name,
                    event.target.value ? event.target.value.split("\n") : [],
                  )
                }
              />
            ) : (
              <Input
                aria-label={name}
                disabled={disabled}
                type={secret ? "password" : isNumeric ? "number" : "text"}
                value={value == null ? "" : String(value)}
                onChange={(event) =>
                  onChange(
                    name,
                    isNumeric && event.target.value !== ""
                      ? Number(event.target.value)
                      : event.target.value,
                  )
                }
              />
            )}
          </label>
        );
      })}
    </>
  );
}

function localTime(value: string | null) {
  return value ? new Date(value).toLocaleString() : "";
}

export function ProjectsPage({
  token,
  messages: t,
  canCreate = true,
  includeMemberProjects = false,
}: {
  token: string;
  messages: Messages;
  canCreate?: boolean;
  includeMemberProjects?: boolean;
}) {
  const [rows, setRows] = useState<ProjectRow[]>([]);
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [type, setType] = useState<ProjectRow["type"]>("warehouse");
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<ProjectRow | null>(null);
  const [members, setMembers] = useState<MemberRow[]>([]);
  const [users, setUsers] = useState<UserRow[]>([]);
  const [addUserId, setAddUserId] = useState<string>();
  const [addRole, setAddRole] = useState<ProjectRole>("member");
  const [memberError, setMemberError] = useState("");
  const [confirmMember, setConfirmMember] = useState<MemberRow | null>(null);
  const [archiveTarget, setArchiveTarget] = useState<ProjectRow | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ProjectRow | null>(null);
  const [deleteName, setDeleteName] = useState("");
  const [lifecycleBusy, setLifecycleBusy] = useState(false);
  const [adapters, setAdapters] = useState<AdapterRow[]>([]);
  const [adapterError, setAdapterError] = useState("");
  const [exportingFormat, setExportingFormat] = useState<
    "yaml" | "json" | null
  >(null);
  const [policy, setPolicy] = useState<EvolutionPolicy>({
    correction_require_review: true,
    evolution_require_review: true,
  });
  const [policyBusy, setPolicyBusy] = useState(false);
  const [policyError, setPolicyError] = useState("");
  const [policyNotice, setPolicyNotice] = useState("");
  const [pullerPlugins, setPullerPlugins] = useState<PullerPlugin[]>([]);
  const [pullInstances, setPullInstances] = useState<PullInstance[]>([]);
  const [pullError, setPullError] = useState("");
  const [instanceModalOpen, setInstanceModalOpen] = useState(false);
  const [editingInstance, setEditingInstance] = useState<PullInstance | null>(
    null,
  );
  const [instId, setInstId] = useState("");
  const [instPluginId, setInstPluginId] = useState("");
  const [instDisplayName, setInstDisplayName] = useState("");
  const [instEnabled, setInstEnabled] = useState(true);
  const [instScheduleCron, setInstScheduleCron] = useState("");
  const [instTimezone, setInstTimezone] = useState("Asia/Shanghai");
  const [instCatalogPrefix, setInstCatalogPrefix] = useState("");
  const [instTaskNamespace, setInstTaskNamespace] = useState("");
  const [instDropThreshold, setInstDropThreshold] = useState("0.5");
  const [instParams, setInstParams] = useState<Record<string, unknown>>({});
  const [instFormError, setInstFormError] = useState("");
  const [instSaving, setInstSaving] = useState(false);
  const [deleteInstanceTarget, setDeleteInstanceTarget] =
    useState<PullInstance | null>(null);
  const [executors, setExecutors] = useState<ProjectExecutor[]>([]);
  const [executorError, setExecutorError] = useState("");
  const [executorNotice, setExecutorNotice] = useState("");
  const [executorBusy, setExecutorBusy] = useState(false);

  const projectLoadGen = useRef(0);
  const projectAbortRef = useRef<AbortController | null>(null);
  const savedEnabledExecutorsRef = useRef<Set<string>>(new Set());

  const authHeaders = { Authorization: `Bearer ${token}` };
  const refresh = useCallback(
    async (signal?: AbortSignal) => {
      try {
        const response = await fetch("/api/v1/projects", {
          headers: authHeaders,
          signal,
        });
        if (!response.ok) throw new Error();
        const data = (await response.json()) as ProjectRow[];
        if (signal?.aborted) return;
        setRows(data);
      } catch {
        if (!signal?.aborted) setError(t.projectError);
      }
    },
    [token, t.projectError],
  );
  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => {
      controller.abort();
      projectAbortRef.current?.abort();
    };
  }, [refresh]);

  const loadMembers = useCallback(
    async (
      projectId: string,
      signal?: AbortSignal,
      expectedGen = projectLoadGen.current,
    ) => {
      try {
        const [memberResponse, userResponse] = await Promise.all([
          fetch(`/api/v1/projects/${projectId}/members`, {
            headers: authHeaders,
            signal,
          }),
          fetch("/api/v1/users", { headers: authHeaders, signal }),
        ]);
        if (!memberResponse.ok || !userResponse.ok) throw new Error();
        const [nextMembers, nextUsers] = (await Promise.all([
          memberResponse.json(),
          userResponse.json(),
        ])) as [MemberRow[], UserRow[]];
        if (signal?.aborted || projectLoadGen.current !== expectedGen) return;
        setMembers(nextMembers);
        setUsers(nextUsers);
        setMemberError("");
      } catch {
        if (!signal?.aborted && projectLoadGen.current === expectedGen) {
          setMemberError(t.memberError);
        }
      }
    },
    [token, t.memberError],
  );

  const loadAdapters = useCallback(
    async (
      projectId: string,
      signal?: AbortSignal,
      expectedGen = projectLoadGen.current,
    ) => {
      try {
        const response = await fetch(`/api/v1/projects/${projectId}/adapters`, {
          headers: authHeaders,
          signal,
        });
        if (!response.ok) throw new Error();
        const nextAdapters = (await response.json()) as AdapterRow[];
        if (signal?.aborted || projectLoadGen.current !== expectedGen) return;
        setAdapters(nextAdapters);
        setAdapterError("");
      } catch {
        if (!signal?.aborted && projectLoadGen.current === expectedGen) {
          setAdapterError(t.adapterSaveFailed);
        }
      }
    },
    [token, t.adapterSaveFailed],
  );

  const loadPolicy = useCallback(
    async (
      projectId: string,
      signal?: AbortSignal,
      expectedGen = projectLoadGen.current,
    ) => {
      try {
        const response = await fetch(
          `/api/v1/projects/${projectId}/evolution-policy`,
          { headers: authHeaders, signal },
        );
        if (!response.ok) return;
        const data = (await response.json()) as Partial<EvolutionPolicy>;
        if (signal?.aborted || projectLoadGen.current !== expectedGen) return;
        setPolicy({
          correction_require_review: data.correction_require_review ?? true,
          evolution_require_review: data.evolution_require_review ?? true,
        });
        setPolicyError("");
      } catch {
        // Keep safe defaults if the policy endpoint fails transiently
      }
    },
    [token],
  );

  const loadPullerSection = useCallback(
    async (
      projectId: string,
      signal?: AbortSignal,
      expectedGen = projectLoadGen.current,
    ) => {
      try {
        const [pluginsRes, instancesRes] = await Promise.all([
          fetch("/api/v1/puller-plugins", { headers: authHeaders, signal }),
          fetch(
            `/api/v1/projects/${encodeURIComponent(projectId)}/pull-instances`,
            { headers: authHeaders, signal },
          ),
        ]);
        if (!pluginsRes.ok || !instancesRes.ok) throw new Error();
        const [nextPlugins, nextInstances] = (await Promise.all([
          pluginsRes.json(),
          instancesRes.json(),
        ])) as [PullerPlugin[], PullInstance[]];
        if (signal?.aborted || projectLoadGen.current !== expectedGen) return;
        setPullerPlugins(Array.isArray(nextPlugins) ? nextPlugins : []);
        setPullInstances(Array.isArray(nextInstances) ? nextInstances : []);
        setPullError("");
      } catch {
        if (!signal?.aborted && projectLoadGen.current === expectedGen) {
          setPullError(t.pullInstanceSaveFailed);
        }
      }
    },
    [token, t.pullInstanceSaveFailed],
  );

  const loadExecutors = useCallback(
    async (
      projectId: string,
      signal?: AbortSignal,
      expectedGen = projectLoadGen.current,
    ) => {
      try {
        const response = await fetch(
          `/api/v1/projects/${encodeURIComponent(projectId)}/executors`,
          { headers: authHeaders, signal },
        );
        if (!response.ok) throw new Error();
        const nextExecutors = (await response.json()) as ProjectExecutor[];
        if (signal?.aborted || projectLoadGen.current !== expectedGen) return;
        const list = Array.isArray(nextExecutors) ? nextExecutors : [];
        savedEnabledExecutorsRef.current = new Set(
          list.filter((row) => row.enabled).map((row) => row.id),
        );
        setExecutors(list);
        setExecutorError("");
      } catch {
        if (!signal?.aborted && projectLoadGen.current === expectedGen) {
          setExecutorError(t.executorSaveFailed);
        }
      }
    },
    [token, t.executorSaveFailed],
  );

  async function openProject(project: ProjectRow) {
    projectAbortRef.current?.abort();
    const controller = new AbortController();
    projectAbortRef.current = controller;
    const expectedGen = projectLoadGen.current + 1;
    projectLoadGen.current = expectedGen;

    setSelected(project);
    setMemberError("");
    setAdapterError("");
    setPolicyError("");
    setPolicyNotice("");
    setPullError("");
    setExecutorError("");
    setExecutorNotice("");
    setAdapters([]);
    setMembers([]);
    setUsers([]);
    setPullerPlugins([]);
    setPullInstances([]);
    savedEnabledExecutorsRef.current = new Set();
    setExecutors([]);
    setInstanceModalOpen(false);
    setEditingInstance(null);
    setDeleteInstanceTarget(null);
    setAddUserId(undefined);
    setAddRole("member");

    await Promise.all([
      project.my_role === "admin"
        ? loadMembers(project.id, controller.signal, expectedGen)
        : undefined,
      project.my_role === "admin"
        ? loadPolicy(project.id, controller.signal, expectedGen)
        : undefined,
      loadAdapters(project.id, controller.signal, expectedGen),
      project.type === "warehouse"
        ? loadPullerSection(project.id, controller.signal, expectedGen)
        : undefined,
      project.type === "warehouse"
        ? loadExecutors(project.id, controller.signal, expectedGen)
        : undefined,
    ]);
  }

  async function togglePolicy(field: keyof EvolutionPolicy, checked: boolean) {
    if (!selected) return;
    setPolicyBusy(true);
    setPolicyError("");
    setPolicyNotice("");
    try {
      const response = await fetch(
        `/api/v1/projects/${selected.id}/evolution-policy`,
        {
          method: "PUT",
          headers: { ...authHeaders, "Content-Type": "application/json" },
          body: JSON.stringify({ [field]: checked }),
        },
      );
      if (!response.ok) {
        throw new Error(await errorDetail(response, t.evolutionPolicyFailed));
      }
      const updated = (await response.json()) as Partial<EvolutionPolicy>;
      setPolicy({
        correction_require_review:
          updated.correction_require_review ??
          (field === "correction_require_review"
            ? checked
            : policy.correction_require_review),
        evolution_require_review:
          updated.evolution_require_review ??
          (field === "evolution_require_review"
            ? checked
            : policy.evolution_require_review),
      });
      setPolicyNotice(t.evolutionPolicySaved);
    } catch (failure) {
      setPolicyError(
        failure instanceof Error ? failure.message : t.evolutionPolicyFailed,
      );
    } finally {
      setPolicyBusy(false);
    }
  }

  function updateAdapter(id: string, patch: Partial<AdapterRow>) {
    setAdapters((old) =>
      old.map((row) => (row.id === id ? { ...row, ...patch } : row)),
    );
  }

  function updateAdapterParameter(id: string, name: string, value: unknown) {
    setAdapters((old) =>
      old.map((row) =>
        row.id === id
          ? { ...row, parameters: { ...row.parameters, [name]: value } }
          : row,
      ),
    );
  }

  async function saveAdapters() {
    if (!selected) return;
    try {
      const response = await fetch(`/api/v1/projects/${selected.id}/adapters`, {
        method: "PUT",
        headers: { ...authHeaders, "Content-Type": "application/json" },
        body: JSON.stringify({
          adapters: adapters.map((row) => ({
            id: row.id,
            enabled: row.enabled,
            parameters: row.parameters,
          })),
        }),
      });
      if (!response.ok) throw new Error();
      setAdapterError("");
      await loadAdapters(selected.id);
    } catch {
      setAdapterError(t.adapterSaveFailed);
    }
  }

  function secretKeysForPlugin(plugin: PullerPlugin | undefined): string[] {
    if (!plugin) return [];
    const keys = new Set<string>(plugin.secret_keys || []);
    Object.entries(plugin.parameters_schema.properties || {}).forEach(
      ([key, field]) => {
        if (field.format === "password") keys.add(key);
      },
    );
    return Array.from(keys);
  }

  function configuredSecretSet(
    instance: PullInstance | null,
    plugin: PullerPlugin | undefined,
  ): Set<string> {
    if (!instance) return new Set();
    if (instance.configured_secrets && instance.configured_secrets.length > 0) {
      return new Set(instance.configured_secrets);
    }
    if (instance.secret_configured) {
      return new Set(secretKeysForPlugin(plugin));
    }
    return new Set();
  }

  function openCreateInstance() {
    const defaultPlugin = pullerPlugins[0];
    setEditingInstance(null);
    setInstId("");
    setInstPluginId(defaultPlugin?.id ?? "dtb");
    setInstDisplayName("");
    setInstEnabled(true);
    setInstScheduleCron("");
    setInstTimezone("Asia/Shanghai");
    setInstCatalogPrefix("");
    setInstTaskNamespace("");
    setInstDropThreshold("0.5");
    setInstParams({});
    setInstFormError("");
    setInstanceModalOpen(true);
  }

  function openEditInstance(instance: PullInstance) {
    const plugin = pullerPlugins.find((p) => p.id === instance.plugin_id);
    const configured = configuredSecretSet(instance, plugin);
    const initialParams: Record<string, unknown> = { ...instance.parameters };
    for (const key of configured) {
      initialParams[key] = "***";
    }
    setEditingInstance(instance);
    setInstId(instance.id);
    setInstPluginId(instance.plugin_id);
    setInstDisplayName(instance.display_name);
    setInstEnabled(instance.enabled);
    setInstScheduleCron(instance.schedule_cron ?? "");
    setInstTimezone(instance.timezone || "Asia/Shanghai");
    setInstCatalogPrefix(instance.catalog_prefix ?? "");
    setInstTaskNamespace(instance.task_namespace ?? "");
    setInstDropThreshold(String(instance.drop_threshold ?? 0.5));
    setInstParams(initialParams);
    setInstFormError("");
    setInstanceModalOpen(true);
  }

  async function savePullInstance() {
    if (!selected) return;
    const plugin = pullerPlugins.find((p) => p.id === instPluginId);
    const secretKeys = secretKeysForPlugin(plugin);
    const configured = configuredSecretSet(editingInstance, plugin);
    const parametersPayload = buildPullParametersPayload(
      plugin?.parameters_schema ?? { type: "object", properties: {} },
      instParams,
      secretKeys,
      configured,
    );
    const dropValue = Number(instDropThreshold || "0.5");

    setInstSaving(true);
    setInstFormError("");
    try {
      const url = editingInstance
        ? `/api/v1/projects/${encodeURIComponent(selected.id)}/pull-instances/${encodeURIComponent(editingInstance.id)}`
        : `/api/v1/projects/${encodeURIComponent(selected.id)}/pull-instances`;
      const method = editingInstance ? "PUT" : "POST";
      const body = editingInstance
        ? {
            display_name: instDisplayName.trim(),
            enabled: instEnabled,
            parameters: parametersPayload,
            schedule_cron: instScheduleCron.trim() || null,
            timezone: instTimezone.trim() || "Asia/Shanghai",
            catalog_prefix: instCatalogPrefix.trim() || null,
            task_namespace: instTaskNamespace.trim() || null,
            drop_threshold: dropValue,
          }
        : {
            id: instId.trim(),
            plugin_id: instPluginId,
            display_name: instDisplayName.trim(),
            enabled: instEnabled,
            parameters: parametersPayload,
            schedule_cron: instScheduleCron.trim() || null,
            timezone: instTimezone.trim() || "Asia/Shanghai",
            catalog_prefix: instCatalogPrefix.trim() || null,
            task_namespace: instTaskNamespace.trim() || null,
            drop_threshold: dropValue,
          };
      const response = await fetch(url, {
        method,
        headers: { ...authHeaders, "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!response.ok) {
        throw new Error(
          await parseErrorDetail(response, t.pullInstanceSaveFailed),
        );
      }
      setInstanceModalOpen(false);
      setEditingInstance(null);
      setPullError("");
      await loadPullerSection(selected.id);
    } catch (failure) {
      setInstFormError(
        failure instanceof Error ? failure.message : t.pullInstanceSaveFailed,
      );
    } finally {
      setInstSaving(false);
    }
  }

  async function deletePullInstance() {
    if (!selected || !deleteInstanceTarget) return;
    setInstSaving(true);
    try {
      const response = await fetch(
        `/api/v1/projects/${encodeURIComponent(selected.id)}/pull-instances/${encodeURIComponent(deleteInstanceTarget.id)}`,
        { method: "DELETE", headers: authHeaders },
      );
      if (!response.ok) {
        throw new Error(
          await parseErrorDetail(response, t.pullInstanceSaveFailed),
        );
      }
      setDeleteInstanceTarget(null);
      setPullError("");
      await loadPullerSection(selected.id);
    } catch (failure) {
      setPullError(
        failure instanceof Error ? failure.message : t.pullInstanceSaveFailed,
      );
      setDeleteInstanceTarget(null);
    } finally {
      setInstSaving(false);
    }
  }

  function toggleExecutorEnabled(id: string, checked: boolean) {
    setExecutors((old) =>
      old.map((row) =>
        row.id === id
          ? { ...row, enabled: checked }
          : checked
            ? { ...row, enabled: false }
            : row,
      ),
    );
    setExecutorNotice("");
  }

  function updateExecutorParameter(id: string, name: string, value: unknown) {
    setExecutors((old) =>
      old.map((row) =>
        row.id === id
          ? { ...row, parameters: { ...row.parameters, [name]: value } }
          : row,
      ),
    );
    setExecutorNotice("");
  }

  async function saveExecutors() {
    if (!selected) return;
    setExecutorBusy(true);
    setExecutorError("");
    setExecutorNotice("");
    try {
      const payloadExecutors = executors
        .filter(
          (row) => row.enabled || savedEnabledExecutorsRef.current.has(row.id),
        )
        .map((row) => ({
          id: row.id,
          enabled: row.enabled,
          parameters: cleanExecutorParameters(
            row.parameters_schema,
            row.parameters,
          ),
        }));
      const response = await fetch(
        `/api/v1/projects/${encodeURIComponent(selected.id)}/executors`,
        {
          method: "PUT",
          headers: { ...authHeaders, "Content-Type": "application/json" },
          body: JSON.stringify({ executors: payloadExecutors }),
        },
      );
      if (!response.ok) {
        throw new Error(await parseErrorDetail(response, t.executorSaveFailed));
      }
      const updated = (await response.json()) as ProjectExecutor[];
      const nextList = Array.isArray(updated) ? updated : [];
      savedEnabledExecutorsRef.current = new Set(
        nextList.filter((row) => row.enabled).map((row) => row.id),
      );
      setExecutors(nextList);
      setExecutorNotice(t.executorSaved);
    } catch (failure) {
      setExecutorError(
        failure instanceof Error ? failure.message : t.executorSaveFailed,
      );
    } finally {
      setExecutorBusy(false);
    }
  }

  async function create() {
    try {
      const response = await fetch("/api/v1/projects", {
        method: "POST",
        headers: { ...authHeaders, "Content-Type": "application/json" },
        body: JSON.stringify({ name, type }),
      });
      if (response.status === 409) {
        setError(t.projectNameTaken);
        return;
      }
      if (!response.ok) throw new Error();
      setOpen(false);
      setName("");
      setType("warehouse");
      setError("");
      void refresh();
    } catch {
      setError(t.projectError);
    }
  }

  async function putRole(member: MemberRow, role: ProjectRole) {
    if (!selected) return;
    try {
      const response = await fetch(
        `/api/v1/projects/${selected.id}/members/${member.id}`,
        {
          method: "PUT",
          headers: { ...authHeaders, "Content-Type": "application/json" },
          body: JSON.stringify({ role }),
        },
      );
      if (response.status === 409) {
        setMemberError(t.lastProjectAdmin);
        return;
      }
      if (!response.ok) throw new Error();
      await loadMembers(selected.id);
    } catch {
      setMemberError(t.memberError);
    }
  }

  async function addMember() {
    if (!selected || !addUserId) return;
    const user = users.find((item) => item.id === addUserId);
    if (!user) return;
    await putRole({ id: user.id, name: user.name, role: addRole }, addRole);
    setAddUserId(undefined);
    setAddRole("member");
  }

  async function removeMember() {
    if (!selected || confirmMember === null) return;
    try {
      const response = await fetch(
        `/api/v1/projects/${selected.id}/members/${confirmMember.id}`,
        { method: "DELETE", headers: authHeaders },
      );
      if (response.status === 409) {
        setMemberError(t.lastProjectAdmin);
        setConfirmMember(null);
        return;
      }
      if (!response.ok) throw new Error();
      setConfirmMember(null);
      await loadMembers(selected.id);
    } catch {
      setMemberError(t.memberError);
    }
  }

  async function errorDetail(response: Response, fallback: string) {
    try {
      const body = (await response.json()) as { detail?: string };
      return body.detail || fallback;
    } catch {
      return fallback;
    }
  }

  async function archiveProject() {
    if (!archiveTarget) return;
    setLifecycleBusy(true);
    try {
      const response = await fetch(
        `/api/v1/projects/${encodeURIComponent(archiveTarget.id)}/archive`,
        { method: "POST", headers: authHeaders },
      );
      if (!response.ok)
        throw new Error(await errorDetail(response, t.projectError));
      setArchiveTarget(null);
      setError("");
      await refresh();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : t.projectError);
    } finally {
      setLifecycleBusy(false);
    }
  }

  async function restoreProject(project: ProjectRow) {
    setLifecycleBusy(true);
    try {
      const response = await fetch(
        `/api/v1/projects/${encodeURIComponent(project.id)}/restore`,
        { method: "POST", headers: authHeaders },
      );
      if (!response.ok)
        throw new Error(await errorDetail(response, t.projectError));
      setError("");
      await refresh();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : t.projectError);
    } finally {
      setLifecycleBusy(false);
    }
  }

  async function deleteProject() {
    if (!deleteTarget || deleteName !== deleteTarget.name) return;
    setLifecycleBusy(true);
    try {
      const response = await fetch(
        `/api/v1/projects/${encodeURIComponent(deleteTarget.id)}/delete`,
        {
          method: "POST",
          headers: { ...authHeaders, "Content-Type": "application/json" },
          body: JSON.stringify({ name: deleteName }),
        },
      );
      if (!response.ok) {
        throw new Error(await errorDetail(response, t.deleteProjectError));
      }
      setDeleteTarget(null);
      setDeleteName("");
      setError("");
      await refresh();
    } catch (failure) {
      setError(
        failure instanceof Error ? failure.message : t.deleteProjectError,
      );
    } finally {
      setLifecycleBusy(false);
    }
  }

  if (selected) {
    const isAdmin = selected.my_role === "admin";
    const isActive = selected.status === "active";
    const canEditProject = isAdmin && isActive;
    const canEditAdapters = canEditProject;
    const adminCount = members.filter((item) => item.role === "admin").length;
    const lastAdmin = (member: MemberRow) =>
      member.role === "admin" && adminCount <= 1;
    const availableUsers = users.filter(
      (user) => !members.some((member) => member.id === user.id),
    );
    const activePullerPlugin = pullerPlugins.find(
      (plugin) => plugin.id === instPluginId,
    );
    const activeSecretKeys = secretKeysForPlugin(activePullerPlugin);
    const hasConfiguredSecretForModal =
      editingInstance !== null &&
      configuredSecretSet(editingInstance, activePullerPlugin).size > 0;
    const allExecutorsDisabled = executors.every((item) => !item.enabled);
    return (
      <section className="stack admin-page">
        <div className="page-header">
          <div>
            <h1 className="page-title">{selected.name}</h1>
            <p className="page-subtitle">{t.projectMembersHint}</p>
          </div>
          <div className="page-actions">
            {selected.type === "warehouse" &&
              selected.status === "active" &&
              (["yaml", "json"] as const).map((format) => (
                <Button
                  key={format}
                  autoInsertSpace={false}
                  loading={exportingFormat === format}
                  disabled={exportingFormat !== null}
                  onClick={() => {
                    setAdapterError("");
                    setExportingFormat(format);
                    void downloadOssieModel(token, selected.id, format)
                      .catch((err: unknown) => {
                        setAdapterError(
                          exportErrorMessage(err, t.exportOssieFailed),
                        );
                      })
                      .finally(() => setExportingFormat(null));
                  }}
                >
                  {format === "yaml" ? t.exportOssieYaml : t.exportOssieJson}
                </Button>
              ))}
            <Button
              autoInsertSpace={false}
              onClick={() => {
                projectAbortRef.current?.abort();
                projectLoadGen.current += 1;
                setSelected(null);
              }}
            >
              {t.backToProjects}
            </Button>
          </div>
        </div>
        {!isActive && (
          <Alert type="info" showIcon title={t.projectArchivedNotice} />
        )}
        {memberError && <Alert type="error" title={memberError} />}
        <div className="card section-card">
          <h2 className="card-title">{t.sourceAdapters}</h2>
          <p className="page-subtitle">{t.sourceAdaptersHint}</p>
          {adapterError && <Alert type="error" title={adapterError} />}
          <div className="stack">
            {adapters.map((adapter) => (
              <div
                key={adapter.id}
                className="adapter-row"
                data-testid={`adapter-${adapter.id}`}
              >
                <Space wrap>
                  <Checkbox
                    checked={adapter.enabled}
                    disabled={!canEditAdapters || !adapter.compatible}
                    onChange={(event) =>
                      updateAdapter(adapter.id, {
                        enabled: event.target.checked,
                      })
                    }
                  >
                    {adapter.name}
                  </Checkbox>
                  {adapter.project_types.map((type) => (
                    <Tag key={type}>{PROJECT_TYPE_LABELS[type] || type}</Tag>
                  ))}
                </Space>
                {adapter.reason && (
                  <p className="field-hint">{adapter.reason}</p>
                )}
                {adapter.enabled && adapter.compatible && (
                  <div className="stack">
                    <ParameterFields
                      schema={adapter.parameters_schema}
                      values={adapter.parameters}
                      disabled={!canEditAdapters}
                      onChange={(name, value) =>
                        updateAdapterParameter(adapter.id, name, value)
                      }
                    />
                  </div>
                )}
              </div>
            ))}
          </div>
          {isAdmin && (
            <Button
              autoInsertSpace={false}
              type="primary"
              disabled={!canEditAdapters}
              onClick={() => void saveAdapters()}
            >
              {t.adapterSave}
            </Button>
          )}
        </div>
        {selected.type === "warehouse" && (
          <div
            className="card section-card"
            data-testid="puller-instances-section"
          >
            <div className="card-title-row">
              <div>
                <h2 className="card-title">{t.pullerInstances}</h2>
                <p className="page-subtitle">{t.pullerInstancesHint}</p>
              </div>
              {isAdmin && (
                <Button
                  autoInsertSpace={false}
                  type="primary"
                  disabled={!canEditProject}
                  onClick={openCreateInstance}
                >
                  {t.addPullerInstance}
                </Button>
              )}
            </div>
            {pullError && <Alert type="error" title={pullError} />}
            <TableScroll label={t.pullerInstances}>
              <Table
                rowKey="id"
                dataSource={pullInstances}
                pagination={false}
                locale={{ emptyText: t.noPullerInstances }}
                columns={[
                  { title: t.pullInstanceId, dataIndex: "id" },
                  {
                    title: t.pullInstanceDisplayName,
                    dataIndex: "display_name",
                  },
                  {
                    title: t.pullerPlugin,
                    render: (_, row) => <Tag>{row.plugin_id}</Tag>,
                  },
                  {
                    title: t.enabled,
                    render: (_, row) => (
                      <Tag color={row.enabled ? "green" : "default"}>
                        {row.enabled ? t.enabled : t.disabled}
                      </Tag>
                    ),
                  },
                  {
                    title: t.pullScheduleCron,
                    render: (_, row) =>
                      `${row.schedule_cron || t.pullManualOnly} (${row.timezone})`,
                  },
                  {
                    title: `${t.pullCatalogPrefix} / ${t.pullTaskNamespace}`,
                    render: (_, row) =>
                      `${row.catalog_prefix || "—"} / ${row.task_namespace || "—"}`,
                  },
                  { title: t.pullDropThreshold, dataIndex: "drop_threshold" },
                  {
                    title: t.pullLastTableCount,
                    render: (_, row) => row.last_table_count ?? "—",
                  },
                  {
                    title: t.pullSecretConfigured,
                    render: (_, row) => (
                      <Tag color={row.secret_configured ? "green" : "default"}>
                        {row.secret_configured
                          ? t.pullSecretSaved
                          : t.pullSecretUnconfigured}
                      </Tag>
                    ),
                  },
                  ...(isAdmin
                    ? [
                        {
                          title: t.actions,
                          render: (_: unknown, row: PullInstance) => (
                            <Space wrap>
                              <Button
                                autoInsertSpace={false}
                                disabled={!canEditProject}
                                onClick={() => openEditInstance(row)}
                              >
                                {t.providerEdit}
                              </Button>
                              <Button
                                autoInsertSpace={false}
                                danger
                                disabled={!canEditProject}
                                onClick={() => setDeleteInstanceTarget(row)}
                              >
                                {t.deleteAction}
                              </Button>
                            </Space>
                          ),
                        },
                      ]
                    : []),
                ]}
              />
            </TableScroll>
          </div>
        )}
        {selected.type === "warehouse" && (
          <div className="card section-card" data-testid="executors-section">
            <h2 className="card-title">{t.queryExecutors}</h2>
            <p className="page-subtitle">{t.queryExecutorsHint}</p>
            {executorError && <Alert type="error" title={executorError} />}
            {executorNotice && <Alert type="success" title={executorNotice} />}
            {allExecutorsDisabled && (
              <Alert type="info" showIcon title={t.executorDisabledHint} />
            )}
            <div className="stack">
              {executors.map((executor) => (
                <div
                  key={executor.id}
                  className="adapter-row"
                  data-testid={`executor-${executor.id}`}
                >
                  <Space wrap>
                    <Checkbox
                      checked={executor.enabled}
                      disabled={!canEditProject || !executor.compatible}
                      onChange={(event) =>
                        toggleExecutorEnabled(executor.id, event.target.checked)
                      }
                    >
                      {executor.name}
                    </Checkbox>
                    <Tag>{executor.id}</Tag>
                  </Space>
                  {executor.reason && (
                    <p className="field-hint">{executor.reason}</p>
                  )}
                  {executor.enabled && executor.compatible && (
                    <div className="stack">
                      <ParameterFields
                        schema={executor.parameters_schema}
                        values={executor.parameters}
                        disabled={!canEditProject}
                        onChange={(name, value) =>
                          updateExecutorParameter(executor.id, name, value)
                        }
                      />
                    </div>
                  )}
                </div>
              ))}
            </div>
            {isAdmin && (
              <Button
                autoInsertSpace={false}
                type="primary"
                disabled={!canEditProject || executorBusy}
                loading={executorBusy}
                onClick={() => void saveExecutors()}
              >
                {t.executorSave}
              </Button>
            )}
          </div>
        )}
        {selected.my_role === "admin" && (
          <div className="card section-card">
            <h2 className="card-title">{t.evolutionPolicyTitle}</h2>
            <p className="page-subtitle">{t.evolutionPolicyHint}</p>
            {policyError && <Alert type="error" title={policyError} />}
            {policyNotice && <Alert type="success" title={policyNotice} />}
            <div className="stack">
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: 12,
                }}
              >
                <span id="policy-correction-label">
                  {t.correctionRequireReview}
                </span>
                <Switch
                  aria-labelledby="policy-correction-label"
                  aria-label={t.correctionRequireReview}
                  checked={policy.correction_require_review}
                  disabled={policyBusy || selected.status !== "active"}
                  onChange={(checked) =>
                    void togglePolicy("correction_require_review", checked)
                  }
                />
              </div>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: 12,
                }}
              >
                <span id="policy-evolution-label">
                  {t.evolutionRequireReview}
                </span>
                <Switch
                  aria-labelledby="policy-evolution-label"
                  aria-label={t.evolutionRequireReview}
                  checked={policy.evolution_require_review}
                  disabled={policyBusy || selected.status !== "active"}
                  onChange={(checked) =>
                    void togglePolicy("evolution_require_review", checked)
                  }
                />
              </div>
            </div>
          </div>
        )}
        {selected.my_role === "admin" && (
          <div className="card section-card">
            <h2 className="card-title">{t.projectMembers}</h2>
            <Space wrap className="member-add">
              <Select
                aria-label={t.selectUser}
                placeholder={t.selectUser}
                value={addUserId}
                onChange={setAddUserId}
                style={{ minWidth: 220 }}
                options={availableUsers.map((user) => ({
                  value: user.id,
                  label: `${user.name} (${user.role === "admin" ? t.roleAdmin : t.roleUser})`,
                }))}
              />
              <Select
                aria-label={t.selectProjectRole}
                value={addRole}
                onChange={setAddRole}
                options={[
                  { value: "member", label: t.projectMemberRole },
                  { value: "admin", label: t.projectAdminRole },
                ]}
              />
              <Button
                autoInsertSpace={false}
                type="primary"
                disabled={!addUserId}
                onClick={() => void addMember()}
              >
                {t.addMember}
              </Button>
            </Space>
            <TableScroll label={t.projectMembers}>
              <Table
                rowKey="id"
                dataSource={members}
                pagination={false}
                locale={{ emptyText: t.noMembers }}
                columns={[
                  { title: t.name, dataIndex: "name" },
                  {
                    title: t.role,
                    render: (_, member) => (
                      <Select
                        aria-label={`${t.selectProjectRole}: ${member.name}`}
                        value={member.role}
                        disabled={lastAdmin(member)}
                        title={
                          lastAdmin(member) ? t.lastProjectAdmin : undefined
                        }
                        onChange={(role) => void putRole(member, role)}
                        options={[
                          { value: "member", label: t.projectMemberRole },
                          { value: "admin", label: t.projectAdminRole },
                        ]}
                      />
                    ),
                  },
                  {
                    title: t.actions,
                    render: (_, member) => (
                      <Button
                        autoInsertSpace={false}
                        danger
                        disabled={lastAdmin(member)}
                        title={
                          lastAdmin(member) ? t.lastProjectAdmin : undefined
                        }
                        onClick={() => setConfirmMember(member)}
                      >
                        {t.removeMember}
                      </Button>
                    ),
                  },
                ]}
              />
            </TableScroll>
          </div>
        )}
        {confirmMember !== null && (
          <Modal
            open
            title={t.confirmRemoveMember}
            okText={t.confirmRemoveMember}
            cancelText={t.cancel}
            onOk={() => void removeMember()}
            onCancel={() => setConfirmMember(null)}
          >
            <p>{`${t.confirmRemoveMember}: ${confirmMember.name}`}</p>
          </Modal>
        )}
        {instanceModalOpen && (
          <Modal
            open
            title={editingInstance ? t.editPullerInstance : t.addPullerInstance}
            okText={t.save}
            cancelText={t.cancel}
            confirmLoading={instSaving}
            onOk={() => void savePullInstance()}
            onCancel={() => {
              setInstanceModalOpen(false);
              setEditingInstance(null);
            }}
          >
            <div className="stack">
              {instFormError && <Alert type="error" title={instFormError} />}
              <label className="field">
                <span className="field-label">{t.pullerPlugin}</span>
                <Select
                  aria-label={t.pullerPlugin}
                  disabled={editingInstance !== null || !canEditProject}
                  value={instPluginId}
                  onChange={(value) => {
                    setInstPluginId(value);
                    setInstParams({});
                  }}
                  options={pullerPlugins.map((plugin) => ({
                    value: plugin.id,
                    label: `${plugin.display_name} (${plugin.id})`,
                  }))}
                />
              </label>
              <label className="field">
                <span className="field-label">{t.pullInstanceId}</span>
                <Input
                  aria-label={t.pullInstanceId}
                  disabled={editingInstance !== null || !canEditProject}
                  value={instId}
                  onChange={(event) => setInstId(event.target.value)}
                />
                <p className="field-hint">{t.pullInstanceIdHint}</p>
              </label>
              <label className="field">
                <span className="field-label">{t.pullInstanceDisplayName}</span>
                <Input
                  aria-label={t.pullInstanceDisplayName}
                  disabled={!canEditProject}
                  value={instDisplayName}
                  onChange={(event) => setInstDisplayName(event.target.value)}
                />
              </label>
              <Checkbox
                checked={instEnabled}
                disabled={!canEditProject}
                onChange={(event) => setInstEnabled(event.target.checked)}
              >
                {t.enabled}
              </Checkbox>
              <label className="field">
                <span className="field-label">{t.pullScheduleCron}</span>
                <Input
                  aria-label={t.pullScheduleCron}
                  disabled={!canEditProject}
                  value={instScheduleCron}
                  placeholder="02:30"
                  onChange={(event) => setInstScheduleCron(event.target.value)}
                />
                <p className="field-hint">{t.pullScheduleCronHint}</p>
              </label>
              <label className="field">
                <span className="field-label">{t.pullTimezone}</span>
                <Input
                  aria-label={t.pullTimezone}
                  disabled={!canEditProject}
                  value={instTimezone}
                  onChange={(event) => setInstTimezone(event.target.value)}
                />
              </label>
              <label className="field">
                <span className="field-label">{t.pullCatalogPrefix}</span>
                <Input
                  aria-label={t.pullCatalogPrefix}
                  disabled={!canEditProject}
                  value={instCatalogPrefix}
                  onChange={(event) => setInstCatalogPrefix(event.target.value)}
                />
              </label>
              <label className="field">
                <span className="field-label">{t.pullTaskNamespace}</span>
                <Input
                  aria-label={t.pullTaskNamespace}
                  disabled={!canEditProject}
                  value={instTaskNamespace}
                  onChange={(event) => setInstTaskNamespace(event.target.value)}
                />
              </label>
              <label className="field">
                <span className="field-label">{t.pullDropThreshold}</span>
                <Input
                  aria-label={t.pullDropThreshold}
                  disabled={!canEditProject}
                  type="number"
                  step="0.05"
                  min="0.01"
                  max="1"
                  value={instDropThreshold}
                  onChange={(event) => setInstDropThreshold(event.target.value)}
                />
                <p className="field-hint">{t.pullDropThresholdHint}</p>
              </label>
              {activePullerPlugin && (
                <ParameterFields
                  schema={activePullerPlugin.parameters_schema}
                  values={instParams}
                  disabled={!canEditProject}
                  secretKeys={activeSecretKeys}
                  onChange={(name, value) =>
                    setInstParams((old) => ({ ...old, [name]: value }))
                  }
                />
              )}
              {hasConfiguredSecretForModal && (
                <p className="field-hint">{t.pullSecretMaskHint}</p>
              )}
            </div>
          </Modal>
        )}
        {deleteInstanceTarget !== null && (
          <Modal
            open
            title={t.deletePullerInstance}
            okText={t.deleteAction}
            cancelText={t.cancel}
            okButtonProps={{ danger: true }}
            confirmLoading={instSaving}
            onOk={() => void deletePullInstance()}
            onCancel={() => setDeleteInstanceTarget(null)}
          >
            <p>
              {t.confirmDeletePullerInstance.replace(
                "{name}",
                deleteInstanceTarget.display_name,
              )}
            </p>
          </Modal>
        )}
      </section>
    );
  }

  const visibleRows =
    canCreate || includeMemberProjects
      ? rows
      : rows.filter((row) => row.my_role === "admin");
  return (
    <section className="stack admin-page">
      <div className="page-header">
        <div>
          <h1 className="page-title">{t.projects}</h1>
          <p className="page-subtitle">{t.projectsHint}</p>
        </div>
        {canCreate && (
          <div className="page-actions">
            <Button
              autoInsertSpace={false}
              type="primary"
              onClick={() => {
                setOpen(true);
                setName("");
                setType("warehouse");
                setError("");
              }}
            >
              {t.createProject}
            </Button>
          </div>
        )}
      </div>
      {error && <Alert type="error" title={error} />}
      <div className="card section-card token-table-card">
        <TableScroll label={t.projects}>
          <Table
            rowKey="id"
            dataSource={visibleRows}
            pagination={false}
            onRow={(row) => ({
              onClick: () => void openProject(row),
              className:
                row.status === "archived" ? "project-archived-row" : undefined,
              style: { cursor: "pointer" },
            })}
            columns={[
              {
                title: t.name,
                render: (_, row) => (
                  <a
                    onClick={(event) => {
                      event.stopPropagation();
                      void openProject(row);
                    }}
                  >
                    {row.name}
                  </a>
                ),
              },
              {
                title: t.projectType,
                render: (_, row) =>
                  row.type === "document"
                    ? t.projectTypeDocument
                    : t.projectTypeWarehouse,
              },
              {
                title: t.role,
                render: (_, row) =>
                  row.my_role === "admin"
                    ? t.projectAdminRole
                    : t.projectMemberRole,
              },
              {
                title: t.status,
                render: (_, row) =>
                  row.status === "active" ? (
                    <Tag color="green">{t.active}</Tag>
                  ) : row.status === "deleting" ? (
                    <Tag color="red">{t.projectDeleting}</Tag>
                  ) : (
                    <Tag>{t.archived}</Tag>
                  ),
              },
              {
                title: t.created,
                render: (_, row) => localTime(row.created_at),
              },
              {
                title: t.actions,
                render: (_, row) => (
                  <Space
                    wrap
                    onClick={(event) => {
                      event.stopPropagation();
                    }}
                  >
                    <Button
                      autoInsertSpace={false}
                      onClick={() => {
                        void openProject(row);
                      }}
                    >
                      {t.projectMembers}
                    </Button>
                    {canCreate && row.status === "active" && (
                      <Button
                        autoInsertSpace={false}
                        onClick={() => {
                          setArchiveTarget(row);
                          setError("");
                        }}
                      >
                        {t.archiveProject}
                      </Button>
                    )}
                    {canCreate && row.status === "archived" && (
                      <>
                        <Button
                          autoInsertSpace={false}
                          disabled={lifecycleBusy}
                          onClick={() => void restoreProject(row)}
                        >
                          {t.restoreProject}
                        </Button>
                        <Button
                          autoInsertSpace={false}
                          danger
                          disabled={lifecycleBusy || row.id === "default"}
                          title={
                            row.id === "default"
                              ? t.defaultProjectUndeletable
                              : undefined
                          }
                          onClick={() => {
                            setDeleteName("");
                            setDeleteTarget(row);
                            setError("");
                          }}
                        >
                          {t.deleteProject}
                        </Button>
                      </>
                    )}
                  </Space>
                ),
              },
            ]}
          />
        </TableScroll>
      </div>
      {open && (
        <Modal
          open
          title={t.createProject}
          okText={t.create}
          cancelText={t.cancel}
          onOk={() => void create()}
          onCancel={() => setOpen(false)}
        >
          <div className="stack">
            <label className="field">
              <span className="field-label">{t.name}</span>
              <Input
                aria-label={t.name}
                value={name}
                maxLength={200}
                onChange={(event) => setName(event.target.value)}
              />
            </label>
            <label className="field">
              <span className="field-label">{t.projectType}</span>
              <Select
                aria-label={t.projectType}
                value={type}
                onChange={(value) => setType(value)}
                options={[
                  { value: "warehouse", label: t.projectTypeWarehouse },
                  { value: "document", label: t.projectTypeDocument },
                ]}
              />
              <p className="field-hint">{t.projectTypeHint}</p>
            </label>
          </div>
        </Modal>
      )}
      {archiveTarget !== null && (
        <Modal
          open
          title={t.archiveProject}
          okText={t.archiveProject}
          cancelText={t.cancel}
          confirmLoading={lifecycleBusy}
          onOk={() => void archiveProject()}
          onCancel={() => setArchiveTarget(null)}
        >
          <div className="stack">
            <p>{t.archiveConfirm.replace("{name}", archiveTarget.name)}</p>
            <p className="muted">{t.archiveProjectHint}</p>
          </div>
        </Modal>
      )}
      {deleteTarget !== null && (
        <Modal
          open
          title={t.confirmDeleteProject}
          okText={t.deleteProject}
          cancelText={t.cancel}
          okButtonProps={{
            danger: true,
            disabled: deleteName !== deleteTarget.name,
          }}
          confirmLoading={lifecycleBusy}
          onOk={() => void deleteProject()}
          onCancel={() => {
            setDeleteTarget(null);
            setDeleteName("");
          }}
        >
          <div className="stack">
            <p>{t.deleteProjectHint.replace("{name}", deleteTarget.name)}</p>
            <Input
              aria-label={t.name}
              value={deleteName}
              maxLength={200}
              onChange={(event) => setDeleteName(event.target.value)}
            />
            {deleteName !== "" && deleteName !== deleteTarget.name && (
              <p className="field-hint" role="alert">
                {t.deleteProjectMismatch}
              </p>
            )}
          </div>
        </Modal>
      )}
    </section>
  );
}
