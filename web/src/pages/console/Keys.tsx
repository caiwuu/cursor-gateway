import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Alert, App, Button, Card, Flex, Form, Input, Modal, Popconfirm, Switch, Table, Typography } from "antd";
import { PlusOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import { setUserSession, userApi } from "../../api";
import { CopyField } from "../../components/CopyField";
import { PageHeader } from "../../components/PageHeader";
import { formatCompact, formatExact } from "../../format";
import type { GatewayToken } from "../../types";
import { USER_ORIGIN, maskToken } from "./shared";

export default function Keys() {
  const { message } = App.useApp();
  const nav = useNavigate();
  const [tokens, setTokens] = useState<GatewayToken[]>([]);
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<GatewayToken | null>(null);
  const [form] = Form.useForm<{ name: string }>();

  const load = () =>
    userApi
      .tokens()
      .then((d) => setTokens(d.tokens))
      .catch((e) => {
        const text = String((e as Error).message || e);
        if (text.includes("登录")) {
          setUserSession("");
          nav("/login", { replace: true });
          return;
        }
        setError(text);
      });

  useEffect(() => {
    load();
  }, []);

  const example = useMemo(
    () =>
      `curl ${USER_ORIGIN}/v1/chat/completions \\\n  -H "Authorization: Bearer ${created?.token || "sk-你的令牌"}" \\\n  -H "Content-Type: application/json" \\\n  -d '{"model":"grok-4.6","messages":[{"role":"user","content":"你好"}]}'`,
    [created],
  );

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <PageHeader
        title="令牌"
        desc="一把令牌对应一个客户端。余额扣在账号上，令牌只是钥匙。"
        extra={
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreating(true)} style={{ background: "#0d9488", border: "none" }}>
            新建令牌
          </Button>
        }
      />
      {error && <Alert type="error" showIcon closable message={error} />}
      <Card bordered={false} className="premium-card" style={{ borderRadius: 12 }}>
        <Typography.Text type="secondary" style={{ fontSize: 12, display: "block", marginBottom: 6 }}>
          Base URL（不要带 /v1）
        </Typography.Text>
        <CopyField value={USER_ORIGIN} />
      </Card>
      <Card bordered={false} className="premium-card" style={{ borderRadius: 12 }} styles={{ body: { padding: 0 } }}>
        <Table
          rowKey="id"
          dataSource={tokens}
          pagination={tokens.length > 20 ? { pageSize: 20 } : false}
          scroll={{ x: 880 }}
          locale={{ emptyText: "还没有令牌。先建一把，再拿去客户端用。" }}
          columns={[
            { title: "名称", dataIndex: "name", width: 140 },
            {
              title: "令牌",
              dataIndex: "token",
              render: (v: string) => (
                <Flex align="center" gap={6}>
                  <Typography.Text className="code-monospace">{maskToken(v)}</Typography.Text>
                  <Button size="small" type="link" onClick={() => navigator.clipboard.writeText(v).then(() => message.success("已复制"))}>
                    复制
                  </Button>
                </Flex>
              ),
            },
            {
              title: "状态",
              width: 90,
              render: (_: unknown, row: GatewayToken) => (
                <Switch
                  checked={row.enabled}
                  onChange={async (on) => {
                    await userApi.updateToken(row.id, { enabled: on });
                    await load();
                  }}
                />
              ),
            },
            {
              title: "调用",
              width: 80,
              dataIndex: "request_count",
              render: (v: number) => v || 0,
            },
            {
              title: "Token",
              width: 90,
              render: (_: unknown, row: GatewayToken) => (
                <Typography.Text title={formatExact((row.prompt_tokens || 0) + (row.completion_tokens || 0))}>
                  {formatCompact((row.prompt_tokens || 0) + (row.completion_tokens || 0))}
                </Typography.Text>
              ),
            },
            {
              title: "最近使用",
              width: 150,
              render: (_: unknown, row: GatewayToken) =>
                row.last_used_at ? dayjs.unix(row.last_used_at).format("MM-DD HH:mm") : "从未",
            },
            {
              title: "操作",
              width: 80,
              fixed: "right",
              render: (_: unknown, row: GatewayToken) => (
                <Popconfirm
                  title="删掉后这把钥匙立刻失效。"
                  onConfirm={async () => {
                    await userApi.deleteToken(row.id);
                    message.success("已删除");
                    await load();
                  }}
                >
                  <Button size="small" danger type="link">
                    删除
                  </Button>
                </Popconfirm>
              ),
            },
          ]}
        />
      </Card>

      <Modal title="新建令牌" open={creating} onCancel={() => setCreating(false)} onOk={() => form.submit()} okText="创建">
        <Form
          form={form}
          layout="vertical"
          onFinish={async (values) => {
            const rec = await userApi.createToken(values.name || "我的令牌");
            setCreated(rec);
            setCreating(false);
            form.resetFields();
            message.success("令牌已创建");
            await load();
          }}
        >
          <Form.Item name="name" label="名称" extra="例如：Cursor / 测试 / 同事 A">
            <Input placeholder="我的令牌" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal title="令牌已生成" open={!!created} onCancel={() => setCreated(null)} footer={null}>
        {created && (
          <Flex vertical gap={12}>
            <Alert type="success" showIcon message="完整密钥只在这里展示一次，先复制走。" />
            <Typography.Text className="code-monospace" copyable style={{ wordBreak: "break-all" }}>
              {created.token}
            </Typography.Text>
            <Typography.Paragraph type="secondary" className="code-monospace" style={{ whiteSpace: "pre-wrap", fontSize: 12, margin: 0 }}>
              {example}
            </Typography.Paragraph>
          </Flex>
        )}
      </Modal>
    </div>
  );
}
