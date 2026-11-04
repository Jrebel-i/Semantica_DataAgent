import type { Messages } from "./i18n";

export function DimensionStatus({
  count,
  tooMany,
  messages,
}: {
  count?: number | null;
  tooMany?: boolean | null;
  messages: Messages;
}) {
  if (tooMany) return <>{messages.dimTooMany}</>;
  if (count == null) return null;
  return (
    <>
      {messages.dimCount}: {count}
    </>
  );
}
