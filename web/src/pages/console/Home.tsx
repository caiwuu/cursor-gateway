import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Alert, Button, Card, Col, Flex, Row, Table, Tag, Typography } from "antd";
import { GiftOutlined, KeyOutlined, MessageOutlined, RightOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import { setUserSession, userApi } from "../../api";
import { PageHeader } from "../../components/PageHeader";
import { formatCompact, formatYuan } from "../../format";
import type { GatewayUser, LedgerEntry, RequestLog, UsageSummary } from "../../types";
import { ledgerKind } from "./shared";

export default function Home() {
  const nav = useNavigate();
  const [me, setMe] = useState<GatewayUser | null>(null);
  const [usage, setUsage] = useState<(UsageSummary & { estimated_cost_yuan?: number }) | null>(null);
  const [today, setToday] = useState<(UsageSummary & { estimated_cost_yuan?: number }) | null>(null);
  const [ledger, setLedger] = useState<LedgerEntry[]>([]);
  const [logs, setLogs] = useState<RequestLog[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    Promise.all([userApi.me(), userApi.usage(7), userApi.usage(1), userApi.ledger(8), userApi.logs(8, 0)])
      .then(([u, week, day, l, r]) => {
        setMe(u);
        setUsage(week);
        setToday(day);
        setLedger(l.ledger);
        setLogs(r.logs);
      })
      .catch((e) => {
        const text = String((e as Error).message || e);
        if (text.includes("登录")) {
          setUserSession("");
          nav("/login", { replace: true });
          return;
        }
        setError(text);
      });
  }, [nav]);

  const cards = [
    { label: "余额", value: formatYuan(me?.balance || 0), hint: "兑卡密后才能调用" },
    { label: "今日消费", value: `¥${(today?.estimated_cost_yuan || 0).toFixed(4)}`, hint: `${formatCompact(today?.totals.requests || 0)} 次请求` },
    { label: "7 日 Token", value: formatCompact(usage?.totals.total_tokens || 0), hint: `输入 ${formatCompact(usage?.totals.prompt_tokens || 0)} · 输出 ${formatCompact(usage?.totals.completion_tokens || 0)}` },
    { label: "令牌", value: String(me?.token_count || 0), hint: "一个业务一把钥匙" },
  ];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <PageHeader
        title={`你好，${me?.username || "用户"}`}
        desc="概览余额和最近用量。令牌、兑换、模型、调试都在左侧这些页。"
        extra={
          <Flex gap={8}>
            <Button icon={<GiftOutlined />} onClick={() => nav("/console/redeem")}>
              兑卡密
            </Button>
            <Button type="primary" icon={<KeyOutlined />} onClick={() => nav("/console/keys")} style={{ background: "#0d9488", border: "none" }}>
              管理令牌
            </Button>
          </Flex>
        }
      />
      {error && <Alert type="error" showIcon closable message={error} />}
      <Row gutter={[16, 16]}>
        {cards.map((c) => (
          <Col xs={12} md={6} key={c.label}>
            <Card bordered={false} className="premium-card" style={{ borderRadius: 12 }}>
              <Typography.Text type="secondary">{c.label}</Typography.Text>
              <Typography.Title level={3} style={{ margin: "8px 0 4px" }}>
                {c.value}
              </Typography.Title>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {c.hint}
              </Typography.Text>
            </Card>
          </Col>
        ))}
      </Row>

      <Flex gap={12} wrap="wrap">
        {[
          { to: "/console/keys", icon: <KeyOutlined />, title: "令牌", desc: "创建 sk-，填进客户端" },
          { to: "/console/models", icon: <RightOutlined />, title: "模型", desc: "看支持的模型和单价" },
          { to: "/console/play", icon: <MessageOutlined />, title: "调试", desc: "先打一句话确认通路" },
          { to: "/console/usage", icon: <RightOutlined />, title: "用量", desc: "按日 / 模型 / 令牌" },
        ].map((x) => (
          <Link key={x.to} to={x.to} style={{ flex: "1 1 200px" }}>
            <Card bordered={false} className="premium-card" hoverable style={{ borderRadius: 12 }}>
              <Flex align="center" gap={10}>
                <span>{x.icon}</span>
                <div>
                  <Typography.Text strong>{x.title}</Typography.Text>
                  <div>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      {x.desc}
                    </Typography.Text>
                  </div>
                </div>
              </Flex>
            </Card>
          </Link>
        ))}
      </Flex>

      <Card bordered={false} className="premium-card" style={{ borderRadius: 12 }} title="最近请求" extra={<Link to="/console/usage">全部</Link>}>
        <Table
          rowKey="id"
          size="small"
          pagination={false}
          dataSource={logs}
          locale={{ emptyText: "还没有请求。先兑卡密，再建一把令牌。" }}
          columns={[
            {
              title: "时间",
              width: 120,
              render: (_: unknown, row: RequestLog) => (row.created_at ? dayjs.unix(row.created_at).format("MM-DD HH:mm") : "—"),
            },
            { title: "模型", dataIndex: "model", ellipsis: true },
            { title: "令牌", dataIndex: "token_name", width: 100 },
            {
              title: "状态",
              width: 80,
              render: (_: unknown, row: RequestLog) => <Tag color={row.status < 400 ? "green" : "red"}>{row.status}</Tag>,
            },
            {
              title: "费用",
              width: 100,
              render: (_: unknown, row: RequestLog) => formatYuan(row.cost || 0),
            },
          ]}
        />
      </Card>

      <Card bordered={false} className="premium-card" style={{ borderRadius: 12 }} title="最近账本" extra={<Link to="/console/redeem">兑换与账单</Link>}>
        <Table
          rowKey="id"
          size="small"
          pagination={false}
          dataSource={ledger}
          locale={{ emptyText: "还没有流水。" }}
          columns={[
            {
              title: "时间",
              width: 120,
              render: (_: unknown, row: LedgerEntry) => (row.created_at ? dayjs.unix(row.created_at).format("MM-DD HH:mm") : "—"),
            },
            {
              title: "类型",
              width: 80,
              render: (_: unknown, row: LedgerEntry) => <Tag>{ledgerKind(row.kind)}</Tag>,
            },
            {
              title: "变动",
              render: (_: unknown, row: LedgerEntry) => (
                <Typography.Text type={row.amount >= 0 ? "success" : undefined}>
                  {row.amount >= 0 ? "+" : ""}
                  {formatYuan(row.amount)}
                </Typography.Text>
              ),
            },
            { title: "余额", render: (_: unknown, row: LedgerEntry) => formatYuan(row.balance) },
            { title: "备注", dataIndex: "note", ellipsis: true },
          ]}
        />
      </Card>
    </div>
  );
}
