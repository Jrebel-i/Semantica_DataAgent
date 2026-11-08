import { useEffect, useState } from "react";
import { Select } from "antd";
import type { components } from "./api";
import type { Messages } from "./i18n";

type Project = components["schemas"]["ProjectView"];

export const PROJECT_STORAGE_KEY = "dataagent.project";

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
}: {
  token: string;
  value: string;
  onChange: (project: string) => void;
  messages: Messages;
}) {
  const [projects, setProjects] = useState<Project[]>([]);

  useEffect(() => {
    let active = true;
    void fetch("/api/v1/projects", {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then((response) => (response.ok ? response.json() : []))
      .then((rows: Project[]) => {
        if (active) setProjects(rows.filter((row) => row.my_role === "admin"));
      })
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, [token]);

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
        options={projects.map((project) => ({
          value: project.id,
          title: project.name,
          label: project.name,
        }))}
        style={{ minWidth: 180 }}
      />
    </label>
  );
}
