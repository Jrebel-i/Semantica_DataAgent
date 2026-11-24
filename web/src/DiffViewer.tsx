import { Fragment, useMemo, useState } from "react";
import type { Messages } from "./i18n";
import { safeDecodePath } from "./paths";
import {
  parseUnifiedDiff,
  type DiffHunk,
  type InlineSegment,
} from "./diffParser";

function renderSegments(content: string, segments?: InlineSegment[]) {
  if (!segments || segments.length === 0) {
    return content || "\u00a0";
  }
  return segments.map((segment, index) => {
    if (segment.kind === "del") {
      return (
        <mark className="diff-mark-del" key={index}>
          {segment.text}
        </mark>
      );
    }
    if (segment.kind === "add") {
      return (
        <mark className="diff-mark-add" key={index}>
          {segment.text}
        </mark>
      );
    }
    return <Fragment key={index}>{segment.text}</Fragment>;
  });
}

function shouldShowHunkDivider(hunk: DiffHunk, index: number): boolean {
  return (
    hunk.explicitHeader &&
    (index > 0 ||
      hunk.oldStart > 1 ||
      hunk.newStart > 1 ||
      Boolean(hunk.section))
  );
}

function hunkDividerText(hunk: DiffHunk, t: Messages): string {
  const base = t.diffHunkLocation
    .replace("{old}", String(hunk.oldStart))
    .replace("{new}", String(hunk.newStart));
  return hunk.section ? `${base} · ${hunk.section}` : base;
}

export function DiffViewer({
  diff,
  messages: t,
  title,
  defaultMode = "split",
}: {
  diff: string;
  messages: Messages;
  title?: string;
  defaultMode?: "split" | "unified";
}) {
  const [mode, setMode] = useState<"split" | "unified">(defaultMode);
  const parsed = useMemo(() => parseUnifiedDiff(diff), [diff]);
  const displayPath = safeDecodePath(
    parsed.newFile || parsed.oldFile || title || "",
  );

  return (
    <div className="diff-viewer" data-diff-mode={mode}>
      <div className="diff-viewer-header">
        <div className="diff-viewer-meta">
          {displayPath && (
            <span className="diff-viewer-path" title={displayPath}>
              {displayPath}
            </span>
          )}
          <span className="diff-stat diff-stat-add">+{parsed.additions}</span>
          <span className="diff-stat diff-stat-del">-{parsed.deletions}</span>
        </div>
        <div
          className="diff-mode-toggle"
          role="group"
          aria-label={t.diffViewMode}
        >
          <button
            type="button"
            className={mode === "split" ? "active" : ""}
            aria-pressed={mode === "split"}
            onClick={() => setMode("split")}
          >
            {t.diffSplit}
          </button>
          <button
            type="button"
            className={mode === "unified" ? "active" : ""}
            aria-pressed={mode === "unified"}
            onClick={() => setMode("unified")}
          >
            {t.diffUnified}
          </button>
        </div>
      </div>

      <div className="diff-viewer-body">
        {parsed.hunks.length === 0 ? (
          diff.trim() ? (
            <pre className="diff-fallback">{diff}</pre>
          ) : (
            <div className="diff-viewer-empty">{t.diffEmpty}</div>
          )
        ) : mode === "split" ? (
          <table className="diff-table diff-table-split">
            <colgroup>
              <col className="diff-col-gutter" />
              <col className="diff-col-sign" />
              <col className="diff-col-code" />
              <col className="diff-col-gutter" />
              <col className="diff-col-sign" />
              <col className="diff-col-code" />
            </colgroup>
            <thead>
              <tr>
                <th colSpan={3} className="diff-side-head diff-side-before">
                  {t.diffBefore}
                </th>
                <th colSpan={3} className="diff-side-head diff-side-after">
                  {t.diffAfter}
                </th>
              </tr>
            </thead>
            <tbody>
              {parsed.hunks.map((hunk, hunkIdx) => (
                <Fragment key={`split-hunk-${hunkIdx}`}>
                  {shouldShowHunkDivider(hunk, hunkIdx) && (
                    <tr className="diff-hunk-row">
                      <td colSpan={6}>{hunkDividerText(hunk, t)}</td>
                    </tr>
                  )}
                  {hunk.splitRows.map((row, rowIdx) => (
                    <tr className="diff-row" key={`split-${hunkIdx}-${rowIdx}`}>
                      <td
                        className={`diff-gutter diff-gutter-${row.left.kind}`}
                      >
                        {row.left.line ?? ""}
                      </td>
                      <td
                        className={`diff-sign diff-sign-${row.left.kind}`}
                        aria-hidden="true"
                      >
                        {row.left.kind === "del" ? "−" : ""}
                      </td>
                      <td className={`diff-code diff-code-${row.left.kind}`}>
                        {renderSegments(row.left.content, row.left.segments)}
                      </td>
                      <td
                        className={`diff-gutter diff-gutter-${row.right.kind} diff-split-divider`}
                      >
                        {row.right.line ?? ""}
                      </td>
                      <td
                        className={`diff-sign diff-sign-${row.right.kind}`}
                        aria-hidden="true"
                      >
                        {row.right.kind === "add" ? "+" : ""}
                      </td>
                      <td className={`diff-code diff-code-${row.right.kind}`}>
                        {renderSegments(row.right.content, row.right.segments)}
                      </td>
                    </tr>
                  ))}
                </Fragment>
              ))}
            </tbody>
          </table>
        ) : (
          <table className="diff-table diff-table-unified">
            <colgroup>
              <col className="diff-col-gutter" />
              <col className="diff-col-gutter" />
              <col className="diff-col-sign" />
              <col />
            </colgroup>
            <thead>
              <tr>
                <th>{t.diffOldLine}</th>
                <th>{t.diffNewLine}</th>
                <th aria-hidden="true" />
                <th>{t.diffContent}</th>
              </tr>
            </thead>
            <tbody>
              {parsed.hunks.map((hunk, hunkIdx) => (
                <Fragment key={`unified-hunk-${hunkIdx}`}>
                  {shouldShowHunkDivider(hunk, hunkIdx) && (
                    <tr className="diff-hunk-row">
                      <td colSpan={4}>{hunkDividerText(hunk, t)}</td>
                    </tr>
                  )}
                  {hunk.lines.map((line, lineIdx) => (
                    <tr
                      className={`diff-row diff-row-${line.kind}`}
                      key={`unified-${hunkIdx}-${lineIdx}`}
                    >
                      <td className={`diff-gutter diff-gutter-${line.kind}`}>
                        {line.oldLine ?? ""}
                      </td>
                      <td className={`diff-gutter diff-gutter-${line.kind}`}>
                        {line.newLine ?? ""}
                      </td>
                      <td
                        className={`diff-sign diff-sign-${line.kind}`}
                        aria-hidden="true"
                      >
                        {line.kind === "del"
                          ? "−"
                          : line.kind === "add"
                            ? "+"
                            : ""}
                      </td>
                      <td className={`diff-code diff-code-${line.kind}`}>
                        {renderSegments(line.content, line.segments)}
                      </td>
                    </tr>
                  ))}
                </Fragment>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
