import { useEffect, useState } from "react";
import { Alert, Button, Input, Space, Typography } from "antd";

export function PersonalSettingsPage({ token }: { token: string }) {
  const [value, setValue] = useState("");
  const [configured, setConfigured] = useState(false);
  const [error, setError] = useState("");
  const headers = {
    Authorization: `Bearer ${token}`,
    "Content-Type": "application/json",
  };
  useEffect(() => {
    void fetch("/api/v1/me/dtb-token", {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then(async (r) => {
        if (!r.ok) throw new Error();
        setConfigured(((await r.json()) as { configured: boolean }).configured);
      })
      .catch(() => setError("读取设置失败"));
  }, [token]);
  async function save(next: string) {
    try {
      const response = await fetch("/api/v1/me/dtb-token", {
        method: "PUT",
        headers,
        body: JSON.stringify({ token: next }),
      });
      if (!response.ok) throw new Error();
      setConfigured(
        ((await response.json()) as { configured: boolean }).configured,
      );
      setValue("");
      setError("");
    } catch {
      setError("保存设置失败");
    }
  }
  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      <Typography.Title level={3}>个人设置</Typography.Title>
      <Typography.Text>
        DTB token：{configured ? "已设置" : "未设置"}
      </Typography.Text>
      {error && <Alert type="error" title={error} />}
      <Input.Password
        aria-label="DTB token"
        value={value}
        onChange={(event) => setValue(event.target.value)}
      />
      <Space>
        <Button
          type="primary"
          disabled={!value}
          onClick={() => void save(value)}
        >
          保存
        </Button>
        <Button disabled={!configured} onClick={() => void save("")}>
          删除
        </Button>
      </Space>
    </Space>
  );
}
