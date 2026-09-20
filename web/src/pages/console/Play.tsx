import { useEffect, useRef, useState } from "react";
import { Alert, App, Button, Card, Empty, Flex, Input, Select, Typography, theme } from "antd";
import { DeleteOutlined, SendOutlined } from "@ant-design/icons";
import { userApi } from "../../api";
import { useAuth } from "../../auth";
import { PageHeader } from "../../components/PageHeader";
import type { GatewayToken, ShopInfo } from "../../types";

type ChatMsg = { role: "user" | "assistant"; content: string; thinking?: string };

export default function Play() {
  const { token } = theme.useToken();
  const { message } = App.useApp();
  const { me } = useAuth();
  const [shop, setShop] = useState<ShopInfo | null>(null);
  const [tokens, setTokens] = useState<GatewayToken[]>([]);
  const [tokenId, setTokenId] = useState("");
  const [model, setModel] = useState("");
  const [prompt, setPrompt] = useState("");
  const [msgs, setMsgs] = useState<ChatMsg[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const scroller = useRef<HTMLDivElement>(null);

  const current = tokens.find((x) => x.id === tokenId);
  const models = shop?.model_list || [];
  const aliases = me?.model_aliases || {};

  useEffect(() => {
    Promise.all([userApi.shop(), userApi.tokens()]).then(([s, t]) => {
      setShop(s);
      setTokens(t.tokens);
      const first = t.tokens.find((x) => x.enabled) || t.tokens[0];
      if (first) setTokenId(first.id);
    });
  }, []);

  useEffect(() => {
    if (model || !models.length) return;
    setModel(aliases[models[0]] || models[0]);
  }, [aliases, model, models]);

  useEffect(() => {
    if (scroller.current) scroller.current.scrollTop = scroller.current.scrollHeight;
  }, [msgs, busy]);

  async function send() {
    const text = prompt.trim();
    if (!text || busy || !current?.token || !model) return;
    setPrompt("");
    setBusy(true);
    setError("");
    setMsgs((m) => [...m, { role: "user", content: text }, { role: "assistant", content: "", thinking: "" }]);
    try {
      const res = await fetch("/v1/chat/completions", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${current.token}`,
        },
        body: JSON.stringify({
          model,
          stream: true,
          messages: [
            ...msgs.filter((m) => m.content).map((m) => ({ role: m.role, content: m.content })),
            { role: "user", content: text },
          ],
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
      setMsgs((cur) => {
        const last = cur[cur.length - 1];
        if (last && last.role === "assistant" && !last.content && !last.thinking) return cur.slice(0, -1);
        return cur;
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16, minHeight: "calc(100vh - 160px)" }}>
      <PageHeader title="调试" desc="用你自己的令牌打一枪，确认模型和余额都通。" />
      {!tokens.length && <Alert type="warning" showIcon message="先去「令牌」建一把钥匙，并且余额大于 0。" />}
      <Card bordered={false} className="premium-card" style={{ borderRadius: 12, flex: 1, display: "flex", flexDirection: "column" }}>
        <Flex gap={8} wrap="wrap" style={{ marginBottom: 12 }}>
          <Select
            value={tokenId || undefined}
            onChange={setTokenId}
            placeholder="选择令牌"
            style={{ minWidth: 180 }}
            options={tokens.map((t) => ({ value: t.id, label: t.name || "令牌" }))}
          />
          <Select
            showSearch
            value={model || undefined}
            onChange={setModel}
            placeholder="选择模型"
            style={{ minWidth: 220 }}
            options={models.map((m) => {
              const alias = aliases[m];
              return { value: alias || m, label: alias ? `${alias} → ${m}` : m };
            })}
          />
          {msgs.length > 0 && (
            <Button type="text" danger icon={<DeleteOutlined />} onClick={() => { setMsgs([]); setError(""); message.success("已清空"); }}>
              清空
            </Button>
          )}
        </Flex>
        {error && <Alert type="error" showIcon closable message={error} style={{ marginBottom: 12 }} />}
        <div
          ref={scroller}
          style={{
            flex: 1,
            minHeight: 320,
            maxHeight: 480,
            overflow: "auto",
            padding: 12,
            borderRadius: 8,
            background: token.colorBgLayout,
          }}
        >
          {msgs.length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="发一句「你好」试试" />
          ) : (
            msgs.map((msg, i) => (
              <div key={i} style={{ marginBottom: 12, textAlign: msg.role === "user" ? "right" : "left" }}>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {msg.role === "user" ? "我" : "助手"}
                </Typography.Text>
                {msg.thinking ? (
                  <Typography.Paragraph type="secondary" style={{ fontSize: 12, margin: "4px 0" }}>
                    {msg.thinking}
                  </Typography.Paragraph>
                ) : null}
                <Typography.Paragraph style={{ margin: "4px 0 0", whiteSpace: "pre-wrap" }}>{msg.content || (busy && i === msgs.length - 1 ? "…" : "")}</Typography.Paragraph>
              </div>
            ))
          )}
        </div>
        <Flex gap={8} style={{ marginTop: 12 }}>
          <Input
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            onPressEnter={() => send()}
            placeholder="输入消息"
            disabled={busy || !current}
          />
          <Button type="primary" icon={<SendOutlined />} loading={busy} onClick={send} style={{ background: "#0d9488", border: "none" }}>
            发送
          </Button>
        </Flex>
      </Card>
    </div>
  );
}
