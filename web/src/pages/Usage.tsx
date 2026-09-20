import { useEffect, useState } from "react";
import { Alert, Button, Card, Col, Empty, Flex, Row, Segmented, Table, Tabs, Tooltip, Typography, theme } from "antd";
import { DownloadOutlined, FundOutlined } from "@ant-design/icons";
import { api } from "../api";
import { PageHeader } from "../components/PageHeader";
import { formatCompact, formatExact } from "../format";
import type { UsageBucket, UsageSummary } from "../types";

function formatCount(n: number) {
  return formatCompact(n);
}

function csvEscape(value: string | number) {
  const text = String(value ?? "");
  if (/[",\n]/.test(text)) return `"${text.replace(/"/g, '""')}"`;
  return text;
}

function downloadCsv(name: string, header: string[], rows: Array<Array<string | number>>) {
  const body = [header, ...rows].map((line) => line.map(csvEscape).join(",")).join("\n");
  const blob = new Blob(["\uFEFF" + body], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

function usageColumns() {
  return [
    { title: "请求", dataIndex: "requests", width: 90, render: (v: number) => <span title={formatExact(v)}>{formatCount(v)}</span> },
    { title: "成功", dataIndex: "success", width: 90, render: (v: number) => <span title={formatExact(v)}>{formatCount(v)}</span> },
    { title: "失败", dataIndex: "errors", width: 90, render: (v: number) => <span title={formatExact(v)}>{formatCount(v)}</span> },
    { title: "输入 Token", dataIndex: "prompt_tokens", render: (v: number) => <span title={formatExact(v)}>{formatCount(v)}</span> },
    { title: "输出 Token", dataIndex: "completion_tokens", render: (v: number) => <span title={formatExact(v)}>{formatCount(v)}</span> },
    {
      title: "合计",
      dataIndex: "total_tokens",
      render: (v: number) => (
        <Typography.Text strong title={formatExact(v)}>
          {formatCount(v)}
        </Typography.Text>
      ),
    },
  ];
}

export default function Usage() {
  const { token } = theme.useToken();
  const [days, setDays] = useState(30);
  const [data, setData] = useState<UsageSummary | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    api
      .usage(days)
      .then(setData)
      .catch((e) => setError(String((e as Error).message || e)))
      .finally(() => setLoading(false));
  }, [days]);

  const totals = data?.totals;
  const cards = [
    { label: "请求次数", value: totals?.requests ?? 0, desc: "含失败，按调用记一次" },
    { label: "成功次数", value: totals?.success ?? 0, desc: "HTTP 2xx / 3xx" },
    { label: "输入 Token", value: totals?.prompt_tokens ?? 0, desc: "prompt / input" },
    { label: "输出 Token", value: totals?.completion_tokens ?? 0, desc: "completion / output" },
  ];

  function exportCsv() {
    if (!data) return;
    downloadCsv(
      `usage-${data.from}-${data.to}.csv`,
      ["日期", "令牌", "节点", "模型", "请求", "成功", "失败", "输入Token", "输出Token", "合计Token"],
      data.rows.map((r) => [
        r.day,
        r.token_name,
        r.node_name,
        r.model,
        r.requests,
        r.success,
        r.errors,
        r.prompt_tokens,
        r.completion_tokens,
        r.total_tokens,
      ]),
    );
  }

  function bucketTable(items: UsageBucket[], empty: string) {
    return (
      <Table
        size="middle"
        rowKey={(r) => r.id || r.name}
        loading={loading}
        pagination={items.length > 20 ? { pageSize: 20, showSizeChanger: false } : false}
        dataSource={items}
        locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={empty} /> }}
        columns={[{ title: "名称", dataIndex: "name", ellipsis: true }, ...usageColumns()]}
      />
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>
      <PageHeader
        title="用量"
        desc="按日汇总输入/输出 Token，给后续按令牌或模型计费用。不含单价，导出后自行乘价格。"
        extra={
          <Flex gap={8} wrap="wrap">
            <Segmented
              value={days}
              onChange={(v) => setDays(Number(v))}
              options={[
                { label: "7 天", value: 7 },
                { label: "30 天", value: 30 },
                { label: "90 天", value: 90 },
              ]}
            />
            <Button icon={<DownloadOutlined />} onClick={exportCsv} disabled={!data?.rows.length}>
              导出 CSV
            </Button>
          </Flex>
        }
      />

      {error && (
        <Alert type="error" showIcon closable message="用量读取失败" description={error} style={{ borderRadius: 10 }} />
      )}

      <Row gutter={[16, 16]}>
        {cards.map((s) => (
          <Col xs={12} md={6} key={s.label}>
            <Card bordered={false} className="premium-card" styles={{ body: { padding: "20px 24px" } }} style={{ borderRadius: 12 }} loading={loading && !data}>
              <Typography.Text type="secondary" style={{ fontSize: 13, fontWeight: 500 }}>
                {s.label}
              </Typography.Text>
              <Typography.Title level={2} style={{ margin: "4px 0 2px", fontSize: 28, fontWeight: 700, letterSpacing: "-0.03em" }}>
                <Tooltip title={formatExact(s.value)}>
                  <span>{formatCount(s.value)}</span>
                </Tooltip>
              </Typography.Title>
              <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                {s.desc}
              </Typography.Text>
            </Card>
          </Col>
        ))}
      </Row>

      <Card
        bordered={false}
        className="premium-card"
        style={{ borderRadius: 12 }}
        title={
          <Flex align="center" gap={8}>
            <FundOutlined style={{ color: token.colorPrimary }} />
            <span>分项汇总</span>
            {data && (
              <Typography.Text type="secondary" style={{ fontSize: 12, fontWeight: 400 }}>
                {data.from} ~ {data.to}
              </Typography.Text>
            )}
          </Flex>
        }
      >
        <Tabs
          items={[
            { key: "token", label: "按令牌", children: bucketTable(data?.by_token || [], "这段时间没有按令牌的用量") },
            { key: "node", label: "按节点", children: bucketTable(data?.by_node || [], "这段时间没有按节点的用量") },
            { key: "model", label: "按模型", children: bucketTable(data?.by_model || [], "这段时间没有按模型的用量") },
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
                  locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有用量。成功的推理会记入账本。" /> }}
                  columns={[{ title: "日期", dataIndex: "name", width: 140 }, ...usageColumns()]}
                />
              ),
            },
          ]}
        />
      </Card>
    </div>
  );
}
