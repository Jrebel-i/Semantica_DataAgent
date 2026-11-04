// @vitest-environment jsdom
import { expect, test } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { DimensionStatus } from "./DimensionStatus";
import { getMessages } from "./i18n";

test("shows count and overflow status", () => {
  const messages = getMessages("zh");
  const view = render(
    <DimensionStatus count={3} tooMany={false} messages={messages} />,
  );
  expect(screen.getByText("已收录数量: 3")).toBeTruthy();
  view.rerender(<DimensionStatus count={7} tooMany messages={messages} />);
  expect(screen.getByText("值过多，未收录")).toBeTruthy();
  cleanup();
});
