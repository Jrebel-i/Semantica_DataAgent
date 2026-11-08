import { Button } from "antd";
import type { Messages } from "../i18n";
import { HighlightedSql } from "../Markdown";
import type { SqlBlock } from "./types";

export function SqlList({
  blocks,
  messages,
  copied,
  onCopy,
}: {
  blocks: SqlBlock[];
  messages: Messages;
  copied: string;
  onCopy: (sql: string) => void;
}) {
  if (blocks.length === 0) return null;
  return (
    <section aria-label={messages.sqlBlocks}>
      {blocks.map((block, index) => (
        <div className="sql-block" key={`${index}-${block.sql.slice(0, 40)}`}>
          <div className="sql-block-head">
            <span>{block.dialect || messages.sqlBlocks}</span>
            <Button type="text" onClick={() => onCopy(block.sql)}>
              {copied === block.sql ? messages.sqlCopied : messages.copySql}
            </Button>
          </div>
          <pre aria-label={messages.sqlBlocks}>
            <code>
              <HighlightedSql sql={block.sql} />
            </code>
          </pre>
          <span className="sr-only">{block.sql}</span>
        </div>
      ))}
    </section>
  );
}
