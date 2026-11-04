import { useState } from "react";
import { Button, Card, Input, Space } from "antd";
import type { Messages } from "./i18n";

export function Login({
  messages,
  onLogin,
}: {
  messages: Messages;
  onLogin: (token: string) => void;
}) {
  const [input, setInput] = useState("");
  return (
    <Card>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          onLogin(input);
        }}
      >
        <Space>
          <Input.Password
            aria-label={messages.token}
            placeholder={messages.token}
            value={input}
            onChange={(e) => setInput(e.target.value)}
          />
          <Button htmlType="submit" type="primary">
            {messages.login}
          </Button>
        </Space>
      </form>
    </Card>
  );
}
