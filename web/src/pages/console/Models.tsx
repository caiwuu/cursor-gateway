import { useEffect, useMemo, useState } from "react";
import { App, Button, Card, Flex, Input, Table, Typography } from "antd";
import { userApi } from "../../api";
import { useAuth } from "../../auth";
import { PageHeader } from "../../components/PageHeader";
import type { ShopModel } from "../../types";

export default function Models() {
  const { message } = App.useApp();
  const { me, shop, setMe } = useAuth();
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [prefix, setPrefix] = useState("ohmy-");

  useEffect(() => {
    setDrafts(me?.model_aliases || {});
  }, [me?.model_aliases]);

  const rows: ShopModel[] = useMemo(() => {
    if (shop?.models?.length) return shop.models;
    return (shop?.model_list || []).map((id) => ({
      id,
      input_price_per_1m: shop?.input_price_per_1m ?? 0,
      output_price_per_1m: shop?.output_price_per_1m ?? 0,
    }));
  }, [shop]);

  const hasOfficial = rows.some(
    (row) => Number(row.official_input_price_per_1m || 0) > 0 || Number(row.official_output_price_per_1m || 0) > 0,
  );
  const dirty = rows.some((row) => (drafts[row.id] || "").trim() !== (me?.model_aliases?.[row.id] || "").trim());

  function priceCell(ours: number, official?: number) {
    const ref = Number(official || 0);
    return (
      <div>
        <div>{ours} 元</div>
        {ref > 0 ? (
          <Typography.Text style={{ fontSize: 12, fontWeight: 650, color: "#0f8f83" }}>
            官方 ≈ {ref} 元
          </Typography.Text>
        ) : null}
      </div>
    );
  }

  async function save(next = drafts) {
    const aliases: Record<string, string> = {};
    for (const row of rows) aliases[row.id] = (next[row.id] || "").trim();
    setSaving(true);
    try {
      const res = await userApi.saveModelAliases(aliases);
      setDrafts(res.model_aliases || {});
      if (me) setMe({ ...me, model_aliases: res.model_aliases || {} });
      message.success("别名已保存。客户端刷新模型列表后生效。");
    } catch (e) {
      message.error(String((e as Error).message || e));
    } finally {
      setSaving(false);
    }
  }

  function applyPrefix() {
    const head = prefix.trim();
    if (!head) {
      message.warning("先填一个前缀");
      return;
    }
    const next = { ...drafts };
    for (const row of rows) {
      if ((next[row.id] || "").trim()) continue;
      next[row.id] = `${head}${row.id}`;
    }
    setDrafts(next);
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <PageHeader
        title="模型"
        desc={
          hasOfficial
            ? "本站单价用于扣费。接入 Cursor 时若和官方模型重名，给它起个别名；/v1/models 会返回别名，请求按别名路由到真实模型。"
            : "客户端填模型 id 即可。接入 Cursor 时若和官方模型重名，在右侧填别名，刷新模型列表后就不会撞名。"
        }
        extra={
          <Button type="primary" loading={saving} disabled={!dirty} onClick={() => save()} style={{ background: "#0d9488", border: "none" }}>
            保存别名
          </Button>
        }
      />
      <Card bordered={false} className="premium-card" style={{ borderRadius: 12 }}>
        <Flex gap={8} wrap="wrap" align="center">
          <Typography.Text type="secondary" style={{ fontSize: 13 }}>
            给还没起别名的模型加前缀
          </Typography.Text>
          <Input value={prefix} onChange={(e) => setPrefix(e.target.value)} style={{ width: 140 }} placeholder="ohmy-" />
          <Button onClick={applyPrefix}>应用到空别名</Button>
        </Flex>
      </Card>
      <Card bordered={false} className="premium-card" style={{ borderRadius: 12 }} styles={{ body: { padding: 0 } }}>
        <Table
          rowKey="id"
          dataSource={rows}
          pagination={false}
          locale={{ emptyText: "暂时没有可调用的模型。" }}
          columns={[
            {
              title: "真实模型",
              dataIndex: "id",
              render: (v: string) => (
                <Typography.Text className="code-monospace" copyable={{ text: v }}>
                  {v}
                </Typography.Text>
              ),
            },
            {
              title: "别名（给 Cursor 用）",
              width: 280,
              render: (_: unknown, row: ShopModel) => (
                <Input
                  value={drafts[row.id] ?? ""}
                  placeholder={row.id}
                  onChange={(e) => setDrafts((cur) => ({ ...cur, [row.id]: e.target.value }))}
                  onPressEnter={() => save()}
                />
              ),
            },
            {
              title: "客户端看到的 id",
              width: 220,
              render: (_: unknown, row: ShopModel) => {
                const id = (drafts[row.id] || "").trim() || row.id;
                return (
                  <Typography.Text className="code-monospace" copyable={{ text: id }}>
                    {id}
                  </Typography.Text>
                );
              },
            },
            {
              title: "输入 / 百万",
              width: 160,
              render: (_: unknown, row: ShopModel) => priceCell(row.input_price_per_1m, row.official_input_price_per_1m),
            },
            {
              title: "输出 / 百万",
              width: 160,
              render: (_: unknown, row: ShopModel) => priceCell(row.output_price_per_1m, row.official_output_price_per_1m),
            },
          ]}
        />
      </Card>
    </div>
  );
}
