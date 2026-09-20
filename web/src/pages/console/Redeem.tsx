import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Alert, App, Button, Card, Flex, Form, Input, Table, Tag, Typography } from "antd";
import { GiftOutlined, ShoppingOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import { setUserSession, userApi } from "../../api";
import { PageHeader } from "../../components/PageHeader";
import { formatYuan } from "../../format";
import type { GatewayUser, LedgerEntry } from "../../types";
import { ledgerKind } from "./shared";

export default function Redeem() {
  const { message } = App.useApp();
  const nav = useNavigate();
  const [me, setMe] = useState<GatewayUser | null>(null);
  const [ledger, setLedger] = useState<LedgerEntry[]>([]);
  const [shopUrl, setShopUrl] = useState("");
  const [error, setError] = useState("");

  const load = () =>
    Promise.all([userApi.me(), userApi.ledger(100), userApi.shop()])
      .then(([u, l, shop]) => {
        setMe(u);
        setLedger(l.ledger);
        setShopUrl((shop.shop_url || u.shop?.shop_url || "").trim());
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

  useEffect(() => {
    load();
  }, []);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <PageHeader title="兑换" desc="用站长发的卡密加余额。消费记录也在这页。" />
      {error && <Alert type="error" showIcon closable message={error} />}
      <Card bordered={false} className="premium-card" style={{ borderRadius: 12 }}>
        <Flex align="center" justify="space-between" wrap="wrap" gap={16} style={{ marginBottom: 16 }}>
          <div>
            <Typography.Text type="secondary">当前余额</Typography.Text>
            <Typography.Title level={3} style={{ margin: "4px 0 0" }}>
              {formatYuan(me?.balance || 0)}
            </Typography.Title>
          </div>
          <Typography.Text type="secondary">单价按模型算，详见「模型」页</Typography.Text>
        </Flex>
        <Flex align="center" justify="space-between" wrap="wrap" gap={12} style={{ marginBottom: 12 }}>
          <Flex align="center" gap={8}>
            <GiftOutlined />
            <Typography.Text strong>兑换卡密</Typography.Text>
          </Flex>
          {shopUrl ? (
            <Button type="link" href={shopUrl} target="_blank" rel="noreferrer" icon={<ShoppingOutlined />} style={{ paddingInline: 0 }}>
              去商店购买
            </Button>
          ) : null}
        </Flex>
        <Form
          layout="inline"
          onFinish={async (values: { code: string }) => {
            const updated = await userApi.redeem(values.code.trim());
            setMe(updated);
            message.success(`已到账，余额 ${formatYuan(updated.balance)}`);
            await load();
          }}
        >
          <Form.Item name="code" rules={[{ required: true, message: "请填写卡密" }]} style={{ flex: 1, minWidth: 280 }}>
            <Input placeholder="CG-XXXX-XXXX-XXXX-XXXX" className="code-monospace" />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit" style={{ background: "#0d9488", border: "none" }}>
              兑换
            </Button>
          </Form.Item>
        </Form>
        {shopUrl ? (
          <Typography.Paragraph type="secondary" style={{ margin: "12px 0 0", fontSize: 13 }}>
            还没有卡密？
            <Typography.Link href={shopUrl} target="_blank" rel="noreferrer">
              前往购买商店
            </Typography.Link>
          </Typography.Paragraph>
        ) : null}
      </Card>
      <Card bordered={false} className="premium-card" style={{ borderRadius: 12 }} title="账单" styles={{ body: { padding: 0 } }}>
        <Table
          rowKey="id"
          dataSource={ledger}
          pagination={ledger.length > 20 ? { pageSize: 20 } : false}
          locale={{ emptyText: "还没有流水。" }}
          columns={[
            {
              title: "时间",
              width: 160,
              render: (_: unknown, row: LedgerEntry) =>
                row.created_at ? dayjs.unix(row.created_at).format("YYYY-MM-DD HH:mm") : "—",
            },
            {
              title: "类型",
              width: 90,
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
