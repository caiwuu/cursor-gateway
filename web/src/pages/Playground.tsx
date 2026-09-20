import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Alert, Button, Empty, Flex, Input, Select, Typography, theme, Avatar, Tooltip, App } from "antd";
import {
  SendOutlined,
  DeleteOutlined,
  CopyOutlined,
  RobotOutlined,
  UserOutlined,
  BulbOutlined,
  ApiOutlined,
  CheckOutlined,
} from "@ant-design/icons";
import { api } from "../api";
import {
  DEFAULT_MODEL_LIST,
  MODE_LABEL,
  enabledModesOf,
  normalizeModeConfig,
  type Mode,
  type ModeConfig,
  type NodeRecord,
} from "../types";
import { useThemeMode } from "../theme";

type ChatMsg = { role: "user" | "assistant"; content: string; thinking?: string };

const QUICK_PROMPTS = [
  { label: "✨ 向 AI 问好", text: "你好！请介绍一下你自己，并告诉我你能提供什么帮助。" },
  { label: "💻 编写快速排序", text: "请使用 TypeScript 编写一个高效的快速排序 (Quick Sort) 算法，并写出测试用例。" },
  { label: "🔍 检查网关状态", text: "请输出一段文字，测试你当前作为 API 网关后端的响应速度与延迟表现。" },
];

export default function Playground() {
  const { token } = theme.useToken();
  const { mode: themeMode } = useThemeMode();
  const { message } = App.useApp();
  const [params] = useSearchParams();
  const [nodes, setNodes] = useState<NodeRecord[]>([]);
  const [fallbackModels, setFallbackModels] = useState<string[]>(DEFAULT_MODEL_LIST);
  const [modelList, setModelList] = useState<string[]>(DEFAULT_MODEL_LIST);
  const AUTO_NODE = "__lb__";
  const [nodeId, setNodeId] = useState(params.get("node") || AUTO_NODE);
  const [mode, setMode] = useState<Mode | "default">("default");
  const [model, setModel] = useState("claude-opus-5");
  const [prompt, setPrompt] = useState("");
  const [msgs, setMsgs] = useState<ChatMsg[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [copiedId, setCopiedId] = useState<number | null>(null);
  const [inputFocused, setInputFocused] = useState(false);
  const [gatewayToken, setGatewayToken] = useState("");
  const scroller = useRef<HTMLDivElement>(null);

  useEffect(() => {
    Promise.all([api.nodes(), api.meta(), api.tokens().catch(() => ({ tokens: [] }))]).then(([n, m, t]) => {
      setNodes(n.nodes);
      setNodeId((cur) => cur || AUTO_NODE);
      const list = m.model_list?.length ? m.model_list : m.models.map((x) => x.id);
      const catalog = list.length ? list : DEFAULT_MODEL_LIST;
      setModelList(catalog);
      setFallbackModels(catalog);
      setGatewayToken(t.tokens.find((x) => x.enabled)?.token || "");
    });
  }, []);

  useEffect(() => {
    if (scroller.current) {
      scroller.current.scrollTop = scroller.current.scrollHeight;
    }
  }, [msgs, busy]);

  const isAuto = nodeId === AUTO_NODE;
  const node = nodes.find((n) => n.id === nodeId);
  const availableModes = useMemo(() => {
    const pool = isAuto ? nodes : node ? [node] : [];
    const set = new Set<Mode>();
    for (const n of pool) {
      for (const m of enabledModesOf(n.mode_config)) set.add(m);
    }
    return Array.from(set);
  }, [isAuto, node, nodes]);
  const resolvedMode: Mode | "default" = mode === "default" || !availableModes.includes(mode as Mode)
    ? "default"
    : mode;
  const models = useMemo(() => {
    const pool = isAuto ? nodes : node ? [node] : [];
    const want = resolvedMode === "default" ? "" : (resolvedMode as Mode);
    const seen = new Set<string>();
    const out: string[] = [];
    for (const n of pool) {
      const cfg: ModeConfig = normalizeModeConfig(n.mode_config, modelList);
      const modes = want ? [want] : enabledModesOf(cfg);
      for (const item of modes) {
        if (!cfg[item]?.enabled) continue;
        for (const id of cfg[item].models || []) {
          if (seen.has(id)) continue;
          seen.add(id);
          out.push(id);
        }
      }
    }
    return out.length ? out : fallbackModels;
  }, [isAuto, node, nodes, resolvedMode, fallbackModels, modelList]);

  useEffect(() => {
    if (mode !== "default" && !availableModes.includes(mode as Mode)) {
      setMode("default");
    }
  }, [availableModes, mode]);

  useEffect(() => {
    if (models.length && !models.includes(model)) {
      setModel(models[0]);
    }
  }, [models, model]);
  const pathMode = resolvedMode === "default" ? "" : `/${resolvedMode}`;
  const url = useMemo(() => {
    if (!isAuto && !node) return "";
    const prefix = isAuto ? "" : `/n/${node!.slug}`;
    return `${prefix}${pathMode}/v1/chat/completions`;
  }, [isAuto, node, pathMode]);

  const fullUrl = useMemo(() => {
    if (!url) return "";
    const origin = window.location.origin;
    return `${origin}${url}`;
  }, [url]);

  async function send(textToSend?: string) {
    const text = (textToSend || prompt).trim();
    if ((!isAuto && !node) || !text || busy) return;
    
    setPrompt("");
    setBusy(true);
    setError("");
    
    // Append user message and prepare empty assistant message
    setMsgs((m) => [
      ...m,
      { role: "user", content: text },
      { role: "assistant", content: "", thinking: "" }
    ]);

    try {
      const res = await fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(gatewayToken ? { Authorization: `Bearer ${gatewayToken}` } : {}),
        },
        body: JSON.stringify({
          model,
          stream: true,
          messages: [...msgs.filter(m => m.content).map(m => ({ role: m.role, content: m.content })), { role: "user", content: text }],
        }),
      });

      if (!res.ok || !res.body) {
        const raw = await res.text();
        throw new Error(raw || `HTTP ${res.status}`);
      }

      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const parts = buf.split("\n\n");
        buf = parts.pop() || "";
        for (const part of parts) {
          const line = part.split("\n").find((l) => l.startsWith("data: "));
          if (!line) continue;
          const data = line.slice(6).trim();
          if (data === "[DONE]") continue;
          try {
            const json = JSON.parse(data);
            const delta = json.choices?.[0]?.delta || {};
            setMsgs((cur) => {
              const next = [...cur];
              const last = next[next.length - 1];
              if (!last || last.role !== "assistant") return cur;
              next[next.length - 1] = {
                ...last,
                thinking: (last.thinking || "") + (delta.reasoning_content || ""),
                content: last.content + (delta.content || ""),
              };
              return next;
            });
          } catch {
            /* ignore */
          }
        }
      }
    } catch (e) {
      setError(String((e as Error).message || e));
      // Remove the empty assistant bubble if error occurred immediately
      setMsgs((cur) => {
        const last = cur[cur.length - 1];
        if (last && last.role === "assistant" && !last.content && !last.thinking) {
          return cur.slice(0, -1);
        }
        return cur;
      });
    } finally {
      setBusy(false);
    }
  }

  const handleCopy = async (text: string, index: number) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedId(index);
      message.success("已复制到剪贴板");
      setTimeout(() => setCopiedId(null), 2000);
    } catch (e) {
      message.error("复制失败");
    }
  };

  const handleClear = () => {
    setMsgs([]);
    setError("");
    message.success("对话已清空");
  };

  return (
    <Flex vertical style={{ height: "100%", background: token.colorBgLayout }}>
      {/* Playground Header Options */}
      <Flex
        align="center"
        justify="space-between"
        gap={12}
        wrap="wrap"
        style={{
          padding: "12px 24px",
          borderBottom: `1px solid ${token.colorSplit}`,
          background: token.colorBgContainer,
          boxShadow: "0 1px 2px rgba(0, 0, 0, 0.03)",
          zIndex: 10,
        }}
      >
        <Flex align="center" gap={12} wrap="wrap" style={{ flex: 1, minWidth: 0 }}>
          <Typography.Text strong style={{ fontSize: 15, display: "flex", alignItems: "center", gap: 6, color: token.colorPrimary }}>
            <ApiOutlined />
            在线调试
          </Typography.Text>
          <div style={{ width: 1, height: 16, backgroundColor: token.colorSplit }} />
          <Select
            value={nodeId || undefined}
            onChange={setNodeId}
            placeholder="选择调试节点"
            style={{ width: 180 }}
            options={[
              { value: AUTO_NODE, label: "负载均衡（自动选节点）" },
              ...nodes.map((n) => ({ value: n.id, label: n.name || n.email || n.slug })),
            ]}
          />
          <Select
            value={resolvedMode}
            onChange={setMode}
            style={{ width: 180 }}
            options={[
              { value: "default", label: "默认模式" },
              ...availableModes.map((m) => ({ value: m, label: MODE_LABEL[m] })),
            ]}
          />
          <Select
            value={model}
            onChange={setModel}
            showSearch
            listHeight={320}
            style={{ minWidth: 200 }}
            options={models.map((m) => ({ value: m, label: m }))}
          />
        </Flex>

        <Flex align="center" gap={12}>
          {url && (
            <Tooltip title="点击复制 API 路径">
              <Typography.Text
                code
                className="code-monospace"
                style={{
                  padding: "4px 8px",
                  background: token.colorBgLayout,
                  borderRadius: 6,
                  cursor: "pointer",
                  fontSize: 12,
                }}
                onClick={() => {
                  navigator.clipboard.writeText(fullUrl);
                  message.success("API 路径已复制");
                }}
              >
                POST {url}
              </Typography.Text>
            </Tooltip>
          )}
          {msgs.length > 0 && (
            <Tooltip title="清空当前对话">
              <Button
                type="text"
                danger
                icon={<DeleteOutlined />}
                onClick={handleClear}
                style={{ borderRadius: 6 }}
              />
            </Tooltip>
          )}
        </Flex>
      </Flex>

      {/* Messages Scroll Area */}
      <div
        ref={scroller}
        style={{
          flex: 1,
          overflowY: "auto",
          padding: "32px 24px",
          scrollBehavior: "smooth",
        }}
      >
        <div style={{ maxWidth: 800, margin: "0 auto", width: "100%" }}>
          {error && <Alert type="error" showIcon message="请求出错" description={error} style={{ marginBottom: 20, borderRadius: 10 }} />}
          
          {msgs.length === 0 && !error && (
            <div style={{ padding: "40px 0 20px 0" }}>
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={
                  <div style={{ marginTop: 8 }}>
                    <Typography.Text strong style={{ fontSize: 16, display: "block", marginBottom: 4 }}>
                      开始调试 API
                    </Typography.Text>
                    <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                      在这里，你可以通过直观的聊天界面直接测试该节点对应的 Cursor 后端。
                    </Typography.Text>
                  </div>
                }
              />
              <Flex vertical gap={12} style={{ maxWidth: 480, margin: "24px auto 0 auto" }}>
                <Typography.Text type="secondary" style={{ fontSize: 12, textAlign: "center", marginBottom: 4 }}>
                  💡 常用测试 Prompt 推荐
                </Typography.Text>
                {QUICK_PROMPTS.map((qp, idx) => (
                  <Button
                    key={idx}
                    type="dashed"
                    block
                    style={{
                      textAlign: "left",
                      height: "auto",
                      padding: "10px 14px",
                      borderRadius: 8,
                      fontSize: 13,
                    }}
                    onClick={() => {
                      setPrompt(qp.text);
                      send(qp.text);
                    }}
                  >
                    {qp.label}
                  </Button>
                ))}
              </Flex>
            </div>
          )}

          <Flex vertical gap={24}>
            {msgs.map((msg, i) => {
              const isUser = msg.role === "user";
              return (
                <Flex
                  key={i}
                  justify={isUser ? "flex-end" : "flex-start"}
                  gap={12}
                  style={{ width: "100%" }}
                >
                  {!isUser && (
                    <Avatar
                      icon={<RobotOutlined />}
                      style={{
                        background: "linear-gradient(135deg, #0d9488, #0f766e)",
                        boxShadow: "0 2px 6px rgba(13, 148, 136, 0.25)",
                        flexShrink: 0,
                      }}
                      size={36}
                    />
                  )}
                  
                  <div
                    style={{
                      maxWidth: "80%",
                      display: "flex",
                      flexDirection: "column",
                      alignItems: isUser ? "flex-end" : "flex-start",
                    }}
                  >
                    <div
                      className={isUser ? "chat-bubble-user" : ""}
                      style={{
                        padding: "12px 18px",
                        borderRadius: isUser ? "16px 4px 16px 16px" : "4px 16px 16px 16px",
                        background: isUser ? undefined : token.colorBgContainer,
                        color: isUser ? "#fff" : token.colorText,
                        border: isUser ? "none" : `1px solid ${token.colorSplit}`,
                        boxShadow: isUser ? "0 4px 12px rgba(13, 148, 136, 0.15)" : "0 2px 8px rgba(0, 0, 0, 0.02)",
                        whiteSpace: "pre-wrap",
                        wordBreak: "break-word",
                        fontSize: 14,
                        lineHeight: 1.6,
                      }}
                    >
                      {/* DeepSeek/R1 reasoning style container */}
                      {!isUser && msg.thinking && (
                        <div
                          style={{
                            borderLeft: `3px solid ${token.colorPrimary}`,
                            paddingLeft: 12,
                            marginBottom: 12,
                            background: themeMode === "dark" ? "rgba(255,255,255,0.03)" : "rgba(0,0,0,0.015)",
                            borderRadius: "0 4px 4px 0",
                            padding: "8px 12px",
                          }}
                        >
                          <Flex align="center" gap={6} style={{ marginBottom: 4, color: token.colorPrimary, fontWeight: 500, fontSize: 12 }}>
                            <BulbOutlined />
                            <span>思考过程</span>
                          </Flex>
                          <Typography.Text type="secondary" italic style={{ fontSize: 13, lineHeight: 1.5, display: "block" }}>
                            {msg.thinking}
                          </Typography.Text>
                        </div>
                      )}

                      {/* Content block */}
                      <div style={{ position: "relative" }}>
                        {msg.content || (busy && i === msgs.length - 1 && !msg.thinking ? "正在输入中…" : "")}
                      </div>
                    </div>

                    {/* Copy option for assistant replies */}
                    {!isUser && msg.content && (
                      <div style={{ marginTop: 4, marginLeft: 4 }}>
                        <Button
                          type="text"
                          size="small"
                          icon={copiedId === i ? <CheckOutlined style={{ color: "#10b981" }} /> : <CopyOutlined />}
                          onClick={() => handleCopy(msg.content, i)}
                          style={{ fontSize: 11, color: token.colorTextSecondary, height: 22, padding: "0 4px" }}
                        >
                          {copiedId === i ? "已复制" : "复制"}
                        </Button>
                      </div>
                    )}
                  </div>

                  {isUser && (
                    <Avatar
                      icon={<UserOutlined />}
                      style={{
                        background: token.colorTextDescription,
                        flexShrink: 0,
                      }}
                      size={36}
                    />
                  )}
                </Flex>
              );
            })}
          </Flex>
        </div>
      </div>

      {/* Message Input Bottom Panel */}
      <div
        style={{
          borderTop: `1px solid ${token.colorSplit}`,
          background: token.colorBgContainer,
          padding: "16px 24px 24px 24px",
          boxShadow: "0 -2px 10px rgba(0, 0, 0, 0.02)",
        }}
      >
        <div style={{ maxWidth: 800, margin: "0 auto", width: "100%" }}>
          <div
            style={{
              display: "flex",
              gap: 8,
              alignItems: "flex-end",
              border: `1px solid ${inputFocused ? token.colorPrimary : token.colorBorder}`,
              boxShadow: inputFocused ? `0 0 0 3px rgba(15, 143, 131, 0.1)` : "none",
              borderRadius: 12,
              padding: "10px 12px",
              background: token.colorBgLayout,
              transition: "all 0.2s ease",
            }}
          >
            <Input.TextArea
              variant="borderless"
              autoSize={{ minRows: 1, maxRows: 6 }}
              value={prompt}
              placeholder="输入测试消息，Enter 发送，Shift+Enter 换行"
              onChange={(e) => setPrompt(e.target.value)}
              onFocus={() => setInputFocused(true)}
              onBlur={() => setInputFocused(false)}
              style={{
                padding: 0,
                fontSize: 14,
                border: "none",
                outline: "none",
                boxShadow: "none",
                background: "transparent",
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  send();
                }
              }}
            />
            <Button
              type="primary"
              icon={<SendOutlined />}
              loading={busy}
              disabled={(!isAuto && !node) || !prompt.trim()}
              onClick={() => send()}
              style={{
                borderRadius: 8,
                background: prompt.trim() && (isAuto || node) ? "linear-gradient(135deg, #0d9488, #0f766e)" : undefined,
                border: "none",
                height: 32,
                width: 36,
                display: "grid",
                placeItems: "center",
              }}
            />
          </div>
          <div style={{ display: "flex", justifyContent: "space-between", marginTop: 8, padding: "0 4px" }}>
            <Typography.Text type="secondary" style={{ fontSize: 11 }}>
              系统模式: <span style={{ fontWeight: 500, textTransform: "capitalize" }}>{mode === "default" ? "默认" : mode}</span>
            </Typography.Text>
            <Typography.Text type="secondary" style={{ fontSize: 11 }}>
              模型: <span style={{ fontWeight: 500 }} className="code-monospace">{model}</span>
            </Typography.Text>
          </div>
        </div>
      </div>
    </Flex>
  );
}

