import type { ReactNode } from "react";
import { Flex, Typography } from "antd";

export function SettingRow({
  title,
  desc,
  children,
}: {
  title: string;
  desc?: ReactNode;
  children: ReactNode;
}) {
  return (
    <Flex justify="space-between" align="center" gap={32} wrap="wrap" style={{ width: "100%" }}>
      <div style={{ flex: "1 1 240px", minWidth: 200, maxWidth: 360 }}>
        <Typography.Text strong style={{ fontSize: 14, display: "block" }}>{title}</Typography.Text>
        {desc ? (
          <div style={{ marginTop: 2 }}>
            <Typography.Text type="secondary" style={{ fontSize: 12.5, lineHeight: 1.4, display: "block" }}>
              {desc}
            </Typography.Text>
          </div>
        ) : null}
      </div>
      <div
        style={{
          flex: "1 1 280px",
          minWidth: 240,
          display: "flex",
          justifyContent: "flex-end",
          alignItems: "center",
        }}
      >
        {children}
      </div>
    </Flex>
  );
}

