import type { QueryResult } from "./types";

export function ResultTable({
  result,
  limit,
  label,
}: {
  result: QueryResult;
  limit?: number;
  label: string;
}) {
  return (
    <div
      className="result-table-scroll"
      tabIndex={0}
      role="region"
      aria-label={label}
    >
      <table>
        <thead>
          <tr>
            {result.columns.map((column, index) => (
              <th key={index}>{column}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {result.rows.slice(0, limit).map((row, index) => (
            <tr key={index}>
              {row.map((value, column) => (
                <td key={column}>{value == null ? "" : String(value)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
