import type { Agent, EffortLevel, Provider } from "./types";

export const EFFORT_LEVELS: EffortLevel[] = [
  "low",
  "medium",
  "high",
  "extra",
  "max",
];

export function modelEffortLevels(
  provider: Provider | undefined,
  model: string,
): EffortLevel[] {
  const levels = provider?.model_capabilities?.[model]?.effort_levels;
  return Array.isArray(levels) ? levels : [];
}

export function allowedEffortLevels(
  agents: Agent[],
  agentId: string,
  provider: Provider | undefined,
  model: string,
): EffortLevel[] {
  const modelLevels = new Set(modelEffortLevels(provider, model));
  const agentLevels = new Set(
    agents.find((agent) => agent.id === agentId)?.effort_levels ?? [],
  );
  return EFFORT_LEVELS.filter(
    (level) => modelLevels.has(level) && agentLevels.has(level),
  );
}

export function defaultEffort(levels: EffortLevel[]): EffortLevel | "" {
  return levels.includes("medium") ? "medium" : "";
}
