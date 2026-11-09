import type { components } from "./api";
import { safeDecodePath } from "./paths";

export type TableSummary = components["schemas"]["TableSummary"];
export type TableDetail = components["schemas"]["TableView"];

const detailCache = new Map<string, Promise<TableDetail>>();

export function clearTableDetailCache() {
  detailCache.clear();
}

export function tableDetailUrl(
  database: string,
  name: string,
  project?: string,
): string {
  const url = `/api/v1/tables/${encodeURIComponent(database)}/${encodeURIComponent(name)}`;
  return project ? `${url}?project=${encodeURIComponent(project)}` : url;
}

export function loadTableDetail(
  token: string,
  database: string,
  name: string,
  project?: string,
): Promise<TableDetail> {
  const key = JSON.stringify([token, database, name, project ?? ""]);
  const cached = detailCache.get(key);
  if (cached) return cached;
  const request = fetch(tableDetailUrl(database, name, project), {
    headers: { Authorization: `Bearer ${token}` },
  }).then((response) => {
    if (!response.ok) throw new Error(`table detail: ${response.status}`);
    return response.json() as Promise<TableDetail>;
  });
  detailCache.set(key, request);
  void request.catch(() => {
    if (detailCache.get(key) === request) detailCache.delete(key);
  });
  return request;
}

export function summaryFromDetail(row: TableDetail): TableSummary {
  return {
    name: row.name,
    database: row.database,
    comment: row.comment,
    ref: row.ref,
    field_count: row.fields.length,
    document_count: row.documents.length,
    has_partition: row.fields.some((field) => field.partition),
  };
}

export function tablePartsFromRef(
  ref: string,
): { database: string; name: string } | null {
  if (!ref.startsWith("table:") && !ref.startsWith("field:")) return null;
  const parts = ref.slice(ref.indexOf(":") + 1).split(".");
  if (parts.length < 2) return null;
  return {
    database: safeDecodePath(parts[0]),
    name: safeDecodePath(
      ref.startsWith("field:") ? parts[1] : parts.slice(1).join("."),
    ),
  };
}
