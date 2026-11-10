import { type ReactNode, useEffect, useRef } from "react";

export function TableScroll({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const content =
      ref.current?.querySelector<HTMLElement>(".ant-table-content");
    if (!content) return;
    content.tabIndex = 0;
    content.setAttribute("role", "region");
    content.setAttribute("aria-label", label);
  });

  return (
    <div ref={ref} className="table-scroll-region">
      {children}
    </div>
  );
}
