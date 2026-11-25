export type AgentProfile = {
  providers: string[];
  default_provider: string;
  default_model: string;
};
export type AgentProfiles = Record<string, AgentProfile>;
export type ModelProtocol = "Claude" | "Codex" | "OpenAI" | "unknown";

export function modelProtocol(modelId: string): ModelProtocol {
  const idx = modelId.lastIndexOf("#");
  if (idx === -1) return "unknown";
  const suffix = modelId
    .slice(idx + 1)
    .trim()
    .toLowerCase();
  if (suffix === "claude") return "Claude";
  if (suffix === "codex") return "Codex";
  if (suffix === "oai") return "OpenAI";
  return "unknown";
}

export function parseAgentProfiles(
  value: string | null | undefined,
): AgentProfiles {
  if (!value) return {};
  const parsed: unknown = JSON.parse(value);
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed))
    throw new Error("Invalid agent profiles");
  for (const profile of Object.values(parsed)) {
    if (
      typeof profile !== "object" ||
      profile === null ||
      !Array.isArray(profile.providers) ||
      !profile.providers.every((item: unknown) => typeof item === "string") ||
      typeof profile.default_provider !== "string" ||
      typeof profile.default_model !== "string"
    )
      throw new Error("Invalid agent profile");
  }
  return parsed as AgentProfiles;
}
