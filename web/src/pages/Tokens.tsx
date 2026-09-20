import { useEffect, useMemo, useState } from "react";
import {
  Alert,
  App,
  Button,
  Card,
  Empty,
  Flex,
  Form,
  Input,
  Modal,
  Popconfirm,
  Switch,
  Table,
  Tag,
  Typography,
  theme,
} from "antd";
import { CopyOutlined, KeyOutlined, PlusOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { CopyField } from "../components/CopyField";
import { PageHeader } from "../components/PageHeader";
import type { GatewayToken } from "../types";
import { formatCompact, formatExact } from "../format";

function maskToken(value: string) {
  if (!value) return "";
  if (value.length <= 16) return value;
  return `${value.slice(0, 7)}…${value.slice(-4)}`;
}

export default function Tokens() {
  const { token } = theme.useToken();
  const { message } = App.useApp();
  const nav = useNavigate();
  const [items, setItems] = useState<GatewayToken[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<GatewayToken | null>(null);
  const [form] = Form.useForm<{ name: string }>();

  const origin = import.meta.env.DEV ? "http://127.0.0.1:8788" : window.location.origin;
  const baseUrl = origin;

  const load = () =>
    api
      .tokens()
      .then((d) => setItems(d.tokens))
      .catch((e) => setError(String((e as Error)?.message || e)))
      .finally(() => setLoading(false));

  useEffect(() => {
    load();
  }, []);

  async function create(values: { name: string }) {
    setError("");
    try {
      const rec = await api.createToken(values.name);
      form.resetFields();
      setCreating(false);
      setCreated(rec);
      await load();
      message.success("令牌已创建");
    } catch (e) {
      setError(String((e as Error)?.message || e));
    }
  }

  async function copyText(value: string) {
    await navigator.clipboard.writeText(value);
    message.success("已复制完整令牌");
  }

  const example = useMemo(
    () =>
      `curl ${baseUrl}/v1/chat/completions \\\n  -H "Authorization: Bearer ${created?.token || "sk-你的令牌"}" \\\n  -H "Content-Type: application/json" \\\n  -d '{"model":"claude-opus-5","messages":[{"role":"user","content":"你好"}]}'`,
    [baseUrl, created],
  );

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <PageHeader
        title="令牌"
        desc="站长也可以在这里发令牌。买家请走用户台注册、兑卡密、自己建令牌。"
        extra={
          <Flex gap={8}>
          <Button onClick={() => nav("/console")}>用户台</Button>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => setCreating(true)}
            style={{
              background: "linear-gradient(135deg, #0d9488, #0f766e)",
              border: "none",
              boxShadow: "0 4px 12px rgba(13, 148, 136, 0.2)",
            }}
          >
            新建令牌
          </Button>
          </Flex>
        }
      />

      {error && (
        <Alert type="error" showIcon closable message="令牌操作失败" description={error} style={{ borderRadius: 10 }} />
      )}

      <Card bordered={false} className="premium-card" style={{ borderRadius: 12 }}>
        <Flex align="center" gap={8} style={{ marginBottom: 12 }}>
          <KeyOutlined style={{ color: token.colorPrimary }} />
          <Typography.Title level={5} style={{ margin: 0 }}>
            接入信息（发给对方）
          </Typography.Title>
        </Flex>
        <Flex vertical gap={12}>
          <div>
            <Typography.Text type="secondary" style={{ fontSize: 12, display: "block", marginBottom: 4 }}>
              Base URL
            </Typography.Text>
            <CopyField value={baseUrl} />
          </div>
          <Typography.Paragraph type="secondary" style={{ margin: 0, fontSize: 13 }}>
            客户端填根地址即可，不要带 /v1，SDK 会自己拼。Authorization 用下方令牌。有令牌后，未带密钥的请求会被拒绝。
          </Typography.Paragraph>
        </Flex>
      </Card>

      <Card
        bordered={false}
        className="premium-card"
        style={{ borderRadius: 12 }}
        styles={{ body: { padding: 0 } }}
      >
        <Table
          rowKey="id"
          loading={loading}
          dataSource={items}
          pagination={items.length > 20 ? { pageSize: 20, showSizeChanger: false } : false}
          locale={{
            emptyText: (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有令牌，先发一张给调用方">
                <Button type="primary" onClick={() => setCreating(true)} style={{ background: "#0d9488", border: "none" }}>
                  新建第一张令牌
                </Button>
              </Empty>
            ),
          }}
          scroll={{ x: 1000 }}
          columns={[
            {
              title: "名称",
              dataIndex: "name",
              width: 140,
              render: (v: string) => <Typography.Text strong>{v}</Typography.Text>,
            },
            {
              title: "用户",
              dataIndex: "username",
              width: 120,
              render: (v: string) => v || <Typography.Text type="secondary">站长</Typography.Text>,
            },
            {
              title: "令牌",
              dataIndex: "token",
              width: 180,
              render: (v: string) => (
                <Flex align="center" gap={8} style={{ flexWrap: "nowrap" }}>
                  <Typography.Text className="code-monospace" style={{ fontSize: 13, whiteSpace: "nowrap" }}>
                    {maskToken(v)}
                  </Typography.Text>
                  <Button size="small" type="text" icon={<CopyOutlined />} onClick={() => copyText(v)} />
                </Flex>
              ),
            },
            {
              title: "状态",
              width: 100,
              render: (_: unknown, row: GatewayToken) => (
                <Switch
                  checked={row.enabled}
                  checkedChildren="启用"
                  unCheckedChildren="停用"
                  onChange={async (on) => {
                    try {
                      await api.updateToken(row.id, { enabled: on });
                      await load();
                    } catch (e) {
                      message.error(String((e as Error)?.message || e));
                    }
                  }}
                />
              ),
            },
            {
              title: "调用次数",
              dataIndex: "request_count",
              width: 100,
              render: (v: number) => <Typography.Text>{v || 0}</Typography.Text>,
            },
            {
              title: "输入 Token",
              dataIndex: "prompt_tokens",
              width: 110,
              render: (v: number) => (
                <Typography.Text title={formatExact(v)}>{formatCompact(v)}</Typography.Text>
              ),
            },
            {
              title: "输出 Token",
              dataIndex: "completion_tokens",
              width: 110,
              render: (v: number) => (
                <Typography.Text title={formatExact(v)}>{formatCompact(v)}</Typography.Text>
              ),
            },
            {
              title: "最近使用",
              width: 160,
              render: (_: unknown, row: GatewayToken) => (
                <Typography.Text type="secondary">
                  {row.last_used_at ? dayjs.unix(row.last_used_at).format("YYYY-MM-DD HH:mm") : "从未"}
                </Typography.Text>
              ),
            },
            {
              title: "创建时间",
              width: 160,
              render: (_: unknown, row: GatewayToken) => (
                <Typography.Text type="secondary">
                  {row.created_at ? dayjs.unix(row.created_at).format("YYYY-MM-DD HH:mm") : "—"}
                </Typography.Text>
              ),
            },
            {
              title: "操作",
              width: 90,
              fixed: "right",
              render: (_: unknown, row: GatewayToken) => (
                <Popconfirm title="删除后对方立刻无法调用。" onConfirm={async () => {
                  await api.deleteToken(row.id);
                  message.success("已删除");
                  await load();
                }}>
                  <Button size="small" danger type="link">
                    删除
                  </Button>
                </Popconfirm>
              ),
            },
          ]}
        />
      </Card>

      <Modal
        title="新建令牌"
        open={creating}
        onCancel={() => setCreating(false)}
        onOk={() => form.submit()}
        okText="创建"
        cancelText="取消"
      >
        <Form form={form} layout="vertical" onFinish={create} initialValues={{ name: "" }}>
          <Form.Item name="name" label="名称" extra="方便区分发给谁，例如「同事 A」或「测试」">
            <Input placeholder="例如：同事 A" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title="令牌已生成"
        open={!!created}
        onCancel={() => setCreated(null)}
        footer={[
          <Button key="copy" type="primary" icon={<CopyOutlined />} onClick={() => created && copyText(created.token)}>
            复制完整令牌
          </Button>,
        ]}
      >
        {created && (
          <Flex vertical gap={12}>
            <Alert type="success" showIcon message={`${created.name} 可以发给对方了`} />
            <Typography.Text className="code-monospace" copyable={{ text: created.token }} style={{ wordBreak: "break-all" }}>
              {created.token}
            </Typography.Text>
            <Tag color="teal" bordered={false}>
              发给对方：Base URL + 这串 sk- 令牌
            </Tag>
            <Typography.Paragraph type="secondary" style={{ margin: 0, whiteSpace: "pre-wrap", fontSize: 12 }} className="code-monospace">
              {example}
            </Typography.Paragraph>
          </Flex>
        )}
      </Modal>
    </div>
  );
}
