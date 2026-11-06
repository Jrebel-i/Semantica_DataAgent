import { Fragment, useState, type ReactNode } from "react";

const SQL_KEYWORDS = new Set(
  (
    "add all alter and any as asc between by case cast column count create " +
    "cross current date day delete desc distinct distinctrow drop else end " +
    "escape except exists explain false fetch first from full group having " +
    "hour if in inner insert intersect interval into is join key last left " +
    "like limit merge minute month natural not null nulls offset on or order " +
    "outer over partition percent rank replace right rollup row rows select " +
    "set some sum table then true truncate union unique update using values " +
    "when where window with year"
  ).split(" "),
);

const SQL_FUNCTIONS = new Set(
  (
    "avg coalesce concat count date_add date_sub dense_rank format_number " +
    "lag lead max min now nvl percent_rank row_number round row_number " +
    "substr substring sum to_date to_timestamp unix_timestamp"
  ).split(" "),
);

type InlineToken = {
  type: "text" | "code" | "strong" | "em" | "link";
  value: string;
  href?: string;
};

function inlineTokens(text: string): InlineToken[] {
  const tokens: InlineToken[] = [];
  let index = 0;
  const pushText = (value: string) => {
    if (value) tokens.push({ type: "text", value });
  };
  while (index < text.length) {
    const rest = text.slice(index);
    const code = /^`([^`]+)`/.exec(rest);
    if (code) {
      tokens.push({ type: "code", value: code[1] });
      index += code[0].length;
      continue;
    }
    const strong = /^\*\*([^*]+)\*\*/.exec(rest) || /^__([^_]+)__/.exec(rest);
    if (strong) {
      tokens.push({ type: "strong", value: strong[1] });
      index += strong[0].length;
      continue;
    }
    const em = /^\*([^*]+)\*/.exec(rest);
    if (em) {
      tokens.push({ type: "em", value: em[1] });
      index += em[0].length;
      continue;
    }
    const link = /^\[([^\]]+)\]\(([^)]+)\)/.exec(rest);
    if (link) {
      tokens.push({ type: "link", value: link[1], href: link[2] });
      index += link[0].length;
      continue;
    }
    const next = rest.slice(1).search(/[`*_\[]/);
    if (next < 0) {
      pushText(rest);
      break;
    }
    pushText(rest.slice(0, next + 1));
    index += next + 1;
  }
  return tokens;
}

function Inline({ text }: { text: string }) {
  return (
    <>
      {inlineTokens(text).map((token, index) => {
        if (token.type === "code")
          return <code key={index}>{token.value}</code>;
        if (token.type === "strong")
          return <strong key={index}>{token.value}</strong>;
        if (token.type === "em") return <em key={index}>{token.value}</em>;
        if (token.type === "link")
          return (
            <a key={index} href={token.href} target="_blank" rel="noreferrer">
              {token.value}
            </a>
          );
        return <Fragment key={index}>{token.value}</Fragment>;
      })}
    </>
  );
}

export function HighlightedSql({ sql }: { sql: string }) {
  const pattern =
    /(--[^\n]*|\/\*[\s\S]*?\*\/|'(?:''|[^'])*'|\b\d+(?:\.\d+)?\b|[A-Za-z_][A-Za-z0-9_]*)/g;
  const parts: ReactNode[] = [];
  let last = 0;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(sql))) {
    if (match.index > last) parts.push(sql.slice(last, match.index));
    const value = match[0];
    if (value.startsWith("--") || value.startsWith("/*")) {
      parts.push(
        <span className="sql-comment" key={`${match.index}-comment`}>
          {value}
        </span>,
      );
    } else if (value.startsWith("'")) {
      parts.push(
        <span className="sql-string" key={`${match.index}-string`}>
          {value}
        </span>,
      );
    } else if (/^\d/.test(value)) {
      parts.push(
        <span className="sql-number" key={`${match.index}-number`}>
          {value}
        </span>,
      );
    } else if (SQL_KEYWORDS.has(value.toLowerCase())) {
      parts.push(
        <span className="sql-keyword" key={`${match.index}-keyword`}>
          {value}
        </span>,
      );
    } else if (SQL_FUNCTIONS.has(value.toLowerCase())) {
      parts.push(
        <span className="sql-function" key={`${match.index}-function`}>
          {value}
        </span>,
      );
    } else {
      parts.push(value);
    }
    last = match.index + value.length;
  }
  if (last < sql.length) parts.push(sql.slice(last));
  return <>{parts}</>;
}

function CodeBlock({ code, language }: { code: string; language: string }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      /* Clipboard access may be denied on plain HTTP. */
    }
  }
  return (
    <div className="code-block">
      <div className="code-head">
        <span>{language || "code"}</span>
        <button type="button" onClick={() => void copy()}>
          {copied ? "已复制" : "复制"}
        </button>
      </div>
      <pre>
        <code>
          {language.toLowerCase() === "sql" ? (
            <HighlightedSql sql={code} />
          ) : (
            code
          )}
        </code>
      </pre>
    </div>
  );
}

function splitRow(line: string): string[] {
  let value = line.trim();
  if (value.startsWith("|")) value = value.slice(1);
  if (value.endsWith("|")) value = value.slice(0, -1);
  return value.split("|").map((cell) => cell.trim());
}

function isSeparator(line: string) {
  const cells = splitRow(line);
  return cells.length > 0 && cells.every((cell) => /^:?-{2,}:?$/.test(cell));
}

type Block =
  | { kind: "code"; lines: string[]; language: string }
  | { kind: "heading"; level: number; text: string }
  | { kind: "table"; header: string[]; align: string[]; rows: string[][] }
  | { kind: "list"; ordered: boolean; items: { text: string; depth: number }[] }
  | { kind: "quote"; lines: string[] }
  | { kind: "rule" }
  | { kind: "paragraph"; lines: string[] };

function blocks(text: string): Block[] {
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const result: Block[] = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }
    const fence = /^\s*```([\w+-]*)\s*$/.exec(line);
    if (fence) {
      const code: string[] = [];
      index += 1;
      while (index < lines.length && !/^\s*```\s*$/.test(lines[index])) {
        code.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      result.push({ kind: "code", lines: code, language: fence[1] });
      continue;
    }
    const heading = /^\s*(#{1,4})\s+(.*)$/.exec(line);
    if (heading) {
      result.push({
        kind: "heading",
        level: heading[1].length,
        text: heading[2],
      });
      index += 1;
      continue;
    }
    if (
      line.includes("|") &&
      index + 1 < lines.length &&
      isSeparator(lines[index + 1])
    ) {
      const header = splitRow(line);
      const align = splitRow(lines[index + 1]).map((cell) => {
        const left = cell.startsWith(":");
        const right = cell.endsWith(":");
        if (left && right) return "center";
        if (right) return "right";
        return "left";
      });
      const rows: string[][] = [];
      index += 2;
      while (
        index < lines.length &&
        lines[index].includes("|") &&
        lines[index].trim()
      ) {
        rows.push(splitRow(lines[index]));
        index += 1;
      }
      result.push({ kind: "table", header, align, rows });
      continue;
    }
    const list = /^(\s*)([-*+]|\d+\.)\s+(.*)$/.exec(line);
    if (list) {
      const ordered = /\d+\./.test(list[2]);
      const items: { text: string; depth: number }[] = [];
      while (index < lines.length) {
        const item = /^(\s*)([-*+]|\d+\.)\s+(.*)$/.exec(lines[index]);
        if (!item) break;
        items.push({
          text: item[3],
          depth: Math.min(
            3,
            Math.floor(item[1].replace(/\t/g, "  ").length / 2),
          ),
        });
        index += 1;
      }
      result.push({ kind: "list", ordered, items });
      continue;
    }
    if (/^\s*>\s?/.test(line)) {
      const quote: string[] = [];
      while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
        quote.push(lines[index].replace(/^\s*>\s?/, ""));
        index += 1;
      }
      result.push({ kind: "quote", lines: quote });
      continue;
    }
    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      result.push({ kind: "rule" });
      index += 1;
      continue;
    }
    const paragraph: string[] = [];
    while (index < lines.length) {
      const next = lines[index];
      if (
        !next.trim() ||
        /^\s*```/.test(next) ||
        /^\s*#{1,4}\s+/.test(next) ||
        /^\s*([-*+]|\d+\.)\s+/.test(next) ||
        /^\s*>/.test(next) ||
        (next.includes("|") &&
          index + 1 < lines.length &&
          isSeparator(lines[index + 1]))
      )
        break;
      paragraph.push(next);
      index += 1;
    }
    result.push({ kind: "paragraph", lines: paragraph });
  }
  return result;
}

function renderBlock(block: Block, index: number): ReactNode {
  if (block.kind === "code")
    return (
      <CodeBlock
        key={index}
        code={block.lines.join("\n")}
        language={block.language}
      />
    );
  if (block.kind === "heading") {
    const Tag = `h${block.level}` as "h1" | "h2" | "h3" | "h4";
    return (
      <Tag key={index}>
        <Inline text={block.text} />
      </Tag>
    );
  }
  if (block.kind === "table")
    return (
      <div className="md-table-wrap" key={index}>
        <table className="md-table">
          <thead>
            <tr>
              {block.header.map((cell, cellIndex) => (
                <th
                  key={cellIndex}
                  style={{
                    textAlign: (block.align[cellIndex] || "left") as "left",
                  }}
                >
                  <Inline text={cell} />
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {block.rows.map((row, rowIndex) => (
              <tr key={rowIndex}>
                {block.header.map((_, cellIndex) => (
                  <td
                    key={cellIndex}
                    style={{
                      textAlign: (block.align[cellIndex] || "left") as "left",
                    }}
                  >
                    <Inline text={row[cellIndex] ?? ""} />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  if (block.kind === "list") {
    const items = block.items.map((item, itemIndex) => (
      <li key={itemIndex} style={{ marginLeft: item.depth * 16 }}>
        <Inline text={item.text} />
      </li>
    ));
    return block.ordered ? (
      <ol key={index}>{items}</ol>
    ) : (
      <ul key={index}>{items}</ul>
    );
  }
  if (block.kind === "quote")
    return (
      <blockquote key={index} className="markdown-quote">
        <Inline text={block.lines.join(" ")} />
      </blockquote>
    );
  if (block.kind === "rule") return <hr className="md-rule" key={index} />;
  return (
    <p key={index}>
      {block.lines.map((line, lineIndex) => (
        <Fragment key={lineIndex}>
          {lineIndex > 0 && <br />}
          <Inline text={line} />
        </Fragment>
      ))}
    </p>
  );
}

export function Markdown({ text }: { text: string }) {
  return (
    <div className="markdown">
      {blocks(text || "").map((block, index) => renderBlock(block, index))}
    </div>
  );
}
