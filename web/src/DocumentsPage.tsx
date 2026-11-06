import { useEffect, useState } from "react";
import { Alert, Button, Tree, Typography } from "antd";
import type { paths } from "./api";
import type { Messages } from "./i18n";

type Category =
  paths["/api/v1/categories"]["get"]["responses"][200]["content"]["application/json"][number];
type Document =
  paths["/api/v1/document"]["get"]["responses"][200]["content"]["application/json"];

export function DocumentsPage({
  token,
  messages,
}: {
  token: string;
  messages: Messages;
}) {
  const [categories, setCategories] = useState<Category[]>([]);
  const [category, setCategory] = useState<Category | null>(null);
  const [doc, setDoc] = useState<Document | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    fetch("/api/v1/categories", {
      headers: { Authorization: `Bearer ${token}` },
      signal: controller.signal,
    })
      .then((r) => {
        if (!r.ok) throw new Error();
        return r.json() as Promise<Category[]>;
      })
      .then(setCategories)
      .catch(() => {
        if (!controller.signal.aborted) setError(true);
      });
    return () => controller.abort();
  }, [token]);
  async function load(path: string) {
    try {
      const r = await fetch(path, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!r.ok) throw new Error();
      setDoc((await r.json()) as Document);
      setError(false);
    } catch {
      setError(true);
    }
  }
  return (
    <section>
      <Typography.Title level={2}>{messages.categories}</Typography.Title>
      {error && <Alert type="error" title={messages.error} />}
      <Tree
        key={categories.map((c) => c.ref).join("|")}
        defaultExpandAll
        treeData={categories.map((c) => ({
          key: c.ref,
          title: (
            <>
              <Button
                type="link"
                onClick={() => {
                  setCategory(c);
                  setDoc(null);
                  void load(
                    `/api/v1/category-index?${new URLSearchParams({ category: c.name })}`,
                  );
                }}
              >
                {c.name}
              </Button>{" "}
              {c.description}
            </>
          ),
          children: c.documents.map((d) => ({
            key: d.ref,
            isLeaf: true,
            title: (
              <Button
                type="link"
                onClick={() =>
                  void load(
                    `/api/v1/document?${new URLSearchParams({ doc_id: d.ref })}`,
                  )
                }
              >
                {d.path}
              </Button>
            ),
          })),
        }))}
      />
      {category && (
        <Typography.Title level={3}>
          {messages.documents}: {category.name}
        </Typography.Title>
      )}
      {doc && (
        <>
          <Typography.Title level={4}>
            {doc.path} {doc.generated && `(${messages.generated})`}
          </Typography.Title>
          <pre style={{ whiteSpace: "pre-wrap" }}>
            {doc.content
              .split("\n")
              .map((line, i) => `${doc.start_line + i}  ${line}`)
              .join("\n")}
          </pre>
        </>
      )}
    </section>
  );
}
