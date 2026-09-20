import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  Affix,
  Alert,
  App,
  Button,
  Card,
  Col,
  Descriptions,
  Form,
  Input,
  Popconfirm,
  Row,
  Select,
  Space,
  Switch,
  Typography,
  theme,
  Flex,
  Tag,
} from "antd";
import {
  KeyOutlined,
  ThunderboltOutlined,
  PlayCircleOutlined,
  StopOutlined,
  StarOutlined,
  MessageOutlined,
  DeleteOutlined,
  SaveOutlined,
  RollbackOutlined,
  EyeOutlined,
} from "@ant-design/icons";
import dayjs from "dayjs";
import { api } from "../api";
import { CopyField } from "../components/CopyField";
import { PageHeader } from "../components/PageHeader";
import { StatusBadge } from "../components/StatusBadge";
import {
  DEFAULT_MODEL_LIST,
  MODE_HINT,
  MODE_LABEL,
  MODES,
  defaultModeConfig,
  emptyNodeDraft,
  enabledModesOf,
  normalizeModeConfig,
  type Mode,
  type ModeConfig,
  type NodeRecord,
} from "../types";

function maskLong(text: string) {
  if (text.length <= 28) return text;
  if (text.startsWith("crsr_") && text.length > 14) return `${text.slice(0, 9)}…${text.slice(-4)}`;
  return `${text.slice(0, 8)}…${text.slice(-6)}`;
}

function InfoText({ value, mono = false, empty = "创建后自动生成" }: { value?: ReactNode; mono?: boolean; empty?: string }) {
  if (value === undefined || value === null || value === "") {
    return <Typography.Text type="secondary">{empty}</Typography.Text>;
  }
  const full = String(value);
  const shown = maskLong(full);
  return (
    <Typography.Text
      copyable={{ text: full, tooltips: ["复制完整内容", "已复制"] }}
      className={mono ? "code-monospace" : undefined}
      style={{ fontSize: 13, wordBreak: "break-all" }}
    >
      {shown}
    </Typography.Text>
  );
}

function timeText(ts?: number) {
  return ts ? dayjs.unix(ts).format("YYYY-MM-DD HH:mm:ss") : "";
}

type Draft = { api_key: string; default_mode: Mode; mode_config: ModeConfig };

export default function NodeEditor() {
  const { id } = useParams();
  const nav = useNavigate();
  const { message } = App.useApp();
  const { token } = theme.useToken();
  const creating = !id;
  const [form] = Form.useForm<Draft>();
  const [node, setNode] = useState<NodeRecord | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [modelList, setModelList] = useState<string[]>(DEFAULT_MODEL_LIST);
  const [modeDefaults, setModeDefaults] = useState<ModeConfig>(defaultModeConfig());

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const s = await api.settings().catch(() => null);
      const list = s?.model_list?.length ? s.model_list : DEFAULT_MODEL_LIST;
      const defaults = normalizeModeConfig(s?.mode_defaults, list);
      if (cancelled) return;
      setModelList(list);
      setModeDefaults(defaults);
      if (!id) {
        const on = enabledModesOf(defaults);
        form.setFieldsValue({
          ...emptyNodeDraft(),
          default_mode: on.includes("account") ? "account" : on[0] || "account",
          mode_config: defaults,
        });
        return;
      }
      const n = await api.node(id);
      if (cancelled) return;
      setNode(n);
      form.setFieldsValue({
        api_key: n.api_key,
        default_mode: n.default_mode,
        mode_config: normalizeModeConfig(n.mode_config, list, defaults),
      });
    })().catch((e) => {
      if (!cancelled) setError(String((e as Error)?.message || e));
    });
    return () => {
      cancelled = true;
    };
  }, [id, form]);

  const origin = import.meta.env.DEV ? "http://127.0.0.1:8788" : window.location.origin;
  const urls = useMemo(() => {
    const slug = node?.slug || "your-slug";
    const host = node?.host && node.host !== "0.0.0.0" ? node.host : "127.0.0.1";
    const port = node?.port;
    return {
      dedicated: port ? `http://${host}:${port}` : "",
      path: `${origin}/n/${slug}`,
      pool: origin,
    };
  }, [node, origin]);

  async function save(values: Draft) {
    setBusy("save");
    setError("");
    try {
      const body: Record<string, unknown> = {
        api_key: values.api_key,
        default_mode: values.default_mode,
        mode_config: normalizeModeConfig(values.mode_config, modelList, modeDefaults),
      };
      const saved = creating ? await api.createNode(body) : await api.updateNode(id!, body);
      message.success(creating ? `已创建节点 ${saved.name || saved.email || ""}` : "保存成功");
      if (creating) nav(`/nodes/${saved.id}`, { replace: true });
      else {
        setNode(saved);
        form.setFieldsValue({
          api_key: saved.api_key,
          default_mode: saved.default_mode,
          mode_config: normalizeModeConfig(saved.mode_config, modelList, modeDefaults),
        });
      }
    } catch (e) {
      setError(String((e as Error)?.message || e));
    } finally {
      setBusy("");
    }
  }

  async function run(action: "exchange" | "start" | "stop" | "default" | "delete") {
    if (!id) return;
    setBusy(action);
    setError("");
    try {
      if (action === "delete") {
        await api.deleteNode(id);
        message.success("已成功删除节点");
        nav("/nodes");
        return;
      }
      if (action === "exchange") {
        const res = await api.exchange(id);
        message.success(res.email ? `已换票成功: ${res.email}` : "已成功换取 Access Token");
        const n = await api.node(id);
        setNode(n);
        form.setFieldsValue({
          api_key: n.api_key,
          default_mode: n.default_mode,
          mode_config: normalizeModeConfig(n.mode_config, modelList, modeDefaults),
        });
      } else if (action === "start") {
        const n = await api.startNode(id);
        setNode(n);
        message.success("节点已启动运行");
      } else if (action === "stop") {
        const n = await api.stopNode(id);
        setNode(n);
        message.success("节点已停止运行");
      } else if (action === "default") {
        const n = await api.setDefault(id);
        setNode(n);
        message.success("已设为默认节点");
      }
    } catch (e) {
      setError(String((e as Error)?.message || e));
    } finally {
      setBusy("");
    }
  }

  const modeConfig = Form.useWatch("mode_config", form) as ModeConfig | undefined;
  const enabledModes = enabledModesOf(modeConfig);
  const title = creating ? "新建节点" : node?.name || node?.email || "配置节点";

  function setModeEnabled(mode: Mode, on: boolean) {
    const next = normalizeModeConfig(form.getFieldValue("mode_config"), modelList, modeDefaults);
    next[mode] = {
      enabled: on,
      models: on && !next[mode].models.length ? [...(modeDefaults[mode]?.models || modelList)] : next[mode].models,
    };
    const stillOn = enabledModesOf(next);
    const currentDefault = form.getFieldValue("default_mode") as Mode;
    form.setFieldsValue({
      mode_config: next,
      default_mode: stillOn.includes(currentDefault) ? currentDefault : stillOn[0] || "account",
    });
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <PageHeader
        breadcrumb={[{ title: "节点列表", href: "/nodes" }, { title: creating ? "新建" : title }]}
        title={title}
        desc="填写 API Key，并按推理模式启用/配置可用模型。名称、端口、机器码等会自动生成，可在下方查看但不能改。"
      />
      {error && (
        <Alert
          type="error"
          showIcon
          closable
          message="配置错误"
          description={error}
          style={{ marginBottom: 12, borderRadius: 10 }}
        />
      )}

      <Row gutter={[24, 24]}>
        <Col xs={24} xl={16}>
          <Form form={form} layout="vertical" requiredMark={false} initialValues={emptyNodeDraft()} onFinish={save}>
            <Card bordered={false} className="premium-card" style={{ borderRadius: 12 }}>
              <Flex align="center" gap={8} style={{ marginBottom: 4 }}>
                <KeyOutlined style={{ fontSize: 16, color: token.colorPrimary }} />
                <Typography.Title level={5} style={{ margin: 0, fontWeight: 600 }}>
                  账号与推理
                </Typography.Title>
              </Flex>
              <Typography.Paragraph type="secondary" style={{ margin: "4px 0 16px", fontSize: 13 }}>
                保存时会立刻用 API Key 换票。成功后节点名称就是该账号邮箱。
              </Typography.Paragraph>

              <Form.Item
                name="api_key"
                label="API Key"
                rules={creating ? [{ required: true, message: "请填写 API Key" }] : []}
              >
                <Input placeholder="crsr_…" autoComplete="off" spellCheck={false} className="code-monospace" style={{ borderRadius: 6 }} />
              </Form.Item>
              <Form.Item name="default_mode" label="默认推理模式">
                <Select
                  style={{ borderRadius: 6 }}
                  options={enabledModes.map((value) => ({
                    value,
                    label: MODE_LABEL[value],
                  }))}
                />
              </Form.Item>
              <Typography.Text type="secondary" style={{ fontSize: 13, display: "block", marginBottom: 10 }}>
                按模式启用，并勾选该模式对外提供的模型。未启用的模式不会被 /v1 或对应路径调用。
              </Typography.Text>
              <Flex vertical gap={12} style={{ marginBottom: 8 }}>
                {MODES.map((mode) => {
                  const on = !!modeConfig?.[mode]?.enabled;
                  return (
                    <div
                      key={mode}
                      style={{
                        padding: "12px 14px",
                        borderRadius: 8,
                        border: `1px solid ${token.colorSplit}`,
                        background: on ? token.colorBgContainer : token.colorBgLayout,
                        opacity: on ? 1 : 0.78,
                      }}
                    >
                      <Flex align="center" justify="space-between" gap={12} wrap="wrap">
                        <div>
                          <Typography.Text strong>{MODE_LABEL[mode]}</Typography.Text>
                          <Typography.Paragraph type="secondary" style={{ margin: "2px 0 0", fontSize: 12 }}>
                            {MODE_HINT[mode]}
                          </Typography.Paragraph>
                        </div>
                        <Form.Item name={["mode_config", mode, "enabled"]} valuePropName="checked" noStyle>
                          <Switch checkedChildren="启用" unCheckedChildren="停用" onChange={(v) => setModeEnabled(mode, v)} />
                        </Form.Item>
                      </Flex>
                      <Form.Item
                        name={["mode_config", mode, "models"]}
                        style={{ margin: "10px 0 0" }}
                        rules={on ? [{ required: true, type: "array", min: 1, message: "请至少选择一个模型" }] : []}
                      >
                        <Select
                          mode="multiple"
                          allowClear
                          showSearch
                          disabled={!on}
                          placeholder={on ? "选择该模式可用的模型" : "先启用该模式"}
                          options={modelList.map((id) => ({ value: id, label: id }))}
                          listHeight={320}
                          style={{ width: "100%" }}
                        />
                      </Form.Item>
                    </div>
                  );
                })}
              </Flex>
              {!creating && (
                <Button
                  loading={busy === "exchange"}
                  icon={<ThunderboltOutlined />}
                  onClick={() => run("exchange")}
                  style={{ borderRadius: 6, fontWeight: 500 }}
                >
                  重新用 API Key 换票
                </Button>
              )}
            </Card>
          </Form>

            <Card
              bordered={false}
              className="premium-card"
              style={{ borderRadius: 12, marginTop: 16 }}
              title={
                <Flex align="center" gap={8}>
                  <EyeOutlined style={{ color: token.colorPrimary }} />
                  <span style={{ fontWeight: 600 }}>自动生成信息</span>
                  <Tag bordered={false} style={{ margin: 0, borderRadius: 4 }}>
                    只读
                  </Tag>
                </Flex>
              }
            >
              <Typography.Paragraph type="secondary" style={{ marginTop: -4, marginBottom: 16, fontSize: 13 }}>
                这些字段保存时自动写入，这里只能查看和复制。
              </Typography.Paragraph>
              <Descriptions
                size="small"
                column={1}
                colon
                styles={{ label: { width: 140, color: token.colorTextSecondary } }}
                items={[
                  { label: "节点名称", children: <InfoText value={node?.name} /> },
                  { label: "账号邮箱", children: <InfoText value={node?.email} /> },
                  { label: "路径 slug", children: <InfoText value={node?.slug} mono /> },
                  { label: "监听地址", children: <InfoText value={node ? `${node.host}:${node.port}` : ""} mono /> },
                  { label: "Access Token", children: <InfoText value={node?.access_token} mono empty={node ? "尚未换票" : "创建后自动生成"} /> },
                  { label: "Refresh Token", children: <InfoText value={node?.refresh_token} mono empty={node ? "尚未换票" : "创建后自动生成"} /> },
                  { label: "machine_id", children: <InfoText value={node?.machine_id} mono /> },
                  { label: "mac_machine_id", children: <InfoText value={node?.mac_machine_id} mono /> },
                  { label: "账号 sub", children: <InfoText value={node?.sub} mono /> },
                  { label: "fingerprint", children: <InfoText value={node?.fingerprint} mono /> },
                  { label: "backend", children: <InfoText value={node?.backend} mono /> },
                  { label: "agent_host", children: <InfoText value={node?.agent_host} mono /> },
                  { label: "客户端类型", children: <InfoText value={node?.account_client_type} /> },
                  { label: "客户端版本", children: <InfoText value={node?.account_client_version} mono /> },
                  { label: "工作区", children: <InfoText value={node?.account_workspace} mono /> },
                  { label: "创建时间", children: <InfoText value={timeText(node?.created_at)} empty="—" /> },
                  { label: "更新时间", children: <InfoText value={timeText(node?.updated_at)} empty="—" /> },
                  { label: "最近调用", children: <InfoText value={timeText(node?.last_used_at)} empty="尚未调用" /> },
                ]}
              />
            </Card>
        </Col>

        <Col xs={24} xl={8}>
          <div style={{ position: "sticky", top: 80, display: "flex", flexDirection: "column", gap: 16 }}>
            {node && (
              <Card
                title={<span style={{ fontWeight: 600 }}>当前运行状态</span>}
                bordered={false}
                className="premium-card"
                style={{ borderRadius: 12 }}
                styles={{ body: { padding: "16px 20px" } }}
              >
                <Space direction="vertical" style={{ width: "100%" }} size={14}>
                  <Flex align="center" justify="space-between">
                    <StatusBadge node={node} />
                    {node.is_default && (
                      <Tag color="teal" bordered={false} style={{ borderRadius: 4, fontWeight: 500, margin: 0 }}>
                        默认节点
                      </Tag>
                    )}
                  </Flex>

                  <div style={{ background: token.colorBgLayout, padding: "10px 14px", borderRadius: 8 }}>
                    <Typography.Text type="secondary" style={{ fontSize: 12, display: "block" }}>
                      节点名称（账号邮箱）
                    </Typography.Text>
                    <Typography.Text strong style={{ fontSize: 14 }}>
                      {node.name || node.email || "（换票后自动填入）"}
                    </Typography.Text>
                  </div>

                  <div style={{ background: token.colorBgLayout, padding: "10px 14px", borderRadius: 8 }}>
                    <Typography.Text type="secondary" style={{ fontSize: 12, display: "block" }}>
                      自动分配端口
                    </Typography.Text>
                    <Typography.Text strong className="code-monospace" style={{ fontSize: 14 }}>
                      {node.host}:{node.port}
                    </Typography.Text>
                  </div>

                  {node.runtime.listen_error && (
                    <Alert type="warning" showIcon message="端口监听错误" description={node.runtime.listen_error} style={{ borderRadius: 8 }} />
                  )}

                  <Flex vertical gap={8} style={{ width: "100%" }}>
                    <Button
                      type={node.enabled ? "default" : "primary"}
                      icon={node.enabled ? <StopOutlined /> : <PlayCircleOutlined />}
                      loading={!!busy}
                      onClick={() => run(node.enabled ? "stop" : "start")}
                      block
                      style={{
                        borderRadius: 8,
                        height: 36,
                        background: !node.enabled ? "linear-gradient(135deg, #0d9488, #0f766e)" : undefined,
                        border: "none",
                        color: node.enabled ? undefined : "#fff",
                        fontWeight: 600,
                        display: "inline-flex",
                        alignItems: "center",
                        justifyContent: "center",
                      }}
                    >
                      {node.enabled ? "停止运行" : "启动服务"}
                    </Button>
                    <Flex gap={8} style={{ width: "100%" }}>
                      <Button
                        disabled={node.is_default}
                        icon={<StarOutlined />}
                        loading={!!busy}
                        onClick={() => run("default")}
                        style={{
                          borderRadius: 8,
                          flex: 1,
                          height: 36,
                          display: "inline-flex",
                          alignItems: "center",
                          justifyContent: "center",
                        }}
                      >
                        设为默认
                      </Button>
                      <Button
                        icon={<MessageOutlined />}
                        onClick={() => nav(`/playground?node=${node.id}`)}
                        style={{
                          borderRadius: 8,
                          flex: 1,
                          height: 36,
                          display: "inline-flex",
                          alignItems: "center",
                          justifyContent: "center",
                        }}
                      >
                        调试
                      </Button>
                    </Flex>
                  </Flex>
                </Space>
              </Card>
            )}

            <Card
              title={<span style={{ fontWeight: 600 }}>节点接入地址</span>}
              bordered={false}
              className="premium-card"
              style={{ borderRadius: 12 }}
              styles={{ body: { padding: "16px 20px" } }}
            >
              <Space direction="vertical" size={14} style={{ width: "100%" }}>
                <div>
                  <Typography.Text type="secondary" style={{ fontSize: 12, display: "block", marginBottom: 4 }}>
                    负载均衡入口（推荐）
                  </Typography.Text>
                  <CopyField value={urls.pool} compact />
                </div>
                {urls.dedicated && (
                  <div>
                    <Typography.Text type="secondary" style={{ fontSize: 12, display: "block", marginBottom: 4 }}>
                      本节点独立端口
                    </Typography.Text>
                    <CopyField value={urls.dedicated} compact />
                  </div>
                )}
                <div>
                  <Typography.Text type="secondary" style={{ fontSize: 12, display: "block", marginBottom: 4 }}>
                    本节点固定路径
                  </Typography.Text>
                  <CopyField value={urls.path} compact />
                </div>
              </Space>
            </Card>
          </div>
        </Col>
      </Row>

      <Affix offsetBottom={0}>
        <div
          style={{
            marginTop: 24,
            padding: "16px 24px",
            background: token.colorBgLayout,
            borderTop: `1px solid ${token.colorSplit}`,
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            gap: 12,
            boxShadow: "0 -4px 12px rgba(0, 0, 0, 0.03)",
          }}
        >
          <div>
            {!creating && (
              <Popconfirm title="确定删除这个节点？此操作不可恢复。" onConfirm={() => run("delete")} okText="确定" cancelText="取消">
                <Button danger icon={<DeleteOutlined />} style={{ borderRadius: 8, fontWeight: 500 }}>
                  删除节点
                </Button>
              </Popconfirm>
            )}
          </div>
          <Space size={12}>
            <Button icon={<RollbackOutlined />} onClick={() => nav("/nodes")} style={{ borderRadius: 8, fontWeight: 500 }}>
              取消
            </Button>
            <Button
              type="primary"
              icon={<SaveOutlined />}
              loading={busy === "save"}
              onClick={() => form.submit()}
              style={{
                borderRadius: 8,
                background: "linear-gradient(135deg, #0d9488, #0f766e)",
                border: "none",
                fontWeight: 600,
                boxShadow: "0 4px 12px rgba(13, 148, 136, 0.15)",
              }}
            >
              {creating ? "创建节点" : "保存"}
            </Button>
          </Space>
        </div>
      </Affix>
    </div>
  );
}
