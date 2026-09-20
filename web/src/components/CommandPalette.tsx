import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { Input, Modal, Typography, theme, Tag, Space } from "antd";
import {
  AppstoreOutlined,
  DashboardOutlined,
  MessageOutlined,
  FundOutlined,
  KeyOutlined,
  PlusOutlined,
  TeamOutlined,
  SearchOutlined,
  SettingOutlined,
} from "@ant-design/icons";
import { api } from "../api";
import type { NodeRecord } from "../types";
import { useThemeMode } from "../theme";

type Item = {
  key: string;
  label: string;
  hint?: string;
  icon: ReactNode;
  run: () => void;
};

export function CommandPalette({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
}) {
  const nav = useNavigate();
  const { token } = theme.useToken();
  const { mode } = useThemeMode();
  const [q, setQ] = useState("");
  const [active, setActive] = useState(0);
  const [nodes, setNodes] = useState<NodeRecord[]>([]);

  useEffect(() => {
    if (!open) return;
    setQ("");
    setActive(0);
    api
      .nodes()
      .then((d) => setNodes(d.nodes))
      .catch(() => setNodes([]));
  }, [open]);

  const items = useMemo(() => {
    const go = (path: string) => {
      onOpenChange(false);
      nav(path);
    };
    const pages: Item[] = [
      { key: "p-home", label: "概览", hint: "控制台", icon: <DashboardOutlined />, run: () => go("/") },
      { key: "p-nodes", label: "节点", hint: "账号列表", icon: <AppstoreOutlined />, run: () => go("/nodes") },
      { key: "p-new", label: "新建节点", hint: "添加账号", icon: <PlusOutlined />, run: () => go("/nodes/new") },
      { key: "p-tokens", label: "令牌", hint: "分发给别人", icon: <KeyOutlined />, run: () => go("/tokens") },
      { key: "p-users", label: "用户", hint: "账号与卡密", icon: <TeamOutlined />, run: () => go("/users") },
      { key: "p-console", label: "用户台", hint: "自助买 token", icon: <KeyOutlined />, run: () => go("/console") },
      { key: "p-usage", label: "用量", hint: "计费账本", icon: <FundOutlined />, run: () => go("/usage") },
      { key: "p-play", label: "调试", hint: "对话测试", icon: <MessageOutlined />, run: () => go("/playground") },
      { key: "p-set", label: "设置", hint: "管理台", icon: <SettingOutlined />, run: () => go("/settings") },
    ];
    const nodeItems: Item[] = nodes.map((n) => ({
      key: `n-${n.id}`,
      label: n.email || n.name,
      hint: n.default_mode,
      icon: <AppstoreOutlined />,
      run: () => go(`/nodes/${n.id}`),
    }));
    const query = q.trim().toLowerCase();
    return [...pages, ...nodeItems].filter((item) => {
      if (!query) return true;
      return `${item.label} ${item.hint || ""}`.toLowerCase().includes(query);
    });
  }, [nav, nodes, onOpenChange, q]);

  useEffect(() => {
    setActive(0);
  }, [q]);

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((i) => Math.min(items.length - 1, i + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((i) => Math.max(0, i - 1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      items[active]?.run();
    }
  }

  return (
    <Modal
      open={open}
      onCancel={() => onOpenChange(false)}
      footer={null}
      closable={false}
      width={540}
      styles={{
        body: { padding: 0, overflow: "hidden", borderRadius: 12 },
      }}
      destroyOnClose
      style={{ top: "15vh" }}
    >
      <div style={{ padding: "14px 16px 10px 16px" }}>
        <Input
          autoFocus
          size="large"
          variant="borderless"
          prefix={<SearchOutlined style={{ color: token.colorPrimary, fontSize: 18, marginRight: 4 }} />}
          placeholder="搜索页面、管理选项、节点路由..."
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={onKeyDown}
          style={{ height: 40, fontSize: 16, padding: 0 }}
        />
      </div>
      
      <div style={{ borderTop: `1px solid ${token.colorSplit}`, maxHeight: 340, overflowY: "auto", padding: "8px" }}>
        {items.length === 0 ? (
          <div style={{ textAlign: "center", padding: "24px 0" }}>
            <Typography.Text type="secondary" style={{ fontSize: 13 }}>
              没有找到相关指令或节点
            </Typography.Text>
          </div>
        ) : (
          items.map((item, i) => (
            <button
              key={item.key}
              type="button"
              onMouseEnter={() => setActive(i)}
              onClick={item.run}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 12,
                width: "100%",
                border: 0,
                borderRadius: 8,
                padding: "10px 14px",
                cursor: "pointer",
                textAlign: "left",
                background: i === active ? token.colorPrimaryBg : "transparent",
                color: i === active ? token.colorPrimary : token.colorText,
                transition: "all 0.15s ease",
              }}
            >
              <span style={{
                color: i === active ? token.colorPrimary : token.colorTextSecondary,
                fontSize: 15,
                display: "flex"
              }}>
                {item.icon}
              </span>
              <span style={{ flex: 1, fontWeight: i === active ? 600 : 500, fontSize: 14 }}>
                {item.label}
              </span>
              {item.hint ? (
                <Tag
                  bordered={false}
                  color={i === active ? "teal" : "default"}
                  style={{
                    fontSize: 11,
                    borderRadius: 4,
                    margin: 0,
                    textTransform: "lowercase",
                    fontFamily: "monospace"
                  }}
                >
                  {item.hint}
                </Tag>
              ) : null}
            </button>
          ))
        )}
      </div>

      {/* Keyboard Shortcuts Footer */}
      <div
        style={{
          borderTop: `1px solid ${token.colorSplit}`,
          padding: "10px 16px",
          background: mode === "dark" ? "#121214" : "#fafafa",
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
        }}
      >
        <Typography.Text type="secondary" style={{ fontSize: 11 }}>
          输入关键词快速搜索并跳转
        </Typography.Text>
        <Space size={12}>
          <span style={{ fontSize: 11, color: token.colorTextDescription }}>
            <Typography.Text keyboard style={{ fontSize: 10, padding: "2px 4px" }}>↑↓</Typography.Text> 选择
          </span>
          <span style={{ fontSize: 11, color: token.colorTextDescription }}>
            <Typography.Text keyboard style={{ fontSize: 10, padding: "2px 4px" }}>Enter</Typography.Text> 跳转
          </span>
          <span style={{ fontSize: 11, color: token.colorTextDescription }}>
            <Typography.Text keyboard style={{ fontSize: 10, padding: "2px 4px" }}>Esc</Typography.Text> 关闭
          </span>
        </Space>
      </div>
    </Modal>
  );
}
