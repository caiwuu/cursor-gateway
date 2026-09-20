import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Alert, Card, Empty, Flex, Segmented, Table, Tabs, Tag, Typography } from "antd";
import dayjs from "dayjs";
import { setUserSession, userApi } from "../../api";
import { PageHeader } from "../../components/PageHeader";
import { formatCompact, formatExact, formatYuan } from "../../format";
import type { RequestLog, UsageBucket, UsageSummary } from "../../types";

function usageColumns() {
  return [
    { title: "请求", dataIndex: "requests", width: 80, render: (v: number) => formatCompact(v) },
    { title: "成功", dataIndex: "success", width: 80, render: (v: number) => formatCompact(v) },
    { title: "失败", dataIndex: "errors", width: 80, render: (v: number) => formatCompact(v) },
    { title: "输入", dataIndex: "prompt_tokens", render: (v: number) => <span title={formatExact(v)}>{formatCompact(v)}</span> },
    { title: "输出", dataIndex: "completion_tokens", render: (v: number) => <span title={formatExact(v)}>{formatCompact(v)}</span> },
    { title: "合计", dataIndex: "total_tokens", render: (v: number) => <Typography.Text strong>{formatCompact(v)}</Typography.Text> },
  ];
}

export default function Usage() {
  const nav = useNavigate();
  const [days, setDays] = useState(30);
  const [data, setData] = useState<(UsageSummary & { estimated_cost_yuan?: number }) | null>(null);
  const [logs, setLogs] = useState<RequestLog[]>([]);
  const [logTotal, setLogTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const pageSize = 20;

  useEffect(() => {
    setLoading(true);
    userApi
      .usage(days)
      .then(setData)
      .catch((e) => {
        const text = String((e as Error).message || e);
        if (text.includes("登录")) {
          setUserSession("");
          nav("/login", { replace: true });
          return;
        }
        setError(text);
      })
      .finally(() => setLoading(false));
  }, [days, nav]);

  useEffect(() => {
    userApi
      .logs(pageSize, (page - 1) * pageSize)
      .then((d) => {
        setLogs(d.logs);
        setLogTotal(d.total);
      })
      .catch(() => undefined);
  }, [page]);

  function bucketTable(items: UsageBucket[], empty: string) {
    return (
      <Table
        size="middle"
        rowKey={(r) => r.id || r.name}
        loading={loading}
        pagination={items.length > 20 ? { pageSize: 20 } : false}
        dataSource={items}
        locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={empty} /> }}
        columns={[{ title: "名称", dataIndex: "name", ellipsis: true }, ...usageColumns()]}
      />
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <PageHeader
        title="用量"
        desc="只统计你自己的令牌。费用按当前单价估算。"
        extra={
          <Segmented
            value={days}
            onChange={(v) => setDays(Number(v))}
            options={[
              { label: "7 天", value: 7 },
              { label: "30 天", value: 30 },
              { label: "90 天", value: 90 },
            ]}
          />
        }
      />
      {error && <Alert type="error" showIcon closable message={error} />}
      <Flex gap={16} wrap="wrap">
        {[
          { label: "请求", value: formatCompact(data?.totals.requests || 0) },
          { label: "Token", value: formatCompact(data?.totals.total_tokens || 0) },
          { label: "估算费用", value: `¥${(data?.estimated_cost_yuan || 0).toFixed(4)}` },
        ].map((c) => (
          <Card key={c.label} bordered={false} className="premium-card" style={{ flex: 1, minWidth: 180, borderRadius: 12 }}>
            <Typography.Text type="secondary">{c.label}</Typography.Text>
            <Typography.Title level={3} style={{ margin: "8px 0 0" }}>
              {c.value}
            </Typography.Title>
          </Card>
        ))}
      </Flex>
      <Card bordered={false} className="premium-card" style={{ borderRadius: 12 }} title="分项">
        <Tabs
          items={[
            { key: "model", label: "按模型", children: bucketTable(data?.by_model || [], "这段时间没有按模型的用量") },
            { key: "token", label: "按令牌", children: bucketTable(data?.by_token || [], "这段时间没有按令牌的用量") },
            {
              key: "day",
              label: "按日",
              children: (
                <Table
                  size="middle"
                  rowKey="name"
                  loading={loading}
                  pagination={false}
                  dataSource={data?.by_day || []}
                  columns={[{ title: "日期", dataIndex: "name", width: 140 }, ...usageColumns()]}
                />
              ),
            },
          ]}
        />
      </Card>
      <Card bordered={false} className="premium-card" style={{ borderRadius: 12 }} title="请求日志" styles={{ body: { padding: 0 } }}>
        <Table
          rowKey="id"
          dataSource={logs}
          pagination={{
            current: page,
            pageSize,
            total: logTotal,
            showSizeChanger: false,
            onChange: setPage,
          }}
          columns={[
            {
              title: "时间",
              width: 150,
              render: (_: unknown, row: RequestLog) =>
                row.created_at ? dayjs.unix(row.created_at).format("MM-DD HH:mm:ss") : "—",
            },
            { title: "模型", dataIndex: "model", ellipsis: true },
            { title: "令牌", dataIndex: "token_name", width: 110 },
            {
              title: "状态",
              width: 80,
              render: (_: unknown, row: RequestLog) => <Tag color={row.status < 400 ? "green" : "red"}>{row.status}</Tag>,
            },
            {
              title: "Token",
              width: 110,
              render: (_: unknown, row: RequestLog) => `${formatCompact(row.prompt_tokens)}+${formatCompact(row.completion_tokens)}`,
            },
            {
              title: "费用",
              width: 100,
              render: (_: unknown, row: RequestLog) => formatYuan(row.cost || 0),
            },
            { title: "耗时", width: 80, render: (_: unknown, row: RequestLog) => (row.latency_ms ? `${row.latency_ms}ms` : "—") },
            { title: "错误", dataIndex: "error", ellipsis: true },
          ]}
        />
      </Card>
    </div>
  );
}
