import { useEffect, useState } from "react";
import { Card, Table, Typography } from "antd";
import { userApi } from "../../api";
import { PageHeader } from "../../components/PageHeader";
import type { ShopInfo, ShopModel } from "../../types";

export default function Models() {
  const [shop, setShop] = useState<ShopInfo | null>(null);

  useEffect(() => {
    userApi.shop().then(setShop).catch(() => setShop(null));
  }, []);

  const rows: ShopModel[] =
    shop?.models?.length
      ? shop.models
      : (shop?.model_list || []).map((id) => ({
          id,
          input_price_per_1m: shop?.input_price_per_1m ?? 0,
          output_price_per_1m: shop?.output_price_per_1m ?? 0,
        }));
  const hasOfficial = rows.some(
    (row) => Number(row.official_input_price_per_1m || 0) > 0 || Number(row.official_output_price_per_1m || 0) > 0,
  );

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

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <PageHeader
        title="模型"
        desc={hasOfficial ? "本站单价用于扣费。官方价仅供对照，不参与计费。" : "当前可调用的模型及各自单价。客户端填模型 id 即可。"}
      />
      <Card bordered={false} className="premium-card" style={{ borderRadius: 12 }} styles={{ body: { padding: 0 } }}>
        <Table
          rowKey="id"
          dataSource={rows}
          pagination={false}
          locale={{ emptyText: "暂时没有可调用的模型。" }}
          columns={[
            {
              title: "模型",
              dataIndex: "id",
              render: (v: string) => (
                <Typography.Text className="code-monospace" copyable={{ text: v }}>
                  {v}
                </Typography.Text>
              ),
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
