import { useEffect, useMemo, useState } from "react";
import type { components } from "./api";
import { DiscoveryPanel } from "./DiscoveryPanel";
import type { Messages } from "./i18n";
import { ProjectSelector, storedProject, useProjects } from "./ProjectSelector";

type Provider = components["schemas"]["ProviderView"];

export function DiscoveryPage({
  token,
  messages: t,
  onNavigateCorrections,
  onNavigateEvaluations,
  onNavigatePersonalSettings,
}: {
  token: string;
  messages: Messages;
  onNavigateCorrections?: (proposalId?: string) => void;
  onNavigateEvaluations?: () => void;
  onNavigatePersonalSettings?: () => void;
}) {
  const [project, setProject] = useState(storedProject);
  const { projects } = useProjects(token);
  const currentProject = useMemo(
    () => projects.find((item) => item.id === project) ?? null,
    [projects, project],
  );
  const [providers, setProviders] = useState<Provider[]>([]);

  useEffect(() => {
    const controller = new AbortController();
    fetch("/api/v1/providers", {
      headers: { Authorization: `Bearer ${token}` },
      signal: controller.signal,
    })
      .then(async (res) => (res.ok ? ((await res.json()) as unknown) : []))
      .then((provData) => {
        if (!controller.signal.aborted && Array.isArray(provData)) {
          setProviders(provData as Provider[]);
        }
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, [token]);

  return (
    <section className="stack admin-page">
      <div className="page-header">
        <div>
          <h1 className="page-title">{t.discoveryTitle}</h1>
          <p className="page-subtitle">{t.discoveryHint}</p>
        </div>
        <ProjectSelector
          token={token}
          value={project}
          onChange={setProject}
          messages={t}
          includeArchived
        />
      </div>
      <DiscoveryPanel
        token={token}
        project={project}
        projectInfo={currentProject}
        providers={providers}
        messages={t}
        onNavigateCorrections={onNavigateCorrections}
        onNavigateEvaluations={onNavigateEvaluations}
        onNavigatePersonalSettings={onNavigatePersonalSettings}
      />
    </section>
  );
}
