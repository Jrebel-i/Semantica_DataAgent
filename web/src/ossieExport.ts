export async function downloadOssieModel(
  token: string,
  projectId: string,
  format: "yaml" | "json" = "yaml",
): Promise<void> {
  const response = await fetch(
    `/api/v1/projects/${encodeURIComponent(projectId)}/export/ossie?format=${format}`,
    { headers: { Authorization: `Bearer ${token}` } },
  );
  if (!response.ok) {
    let detail = "";
    try {
      const payload = (await response.json()) as { detail?: string };
      if (typeof payload.detail === "string") detail = payload.detail;
    } catch {
      /* Body was not JSON; caller falls back to a generic error message. */
    }
    throw new Error(detail);
  }
  const blob = await response.blob();
  if (typeof URL.createObjectURL !== "function") return;
  const objectUrl = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = objectUrl;
  link.download = `${projectId}.ossie.${format}`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(objectUrl);
}
