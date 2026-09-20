import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { Breadcrumb, Flex, Typography } from "antd";

export function PageHeader({
  title,
  desc,
  extra,
  breadcrumb,
}: {
  title: ReactNode;
  desc?: ReactNode;
  extra?: ReactNode;
  breadcrumb?: { title: ReactNode; href?: string }[];
}) {
  return (
    <div className="page-header">
      {breadcrumb && breadcrumb.length > 0 && (
        <Breadcrumb
          style={{ marginBottom: 8 }}
          items={breadcrumb.map((item) => ({
            title: item.href ? <Link to={item.href}>{item.title}</Link> : item.title,
          }))}
        />
      )}
      <Flex justify="space-between" align="flex-end" gap={20} wrap="wrap">
        <div style={{ minWidth: 0 }}>
          <Typography.Text className="page-header-kicker">工作台</Typography.Text>
          <Typography.Title level={3} className="page-header-title">
            {title}
          </Typography.Title>
          {desc ? (
            <Typography.Paragraph type="secondary" className="page-header-desc">
              {desc}
            </Typography.Paragraph>
          ) : null}
        </div>
        {extra ? <Flex gap={8}>{extra}</Flex> : null}
      </Flex>
    </div>
  );
}
