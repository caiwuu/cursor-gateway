import { useEffect, useState } from "react";
import { Alert, App, Button, Card, Form, Input, Typography } from "antd";
import { userApi } from "../../api";
import { PageHeader } from "../../components/PageHeader";
import { formatYuan } from "../../format";
import type { GatewayUser } from "../../types";

export default function Profile() {
  const { message } = App.useApp();
  const [me, setMe] = useState<GatewayUser | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    userApi.me().then(setMe).catch((e) => setError(String((e as Error).message || e)));
  }, []);

  return (
    <div style={{ maxWidth: 560, margin: "0 auto", width: "100%", display: "flex", flexDirection: "column", gap: 20 }}>
      <PageHeader title="账户" desc="改密码、看账号信息。角色由站长在后台改。" />
      {error && <Alert type="error" showIcon closable message={error} />}
      <Card bordered={false} className="premium-card" style={{ borderRadius: 12 }}>
        <Typography.Paragraph style={{ marginBottom: 4 }}>
          用户名 <Typography.Text strong>{me?.username || "—"}</Typography.Text>
        </Typography.Paragraph>
        <Typography.Paragraph type="secondary" style={{ marginBottom: 4 }}>
          角色 {me?.role === "admin" ? "管理员" : "普通用户"} · 余额 {formatYuan(me?.balance || 0)}
        </Typography.Paragraph>
      </Card>
      <Card bordered={false} className="premium-card" style={{ borderRadius: 12 }} title="修改密码">
        <Form
          layout="vertical"
          onFinish={async (values: { old_password: string; password: string }) => {
            await userApi.changePassword(values.old_password, values.password);
            message.success("密码已更新");
          }}
        >
          <Form.Item name="old_password" label="当前密码" rules={[{ required: true, message: "请填写当前密码" }]}>
            <Input.Password autoComplete="current-password" />
          </Form.Item>
          <Form.Item name="password" label="新密码" rules={[{ required: true, min: 6, message: "至少 6 位" }]}>
            <Input.Password autoComplete="new-password" />
          </Form.Item>
          <Button type="primary" htmlType="submit" style={{ background: "#0d9488", border: "none" }}>
            保存
          </Button>
        </Form>
      </Card>
    </div>
  );
}
