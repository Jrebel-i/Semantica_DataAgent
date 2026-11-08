export async function copyText(value: string) {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value);
      return;
    } catch {
      // Plain HTTP deployments may deny the async clipboard API.
    }
  }
  const area = document.createElement("textarea");
  area.value = value;
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  try {
    if (!document.execCommand("copy")) throw new Error("Copy failed");
  } finally {
    area.remove();
  }
}

export function parseSseBlock(
  block: string,
): { event: string; data: unknown } | null {
  let event = "message";
  const data: string[] = [];
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  if (data.length === 0) return null;
  try {
    return { event, data: JSON.parse(data.join("\n")) };
  } catch {
    return null;
  }
}

export async function readSse(
  response: Response,
  onEvent: (event: string, data: Record<string, unknown>) => void,
) {
  if (!response.body) throw new Error("Streaming is not supported");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    buffer = buffer.replace(/\r\n/g, "\n");
    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      const parsed = parseSseBlock(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 2);
      if (parsed && typeof parsed.data === "object" && parsed.data !== null) {
        onEvent(parsed.event, parsed.data as Record<string, unknown>);
      }
      boundary = buffer.indexOf("\n\n");
    }
    if (done) break;
  }
  const parsed = parseSseBlock(buffer);
  if (parsed && typeof parsed.data === "object" && parsed.data !== null) {
    onEvent(parsed.event, parsed.data as Record<string, unknown>);
  }
}
