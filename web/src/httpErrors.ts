export async function readErrorDetail(
  response: Response,
  fallback: string,
): Promise<string> {
  try {
    const data = (await response.json()) as { detail?: unknown };
    if (typeof data.detail === "string" && data.detail.trim()) {
      return data.detail;
    }
  } catch {
    // Ignore non-JSON error responses
  }
  return fallback;
}
