import { useEffect, useRef, useState } from "react";
import { Select } from "antd";
import type { components } from "./api";
import type { Messages } from "./i18n";

export type Project = components["schemas"]["ProjectView"];

export const PROJECT_STORAGE_KEY = "dataagent.project";
export const PROJECT_CHANGE_EVENT = "dataagent:project-change";

const pendingProjects = new Map<string, Promise<Project[]>>();

export function loadProjects(token: string): Promise<Project[]> {
  const pending = pendingProjects.get(token);
  if (pending) return pending;
  const request = fetch("/api/v1/projects", {
    headers: { Authorization: `Bearer ${token}` },
  })
    .then((response) => (response.ok ? response.json() : []))
    .then((rows: unknown) => (Array.isArray(rows) ? (rows as Project[]) : []))
    .catch(() => []);
  pendingProjects.set(token, request);
  void request.finally(() => {
    if (pendingProjects.get(token) === request) pendingProjects.delete(token);
  });
  return request;
}

export function useProjects(token: string) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    let active = true;
    setProjects([]);
    setLoaded(false);
    void loadProjects(token).then((rows) => {
      if (active) {
        setProjects(rows);
        setLoaded(true);
      }
    });
    return () => {
      active = false;
    };
  }, [token]);
  return { projects, loaded };
}

export function manageableProjects(projects: Project[]): Project[] {
  return projects.filter(
    (row) => row.my_role === "admin" && row.status === "active",
  );
}

export function storedProject(): string {
  try {
    return localStorage.getItem(PROJECT_STORAGE_KEY) || "default";
  } catch {
    return "default";
  }
}

export function rememberProject(project: string) {
  try {
    localStorage.setItem(PROJECT_STORAGE_KEY, project);
  } catch {
    // Private browsing may reject storage; the page still works for this visit.
  }
  try {
    window.dispatchEvent(
      new CustomEvent(PROJECT_CHANGE_EVENT, { detail: project }),
    );
  } catch {
    // Ignore when CustomEvent is unavailable.
  }
}

export function ProjectSelector({
  token,
  value,
  onChange,
  messages: t,
  includeMemberProjects = false,
  includeArchived = false,
  ariaLabel,
  compact = false,
}: {
  token: string;
  value: string;
  onChange: (project: string) => void;
  messages: Messages;
  includeMemberProjects?: boolean;
  includeArchived?: boolean;
  ariaLabel?: string;
  compact?: boolean;
}) {
  const { projects, loaded } = useProjects(token);
  const available = includeArchived
    ? includeMemberProjects
      ? projects.filter((row) => row.status !== "deleting")
      : projects.filter(
          (row) => row.my_role === "admin" && row.status !== "deleting",
        )
    : includeMemberProjects
      ? projects.filter((row) => row.status === "active")
      : manageableProjects(projects);

  const valueRef = useRef(value);
  valueRef.current = value;

  useEffect(() => {
    if (!loaded || available.length === 0) return;
    if (available.some((row) => row.id === value)) return;
    const next = available[0].id;
    valueRef.current = next;
    onChange(next);
    rememberProject(next);
  }, [available, loaded, onChange, value]);

  useEffect(() => {
    const onProjectChange = (event: Event) => {
      const next = (event as CustomEvent<string>).detail;
      if (!next || next === valueRef.current) return;
      if (available.some((row) => row.id === next)) {
        valueRef.current = next;
        onChange(next);
      }
    };
    window.addEventListener(PROJECT_CHANGE_EVENT, onProjectChange);
    return () => {
      window.removeEventListener(PROJECT_CHANGE_EVENT, onProjectChange);
    };
  }, [available, onChange]);

  const labelText = ariaLabel ?? t.projects;

  return (
    <label
      className={`project-selector${compact ? " project-selector--compact" : ""}`}
    >
      {!compact && <span className="muted">{labelText}</span>}
      <Select
        aria-label={labelText}
        value={value}
        onChange={(project) => {
          valueRef.current = project;
          onChange(project);
          rememberProject(project);
        }}
        options={available.map((project) => ({
          value: project.id,
          title: project.name,
          label:
            project.status === "archived"
              ? `${project.name} (${t.archived})`
              : project.name,
        }))}
        style={{ minWidth: compact ? 164 : 180 }}
      />
    </label>
  );
}
