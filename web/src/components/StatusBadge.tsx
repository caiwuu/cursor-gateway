import { Flex, Typography } from "antd";
import type { NodeRecord } from "../types";

export function nodeStatus(node: NodeRecord): {
  status: "success" | "warning" | "default";
  text: string;
  color: string;
  className: string;
} {
  if (node.runtime.listening) {
    return { status: "success", text: "运行中", color: "#10b981", className: "status-pulse-green" };
  }
  if (node.enabled) {
    return { status: "warning", text: "未绑定", color: "#f59e0b", className: "status-pulse-yellow" };
  }
  return { status: "default", text: "已停用", color: "#9ca3af", className: "status-dot-gray" };
}

export function StatusBadge({ node }: { node: NodeRecord }) {
  const s = nodeStatus(node);
  return (
    <Flex align="center" gap={8} style={{ display: "inline-flex" }}>
      <span className={s.className} style={{ flexShrink: 0 }} />
      <Typography.Text style={{ fontSize: 13, fontWeight: 500, color: s.color }}>
        {s.text}
      </Typography.Text>
    </Flex>
  );
}

