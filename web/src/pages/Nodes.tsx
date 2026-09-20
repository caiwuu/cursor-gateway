import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Alert,
  App,
  Button,
  Card,
  Dropdown,
  Empty,
  Flex,
  Input,
  Segmented,
  Table,
  Tag,
  Typography,
  theme,
  Tooltip,
} from "antd";
import {
  MoreOutlined,
  PlusOutlined,
  SearchOutlined,
  PlayCircleOutlined,
  StopOutlined,
  SettingOutlined,
  MessageOutlined,
  StarOutlined,
  GlobalOutlined,
} from "@ant-design/icons";
import dayjs from "dayjs";
import { api } from "../api";
import { PageHeader } from "../components/PageHeader";
import { StatusBadge } from "../components/StatusBadge";
import { MODE_LABEL, enabledModesOf, type Mode, type NodeRecord } from "../types";

type Filter = "all" | "up" | "down";

export default function Nodes() {
  const nav = useNavigate();
  const { token } = theme.useToken();
  const { message } = App.useApp();
  const [nodes, setNodes] = useState<NodeRecord[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [q, setQ] = useState("");
  const [filter, setFilter] = useState<Filter>("all");

  const load = () =>
    api
      .nodes()
      .then((d) => setNodes(d.nodes))
      .catch((e) => setError(String(e.message || e)))
      .finally(() => setLoading(false));

  useEffect(() => {
    load();
  }, []);

  const rows = useMemo(() => {
    const query = q.trim().toLowerCase();
    return nodes.filter((n) => {
      if (filter === "up" && !n.runtime.listening) return false;
      if (filter === "down" && n.runtime.listening) return false;
      if (!query) return true;
      return `${n.name} ${n.slug} ${n.email} ${n.notes}`.toLowerCase().includes(query);
    });
  }, [nodes, q, filter]);

  async function toggle(node: NodeRecord) {
    setBusy(node.id);
    try {
      if (node.enabled) {
        await api.stopNode(node.id);
        message.success(`已停止节点: ${node.name}`);
      } else {
        await api.startNode(node.id);
        message.success(`已启动节点: ${node.name}`);
      }
      await load();
    } catch (e) {
      message.error(String((e as Error).message || e));
    } finally {
      setBusy("");
    }
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <PageHeader
        title="节点列表"
        desc="每个 Cursor 账号一个节点。创建时填 API Key，并按推理模式启用可用模型。"
        extra={
          <Button
            type="primary"
            icon={<PlusOutlined />}
            style={{
              background: "linear-gradient(135deg, #0d9488, #0f766e)",
              border: "none",
              boxShadow: "0 4px 12px rgba(13, 148, 136, 0.2)",
            }}
            onClick={() => nav("/nodes/new")}
          >
            新建节点
          </Button>
        }
      />
      
      {error && (
        <Alert
          type="error"
          showIcon
          closable
          message="加载节点失败"
          description={error}
          style={{ borderRadius: 10 }}
        />
      )}

      <Card
        bordered={false}
        className="premium-card"
        style={{ borderRadius: 12 }}
        styles={{ body: { padding: 0 } }}
      >
        <Flex
          justify="space-between"
          align="center"
          gap={16}
          wrap="wrap"
          style={{ padding: "16px 20px", borderBottom: `1px solid ${token.colorSplit}` }}
        >
          <Input
            allowClear
            prefix={<SearchOutlined style={{ color: token.colorTextSecondary }} />}
            placeholder="搜索节点名称、slug、邮箱或备注..."
            value={q}
            onChange={(e) => setQ(e.target.value)}
            style={{ width: 320, borderRadius: 8 }}
          />
          <Segmented<Filter>
            value={filter}
            onChange={setFilter}
            style={{ borderRadius: 8, padding: 3 }}
            options={[
              { label: "全部节点", value: "all" },
              { label: "运行中", value: "up" },
              { label: "已停用", value: "down" },
            ]}
          />
        </Flex>

        <Table
          rowKey="id"
          loading={loading}
          dataSource={rows}
          scroll={{ x: 920 }}
          pagination={nodes.length > 20 ? { pageSize: 20, showSizeChanger: false } : false}
          onRow={(row) => ({
            onClick: () => nav(`/nodes/${row.id}`),
            style: { cursor: "pointer" },
          })}
          locale={{
            emptyText: (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={nodes.length === 0 ? "暂无节点，开始创建你的第一个节点吧" : "没有匹配的节点"}
                style={{ padding: "40px 0" }}
              >
                {nodes.length === 0 && (
                  <Button
                    type="primary"
                    onClick={() => nav("/nodes/new")}
                    style={{ background: "#0d9488", border: "none", borderRadius: 8 }}
                  >
                    创建第一个节点
                  </Button>
                )}
              </Empty>
            ),
          }}
          columns={[
            {
              title: "名称 & 账号信息",
              dataIndex: "name",
              width: 300,
              render: (name: string, row: NodeRecord) => {
                return (
                  <div style={{ minWidth: 0 }}>
                    <Flex align="center" gap={6} wrap="wrap">
                      <Typography.Text strong style={{ fontSize: 15 }}>
                        {name}
                      </Typography.Text>
                      {row.is_default && (
                        <Tag
                          color="teal"
                          bordered={false}
                          style={{ fontSize: 11, padding: "0 6px", borderRadius: 4, fontWeight: 500 }}
                        >
                          默认
                        </Tag>
                      )}
                    </Flex>
                  </div>
                );
              },
            },
            {
              title: "运行状态",
              width: 120,
              render: (_: unknown, row: NodeRecord) => <StatusBadge node={row} />,
            },
            {
              title: "推理模式",
              width: 220,
              render: (_: unknown, row: NodeRecord) => {
                const active = enabledModesOf(row.mode_config);
                const colorOf = (v: Mode) =>
                  v === "bot" ? "orange" : v === "account" ? "processing" : v === "sand-direct" ? "purple" : "default";
                if (!active.length) {
                  return <Typography.Text type="secondary">未启用</Typography.Text>;
                }
                return (
                  <Flex gap={4} wrap="wrap">
                    {active.map((v) => (
                      <Tag
                        key={v}
                        color={colorOf(v)}
                        bordered={false}
                        style={{ textTransform: "capitalize", borderRadius: 4, fontWeight: 500, margin: 0 }}
                      >
                        {MODE_LABEL[v] || v}
                        {row.default_mode === v ? " · 默认" : ""}
                      </Tag>
                    ))}
                  </Flex>
                );
              },
            },
            {
              title: "监听地址",
              width: 160,
              render: (_: unknown, row: NodeRecord) => (
                <Flex align="center" gap={6}>
                  <GlobalOutlined style={{ color: token.colorTextDescription, fontSize: 13 }} />
                  <Typography.Text code className="code-monospace" style={{ fontSize: 13, padding: "2px 6px" }}>
                    {row.host}:{row.port}
                  </Typography.Text>
                </Flex>
              ),
            },
            {
              title: "最近活跃时间",
              width: 150,
              render: (_: unknown, row: NodeRecord) => (
                <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                  {row.last_used_at ? dayjs.unix(row.last_used_at).format("YYYY-MM-DD HH:mm") : "从未调用"}
                </Typography.Text>
              ),
            },
            {
              title: "操作",
              width: 140,
              render: (_: unknown, row: NodeRecord) => (
                <Flex
                  justify="flex-end"
                  gap={8}
                  onClick={(e) => e.stopPropagation()}
                >
                  <Tooltip title={row.enabled ? "停止后台服务" : "启动后台服务"}>
                    <Button
                      size="small"
                      type={row.enabled ? "default" : "primary"}
                      style={{
                        borderRadius: 6,
                        display: "inline-flex",
                        alignItems: "center",
                        background: !row.enabled ? "linear-gradient(135deg, #0d9488, #0f766e)" : undefined,
                        border: "none",
                        color: row.enabled ? undefined : "#fff",
                      }}
                      icon={row.enabled ? <StopOutlined /> : <PlayCircleOutlined />}
                      loading={busy === row.id}
                      onClick={() => toggle(row)}
                    >
                      {row.enabled ? "停止" : "启动"}
                    </Button>
                  </Tooltip>
                  <Dropdown
                    trigger={["click"]}
                    menu={{
                      items: [
                        { key: "edit", label: "节点配置", icon: <SettingOutlined /> },
                        { key: "play", label: "在线调试", icon: <MessageOutlined /> },
                        { type: "divider" },
                        { key: "default", label: "设为默认节点", icon: <StarOutlined />, disabled: row.is_default },
                      ],
                      onClick: async ({ key }) => {
                        if (key === "edit") nav(`/nodes/${row.id}`);
                        if (key === "play") nav(`/playground?node=${row.id}`);
                        if (key === "default") {
                          try {
                            await api.setDefault(row.id);
                            message.success(`已成功设置 ${row.name} 为默认节点`);
                            await load();
                          } catch (e) {
                            message.error(String((e as Error).message || e));
                          }
                        }
                      },
                    }}
                  >
                    <Button size="small" icon={<MoreOutlined />} style={{ borderRadius: 6 }} />
                  </Dropdown>
                </Flex>
              ),
            },
          ]}
        />
      </Card>
    </div>
  );
}

