import type { QueryResult } from "./types";

export function ResultTable({
  result,
  limit,
}: {
  result: QueryResult;
  limit?: number;
}) {
  return (
    <div className="result-table-scroll">
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
