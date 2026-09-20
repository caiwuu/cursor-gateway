import { useEffect, useState, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { Alert, Button, Card, Col, Empty, Flex, Row, Table, Tag, Tooltip, Typography, theme, Space, Switch } from "antd";
import {
  PlusOutlined,
  AppstoreOutlined,
  CheckCircleOutlined,
  SwapOutlined,
  FundOutlined,
  BugOutlined,
  SyncOutlined,
  ArrowRightOutlined,
  CodeOutlined,
} from "@ant-design/icons";
import dayjs from "dayjs";
import { api } from "../api";
import { PageHeader } from "../components/PageHeader";
import { CopyField } from "../components/CopyField";
import type { AppMeta, RequestLog, NodeRecord } from "../types";
import { useThemeMode } from "../theme";
import { formatCompact, formatExact } from "../format";

export default function Overview() {
  const nav = useNavigate();
  const { token } = theme.useToken();
  const { mode } = useThemeMode();
  const [meta, setMeta] = useState<AppMeta | null>(null);
  const [nodes, setNodes] = useState<NodeRecord[]>([]);
  const [logs, setLogs] = useState<RequestLog[]>([]);
  const [logTotal, setLogTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [logsLoading, setLogsLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const timerRef = useRef<any>(null);
  const queryRef = useRef({ page: 1, pageSize: 20 });
  queryRef.current = { page, pageSize };

  const fetchData = async (isManual = false, reason: "full" | "page" | "auto" = "full") => {
    const { page: p, pageSize: ps } = queryRef.current;
    if (isManual) setRefreshing(true);
    if (reason === "page") setLogsLoading(true);
    try {
      const [m, l, n] = await Promise.all([
        api.meta(),
        api.logs("", ps, (p - 1) * ps),
        api.nodes(),
      ]);
      setMeta(m);
      setLogs(l.logs);
      setLogTotal(Number(l.total || 0));
      setNodes(n.nodes);
      const total = Number(l.total || 0);
      const maxPage = Math.max(1, Math.ceil(total / ps) || 1);
      if (p > maxPage) setPage(maxPage);
    } catch (e) {
      setError(String((e as Error).message || e));
    } finally {
      setLoading(false);
      setRefreshing(false);
      setLogsLoading(false);
    }
  };

  useEffect(() => {
    fetchData(false, loading ? "full" : "page");
  }, [page, pageSize]);

  useEffect(() => {
    if (autoRefresh) {
      timerRef.current = setInterval(() => {
        fetchData(false, "auto");
      }, 5000);
    } else {
      if (timerRef.current) clearInterval(timerRef.current);
    }
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [autoRefresh]);

  const host = meta?.listen.host && meta.listen.host !== "0.0.0.0" ? meta.listen.host : window.location.hostname || "127.0.0.1";
  const port = meta?.listen.port ?? 8788;
  const unifiedUrl = `http://${host}:${port}`;
  const hasReadyNode = nodes.some((n) => n.enabled && (n.has_api_key || n.has_access_token));

  const stats = [
    {
      label: "总节点数",
      value: meta?.stats.nodes ?? 0,
      icon: <AppstoreOutlined style={{ fontSize: 18, color: "#0d9488" }} />,
      bgColor: "rgba(13, 148, 136, 0.08)",
      desc: "已注册的 Cursor 账号",
    },
    {
      label: "已启用节点",
      value: meta?.stats.enabled ?? 0,
      icon: <CheckCircleOutlined style={{ fontSize: 18, color: "#10b981" }} />,
      bgColor: "rgba(16, 185, 129, 0.08)",
      desc: "正在后台运行的节点",
    },
    {
      label: "24h 请求数",
      value: meta?.stats.requests_24h ?? 0,
      icon: <SwapOutlined style={{ fontSize: 18, color: "#3b82f6" }} />,
      bgColor: "rgba(59, 130, 246, 0.08)",
      desc: "过去 24 小时 API 呼叫",
    },
    {
      label: "24h Token",
      value: meta?.stats.total_tokens_24h ?? 0,
      icon: <FundOutlined style={{ fontSize: 18, color: "#8b5cf6" }} />,
      bgColor: "rgba(139, 92, 246, 0.08)",
      desc: `输入 ${formatCompact(meta?.stats.prompt_tokens_24h)} · 输出 ${formatCompact(meta?.stats.completion_tokens_24h)}`,
      exact: `${formatExact(meta?.stats.prompt_tokens_24h)} · ${formatExact(meta?.stats.completion_tokens_24h)}`,
    },
    {
      label: "24h 错误数",
      value: meta?.stats.errors_24h ?? 0,
      icon: <BugOutlined style={{ fontSize: 18, color: "#ef4444" }} />,
      bgColor: "rgba(239, 68, 68, 0.08)",
      danger: true,
      desc: "调用失败或网络故障数",
    },
  ];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>
      <PageHeader
        title="控制台概览"
        desc={
          meta ? (
            <Flex align="center" gap={8}>
              <span className="status-pulse-green" style={{ width: 6, height: 6 }} />
              <Typography.Text type="secondary">
                管理服务已启动 · 监听于 {meta.listen.host}:{meta.listen.port}
              </Typography.Text>
            </Flex>
          ) : (
            "读取中…"
          )
        }
        extra={
          <Button
            type="primary"
            icon={<PlusOutlined />}
            size="middle"
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
          message="服务异常"
          description={error}
          style={{ borderRadius: 10 }}
        />
      )}

      {meta && meta.stats.nodes === 0 && (
        <Alert
          type="info"
          showIcon
          style={{ borderRadius: 10 }}
          message="还没有节点"
          description="每个 Cursor 账号建为一个独立节点。只需填 API Key 和推理模式，名称和端口会自动生成。客户端用 /v1 即可在可用节点间负载均衡。"
          action={
            <Button
              type="primary"
              size="small"
              onClick={() => nav("/nodes/new")}
              style={{ background: "#0d9488", border: "none" }}
            >
              创建第一个
            </Button>
          }
        />
      )}

      {/* Stats Grid */}
      <div className="overview-stat-grid">
        {stats.map((s) => (
          <div key={s.label} style={{ minWidth: 0 }}>
            <Card
              loading={loading}
              bordered={false}
              className="premium-card"
              styles={{ body: { padding: "16px 20px" } }}
              style={{ borderRadius: 12, height: "100%" }}
            >
              <Flex vertical gap={12} justify="space-between" style={{ height: "100%" }}>
                <Flex justify="space-between" align="center" style={{ width: "100%" }}>
                  <Typography.Text type="secondary" style={{ fontSize: 13, fontWeight: 500 }}>
                    {s.label}
                  </Typography.Text>
                  <div
                    style={{
                      width: 36,
                      height: 36,
                      borderRadius: 8,
                      backgroundColor: s.bgColor,
                      display: "grid",
                      placeItems: "center",
                      flexShrink: 0,
                    }}
                  >
                    {s.icon}
                  </div>
                </Flex>
                
                <div>
                  <Typography.Title
                    level={2}
                    style={{
                      margin: 0,
                      fontSize: 28,
                      fontWeight: 700,
                      letterSpacing: "-0.03em",
                      lineHeight: 1.1,
                      color: s.danger && (s.value as number) > 0 ? token.colorError : token.colorText,
                    }}
                  >
                    <Tooltip title={formatExact(s.value as number)}>
                      <span>{formatCompact(s.value as number)}</span>
                    </Tooltip>
                  </Typography.Title>
                  <Typography.Text
                    type="secondary"
                    title={"exact" in s ? s.exact : undefined}
                    style={{ fontSize: 11, display: "block", marginTop: 6, lineHeight: 1.3 }}
                  >
                    {s.desc}
                  </Typography.Text>
                </div>
              </Flex>
            </Card>
          </div>
        ))}
      </div>

      {/* Unified Gateway entry */}
      {hasReadyNode && (
        <Card
          bordered={false}
          className="premium-card"
          style={{
            borderRadius: 12,
            background: mode === "dark" ? "linear-gradient(135deg, #18181b, #09090b)" : "linear-gradient(135deg, #ffffff, #f9fafb)",
          }}
          styles={{ body: { padding: "20px 24px" } }}
        >
          <Row gutter={[24, 16]} align="middle">
            <Col xs={24} md={10}>
              <Flex vertical gap={6}>
                <Flex align="center" gap={8}>
                  <CodeOutlined style={{ fontSize: 18, color: token.colorPrimary }} />
                  <Typography.Title level={5} style={{ margin: 0 }}>
                    负载均衡入口
                  </Typography.Title>
                </Flex>
                <Typography.Paragraph type="secondary" style={{ margin: 0, fontSize: 13 }}>
                  请求 <Tag color="teal" style={{ margin: 0 }}>/v1</Tag> 会分到当前启用且已配置凭据的节点。
                  发给别人请用「令牌」页的 <Typography.Text code>sk-…</Typography.Text>。指定账号用 <Typography.Text code>/n/&lt;slug&gt;/v1</Typography.Text>。
                </Typography.Paragraph>
              </Flex>
            </Col>
            <Col xs={24} md={14}>
              <Flex vertical gap={10}>
                <div>
                  <Typography.Text strong style={{ fontSize: 12, display: "block", marginBottom: 4 }}>
                    Base URL（不带 /v1）
                  </Typography.Text>
                  <CopyField value={unifiedUrl} />
                </div>
                <Flex gap={12} align="center" wrap="wrap">
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    示例: <Typography.Text code className="code-monospace">{unifiedUrl}/v1/chat/completions</Typography.Text>
                  </Typography.Text>
                  <Button
                    type="link"
                    size="small"
                    icon={<ArrowRightOutlined />}
                    onClick={() => nav("/playground")}
                    style={{ padding: 0 }}
                  >
                    去调试页测试
                  </Button>
                </Flex>
              </Flex>
            </Col>
          </Row>
        </Card>
      )}

      {/* Recent requests */}
      <Card
        title={
          <Flex align="center" gap={8}>
            <span style={{ fontWeight: 600 }}>最近请求日志</span>
            <Button
              type="text"
              size="small"
              onClick={() => nav("/usage")}
              style={{
                fontSize: 12,
                color: token.colorPrimary,
                background: token.colorPrimaryBg,
                borderRadius: 4,
                padding: "2px 8px",
                height: "auto",
              }}
            >
              用量账本
            </Button>
            {autoRefresh && <span className="status-pulse-green" style={{ width: 6, height: 6 }} />}
          </Flex>
        }
        bordered={false}
        className="premium-card overview-log-card"
        style={{ borderRadius: 12 }}
        styles={{ body: { padding: 0 } }}
        extra={
          <Space size={16}>
            <Flex align="center" gap={6}>
              <Typography.Text type="secondary" style={{ fontSize: 13 }}>自动刷新</Typography.Text>
              <Switch size="small" checked={autoRefresh} onChange={setAutoRefresh} />
            </Flex>
            <Button
              type="text"
              icon={<SyncOutlined spin={refreshing} />}
              disabled={refreshing}
              onClick={() => fetchData(true)}
              style={{ borderRadius: 6 }}
            >
              刷新
            </Button>
          </Space>
        }
      >
        <Table
          size="middle"
          rowKey="id"
          loading={loading || logsLoading}
          pagination={{
            current: page,
            pageSize,
            total: logTotal,
            showSizeChanger: true,
            showQuickJumper: true,
            pageSizeOptions: ["10", "20", "50", "100"],
            showTotal: (t, range) => `${range[0]}-${range[1]} / ${t} 条记录`,
            onChange: (p, ps) => {
              setPage(p);
              setPageSize(ps);
            },
            style: {
              padding: "16px 24px",
              margin: 0,
              borderTop: `1px solid ${token.colorSplit}`,
              display: "flex",
              justifyContent: "flex-end",
              alignItems: "center",
            },
          }}
          dataSource={logs}
          locale={{
            emptyText: (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description="暂无请求。使用 [调试] 页或向节点接口发起一次请求即可在此看到日志。"
                style={{ padding: "32px 0" }}
              >
                <Button type="primary" onClick={() => nav("/playground")} style={{ background: "linear-gradient(135deg, #0d9488, #0f766e)", border: "none", borderRadius: 8 }}>
                  前往调试
                </Button>
              </Empty>
            ),
          }}
          scroll={{ x: 1000 }}
          columns={[
            {
              title: "请求时间",
              dataIndex: "created_at",
              width: 140,
              render: (v: number) => (v ? dayjs.unix(v).format("MM-DD HH:mm:ss") : "—"),
            },
            {
              title: "节点",
              dataIndex: "node_name",
              ellipsis: true,
              render: (v: string, row) => (
                <Typography.Text strong onClick={() => nav(`/nodes/${row.node_id}`)} style={{ cursor: "pointer", color: token.colorPrimary }}>
                  {v}
                </Typography.Text>
              ),
            },
            {
              title: "模式",
              dataIndex: "mode",
              width: 110,
              render: (v: string) => {
                let color = "default";
                if (v === "bot") color = "orange";
                if (v === "account") color = "processing";
                if (v === "sand-direct") color = "purple";
                return <Tag color={color} bordered={false} style={{ textTransform: "capitalize", borderRadius: 4, fontWeight: 600 }}>{v}</Tag>;
              },
            },
            {
              title: "模型",
              dataIndex: "model",
              ellipsis: true,
              render: (v: string) => <Typography.Text className="code-monospace" style={{ fontSize: 13 }}>{v}</Typography.Text>,
            },
            {
              title: "令牌",
              dataIndex: "token_name",
              width: 120,
              ellipsis: true,
              render: (v: string) => v ? <Tag bordered={false} style={{ borderRadius: 4, fontWeight: 500 }}>{v}</Tag> : <Typography.Text type="secondary">—</Typography.Text>,
            },
            {
              title: "Token",
              width: 130,
              render: (_: unknown, row: RequestLog) => {
                const prompt = row.prompt_tokens || 0;
                const completion = row.completion_tokens || 0;
                const total = prompt + completion;
                if (!total) return <Typography.Text type="secondary">—</Typography.Text>;
                const text = `${formatCompact(prompt)}+${formatCompact(completion)}`;
                return (
                  <Tooltip title={`${formatExact(prompt)} (输入) + ${formatExact(completion)} (输出) = ${formatExact(total)} (总计)。估算用量，不含 Cursor 内部 prompt 扩充。`}>
                    <div style={{ display: "inline-flex", flexDirection: "column", gap: 2, cursor: "pointer" }}>
                      <span style={{ fontSize: 12, fontWeight: 500, fontFamily: "monospace" }}>{text}</span>
                      <span style={{ fontSize: 10, color: token.colorTextDescription }}>{formatCompact(total)} 总计</span>
                    </div>
                  </Tooltip>
                );
              },
            },
            {
              title: "状态",
              dataIndex: "status",
              width: 90,
              render: (v: number) => {
                let color = "default";
                if (v >= 500) color = "volcano";
                else if (v >= 400) color = "warning";
                else if (v >= 200) color = "success";
                return (
                  <Tag color={color} bordered={false} style={{ fontWeight: "bold", borderRadius: 4 }}>
                    {v || "—"}
                  </Tag>
                );
              },
            },
            {
              title: "错误",
              dataIndex: "error",
              ellipsis: true,
              render: (v: string) =>
                v ? (
                  <Typography.Text type="danger" ellipsis={{ tooltip: v }} style={{ fontSize: 12 }}>
                    {v}
                  </Typography.Text>
                ) : (
                  <Typography.Text type="secondary">—</Typography.Text>
                ),
            },
            {
              title: "耗时",
              dataIndex: "latency_ms",
              width: 100,
              render: (v: number) => {
                let color = token.colorTextSecondary;
                if (v < 300) color = "#10b981";
                else if (v < 1000) color = "#f59e0b";
                else color = "#ef4444";
                return (
                  <span style={{ color, fontWeight: 600, fontSize: 13 }}>
                    {v ? `${v}ms` : "—"}
                  </span>
                );
              },
            },
          ]}
        />
      </Card>
    </div>
  );
}
