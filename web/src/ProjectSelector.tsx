import { useEffect, useState } from "react";
import { Select } from "antd";
import type { components } from "./api";
import type { Messages } from "./i18n";

export type Project = components["schemas"]["ProjectView"];

export const PROJECT_STORAGE_KEY = "dataagent.project";

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
}

export function ProjectSelector({
  token,
  value,
  onChange,
  messages: t,
  includeMemberProjects = false,
}: {
  token: string;
  value: string;
  onChange: (project: string) => void;
  messages: Messages;
  includeMemberProjects?: boolean;
}) {
  const { projects, loaded } = useProjects(token);
  const available = includeMemberProjects
    ? projects.filter((row) => row.status === "active")
    : manageableProjects(projects);

  useEffect(() => {
    if (!loaded || available.length === 0) return;
    if (available.some((row) => row.id === value)) return;
    const next = available[0].id;
    rememberProject(next);
    onChange(next);
  }, [available, loaded, onChange, value]);

  return (
    <label className="project-selector">
      <span className="muted">{t.projects}</span>
      <Select
        aria-label={t.projects}
        value={value}
        onChange={(project) => {
          rememberProject(project);
          onChange(project);
        }}
        options={available.map((project) => ({
          value: project.id,
          title: project.name,
          label: project.name,
        }))}
        style={{ minWidth: 180 }}
      />
    </label>
  );
}
