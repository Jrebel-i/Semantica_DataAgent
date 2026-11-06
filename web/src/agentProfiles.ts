export type AgentProfile = {
  providers: string[];
  default_provider: string;
  default_model: string;
};
export type AgentProfiles = Record<string, AgentProfile>;
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
