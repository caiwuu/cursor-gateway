import { useEffect, useState, useMemo } from "react";
import {
  Alert,
  App,
  Button,
  Card,
  Col,
  Flex,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Row,
  Segmented,
  Select,
  Switch,
  Table,
  Tag,
  Typography,
  Tabs,
  Badge,
  Tooltip,
  theme,
  Empty,
  Divider,
} from "antd";
import {
  CheckCircleOutlined,
  CopyOutlined,
  DeleteOutlined,
  PlusOutlined,
  TeamOutlined,
  GiftOutlined,
  WalletOutlined,
  SettingOutlined,
  SearchOutlined,
  StopOutlined,
  UserOutlined,
  DollarOutlined,
  KeyOutlined,
  SafetyCertificateOutlined,
  SaveOutlined,
} from "@ant-design/icons";
import dayjs from "dayjs";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import { PageHeader } from "../components/PageHeader";
import { SettingRow } from "../components/SettingRow";
import type { GatewayUser, RedeemCard, AppSettings, ModelPrices } from "../types";
import { DEFAULT_MODEL_LIST } from "../types";
import { formatYuan } from "../format";

type TabKey = "users" | "cards" | "billing";
type CardFilter = "all" | "unused" | "used" | "disabled";

function cardStatus(row: RedeemCard): Exclude<CardFilter, "all"> {
  if (row.used_by) return "used";
  if (row.enabled === false) return "disabled";
  return "unused";
}

function fillPrices(
  list: string[],
  prices: ModelPrices | undefined,
  input: number,
  output: number,
): ModelPrices {
  const next: ModelPrices = {};
  for (const id of list) {
    const src = prices?.[id];
    next[id] = {
      input: src?.input ?? input,
      output: src?.output ?? output,
      official_input: src?.official_input ?? 0,
      official_output: src?.official_output ?? 0,
    };
  }
  return next;
}

export default function Users() {
  const { token } = theme.useToken();
  const { message } = App.useApp();
  const { me, logout } = useAuth();
  const nav = useNavigate();
  const [activeTab, setActiveTab] = useState<TabKey>("users");
  
  // Data States
  const [users, setUsers] = useState<GatewayUser[]>([]);
  const [cards, setCards] = useState<RedeemCard[]>([]);
  const [settings, setSettings] = useState<AppSettings | null>(null);
  
  // UI States
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  
  // Filters
  const [userQuery, setUserQuery] = useState("");
  const [cardQuery, setCardQuery] = useState("");
  const [cardFilter, setCardFilter] = useState<CardFilter>("all");
  const [selectedCardIds, setSelectedCardIds] = useState<string[]>([]);
  const [cardBusy, setCardBusy] = useState(false);
  
  // Modals
  const [creating, setCreating] = useState(false);
  const [recharging, setRecharging] = useState<GatewayUser | null>(null);
  const [resetting, setResetting] = useState<GatewayUser | null>(null);
  const [issuing, setIssuing] = useState(false);
  const [issued, setIssued] = useState<RedeemCard[]>([]);
  
  // Forms
  const [userForm] = Form.useForm<{ username: string; password: string; yuan: number; role: "admin" | "user" }>();
  const [rechargeForm] = Form.useForm<{ yuan: number; note: string }>();
  const [passwordForm] = Form.useForm<{ password: string; confirm: string }>();
  const [cardForm] = Form.useForm<{ count: number; yuan: number; note: string }>();
  const [billingForm] = Form.useForm();

  const load = () => {
    setError("");
    return Promise.all([api.users(), api.cards(), api.settings()])
      .then(([u, c, s]) => {
        setUsers(u.users);
        setCards(c.cards);
        setSettings(s);
        
        const list = s.model_list?.length ? s.model_list : DEFAULT_MODEL_LIST;
        const din = s.input_price_per_1m ?? 2;
        const dout = s.output_price_per_1m ?? 8;
        
        billingForm.setFieldsValue({
          allow_register: s.allow_register !== false,
          input_price_per_1m: din,
          output_price_per_1m: dout,
          model_prices: fillPrices(list, s.model_prices, din, dout),
        });
      })
      .catch((e) => setError(String((e as Error).message || e)))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load();
  }, []);

  async function copyText(value: string, ok = "已复制到剪贴板") {
    if (!value.trim()) {
      message.warning("没有可复制的卡密");
      return;
    }
    await navigator.clipboard.writeText(value);
    message.success(ok);
  }

  async function runCardAction(task: () => Promise<void>) {
    setCardBusy(true);
    try {
      await task();
      setSelectedCardIds([]);
      await load();
    } catch (e) {
      message.error(String((e as Error).message || e));
    } finally {
      setCardBusy(false);
    }
  }

  // Filtered Users
  const filteredUsers = useMemo(() => {
    const q = userQuery.trim().toLowerCase();
    if (!q) return users;
    return users.filter(
      (u) =>
        u.username.toLowerCase().includes(q) ||
        (u.role || "").toLowerCase().includes(q)
    );
  }, [users, userQuery]);

  // Filtered Cards
  const filteredCards = useMemo(() => {
    const q = cardQuery.trim().toLowerCase();
    return cards.filter((c) => {
      if (cardFilter !== "all" && cardStatus(c) !== cardFilter) return false;
      if (!q) return true;
      return (
        c.code.toLowerCase().includes(q) ||
        (c.note || "").toLowerCase().includes(q) ||
        (c.used_name || "").toLowerCase().includes(q)
      );
    });
  }, [cards, cardQuery, cardFilter]);

  const selectedCards = useMemo(
    () => filteredCards.filter((c) => selectedCardIds.includes(c.id)),
    [filteredCards, selectedCardIds],
  );

  // Computed Stats for Users Tab
  const userStats = useMemo(() => {
    const total = users.length;
    const totalBal = users.reduce((sum, u) => sum + (u.balance || 0), 0);
    const admins = users.filter((u) => u.role === "admin").length;
    return { total, totalBal, admins };
  }, [users]);

  // Computed Stats for Cards Tab
  const cardStats = useMemo(() => {
    const total = cards.length;
    const unused = cards.filter((c) => cardStatus(c) === "unused").length;
    const disabled = cards.filter((c) => cardStatus(c) === "disabled").length;
    const redeemed = cards.filter((c) => cardStatus(c) === "used").length;
    const totalAmt = cards.reduce((sum, c) => sum + (c.amount || 0), 0);
    return { total, unused, disabled, redeemed, totalAmt };
  }, [cards]);

  // Handle Save Billing settings
  async function saveBilling(values: any) {
    if (!settings) return;
    setError("");
    setBusy("save_billing");
    try {
      const list = settings.model_list?.length ? settings.model_list : DEFAULT_MODEL_LIST;
      const saved = await api.saveSettings({
        allow_register: values.allow_register !== false,
        input_price_per_1m: values.input_price_per_1m ?? 2,
        output_price_per_1m: values.output_price_per_1m ?? 8,
        model_prices: fillPrices(list, values.model_prices, values.input_price_per_1m ?? 2, values.output_price_per_1m ?? 8),
      });
      setSettings(saved);
      message.success("计价与注册设置已保存且立即生效");
    } catch (e) {
      setError(String((e as Error).message || e));
    } finally {
      setBusy("");
    }
  }

  // Active models list for Tab 3定价 table
  const modelPricingData = useMemo(() => {
    const list = settings?.model_list?.length ? settings.model_list : DEFAULT_MODEL_LIST;
    return list.map((id) => ({ id, key: id }));
  }, [settings]);

  // Header Actions
  const headerExtra = (
    <Flex gap={8}>
      {activeTab === "users" && (
        <Button
          type="primary"
          icon={<PlusOutlined />}
          onClick={() => setCreating(true)}
          style={{
            background: "linear-gradient(135deg, #0d9488, #0f766e)",
            border: "none",
            borderRadius: 8,
            fontWeight: 500,
          }}
        >
          创建用户
        </Button>
      )}
      {activeTab === "cards" && (
        <Button
          type="primary"
          icon={<GiftOutlined />}
          onClick={() => setIssuing(true)}
          style={{
            background: "linear-gradient(135deg, #0d9488, #0f766e)",
            border: "none",
            borderRadius: 8,
            fontWeight: 500,
          }}
        >
          发放充值卡密
        </Button>
      )}
      {activeTab === "billing" && (
        <Button
          type="primary"
          icon={<SaveOutlined />}
          loading={busy === "save_billing"}
          onClick={() => billingForm.submit()}
          style={{
            background: "linear-gradient(135deg, #0d9488, #0f766e)",
            border: "none",
            borderRadius: 8,
            fontWeight: 500,
          }}
        >
          保存计价规则
        </Button>
      )}
    </Flex>
  );

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <PageHeader
        title="用户与计价"
        desc="统一管理普通用户账户、充值卡密分发，以及各模型的具体百万 Token 扣费定价标准。"
        extra={headerExtra}
      />

      {error && <Alert type="error" showIcon closable message={error} style={{ borderRadius: 8, marginBottom: 8 }} />}

      <Tabs
        activeKey={activeTab}
        onChange={(k) => setActiveTab(k as TabKey)}
        type="line"
        size="middle"
        style={{ marginBottom: 0 }}
        items={[
          {
            key: "users",
            label: (
              <span>
                <TeamOutlined />
                用户管理
              </span>
            ),
            children: (
              <Flex vertical gap={16} style={{ marginTop: 8 }}>
                {/* Stats Cards Row */}
                <Flex gap={12} wrap="wrap">
                  <Card style={{ flex: "1 1 200px", borderRadius: 10 }} size="small" styles={{ body: { padding: "12px 16px" } }}>
                    <Flex align="center" gap={12}>
                      <span style={{ fontSize: 24, padding: 8, background: `${token.colorPrimaryBg}`, borderRadius: 8, color: token.colorPrimary }}>
                        <UserOutlined />
                      </span>
                      <div>
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>用户总数</Typography.Text>
                        <div style={{ fontSize: 18, fontWeight: 600 }}>{loading ? "…" : userStats.total} 人</div>
                      </div>
                    </Flex>
                  </Card>
                  <Card style={{ flex: "1 1 200px", borderRadius: 10 }} size="small" styles={{ body: { padding: "12px 16px" } }}>
                    <Flex align="center" gap={12}>
                      <span style={{ fontSize: 24, padding: 8, background: `${token.colorPrimaryBg}`, borderRadius: 8, color: token.colorPrimary }}>
                        <DollarOutlined />
                      </span>
                      <div>
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>总余额</Typography.Text>
                        <div style={{ fontSize: 18, fontWeight: 600, color: token.colorPrimary }}>{loading ? "…" : formatYuan(userStats.totalBal)}</div>
                      </div>
                    </Flex>
                  </Card>
                  <Card style={{ flex: "1 1 200px", borderRadius: 10 }} size="small" styles={{ body: { padding: "12px 16px" } }}>
                    <Flex align="center" gap={12}>
                      <span style={{ fontSize: 24, padding: 8, background: `${token.colorPrimaryBg}`, borderRadius: 8, color: token.colorPrimary }}>
                        <SafetyCertificateOutlined />
                      </span>
                      <div>
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>管理员数量</Typography.Text>
                        <div style={{ fontSize: 18, fontWeight: 600 }}>{loading ? "…" : userStats.admins} 人</div>
                      </div>
                    </Flex>
                  </Card>
                </Flex>

                {/* Users List Card */}
                <Card
                  bordered={false}
                  className="premium-card"
                  style={{ borderRadius: 10 }}
                  styles={{ body: { padding: 0 } }}
                >
                  <Flex justify="space-between" align="center" style={{ padding: "12px 16px" }} gap={12} wrap="wrap">
                    <Input
                      allowClear
                      prefix={<SearchOutlined style={{ color: token.colorTextQuaternary }} />}
                      placeholder="搜索用户名或角色…"
                      value={userQuery}
                      onChange={(e) => setUserQuery(e.target.value)}
                      style={{ maxWidth: 280 }}
                    />
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      已加载 {filteredUsers.length} 项用户数据
                    </Typography.Text>
                  </Flex>

                  <Table
                    rowKey="id"
                    loading={loading}
                    dataSource={filteredUsers}
                    pagination={filteredUsers.length > 15 ? { pageSize: 15, showSizeChanger: false } : false}
                    scroll={{ x: 1080 }}
                    locale={{
                      emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="未找到匹配的用户" />,
                    }}
                    columns={[
                      {
                        title: "用户名",
                        dataIndex: "username",
                        render: (v: string) => (
                          <Flex align="center" gap={8}>
                            <UserOutlined style={{ color: token.colorTextDescription }} />
                            <Typography.Text strong>{v}</Typography.Text>
                          </Flex>
                        ),
                      },
                      {
                        title: "角色权限",
                        width: 150,
                        render: (_: unknown, row: GatewayUser) => (
                          <Select
                            size="small"
                            value={row.role || "user"}
                            style={{ width: 110 }}
                            onChange={async (role) => {
                              await api.updateUser(row.id, { role });
                              message.success(`已更新 ${row.username} 为 ${role === "admin" ? "管理员" : "普通用户"}`);
                              await load();
                            }}
                            options={[
                              { value: "admin", label: "管理员" },
                              { value: "user", label: "普通用户" },
                            ]}
                          />
                        ),
                      },
                      {
                        title: "账户余额",
                        dataIndex: "balance",
                        width: 140,
                        render: (v: number) => (
                          <Typography.Text className="code-monospace" style={{ fontWeight: 600, color: v > 0 ? token.colorPrimary : token.colorTextDescription }}>
                            {formatYuan(v)}
                          </Typography.Text>
                        ),
                      },
                      {
                        title: "API 令牌数",
                        dataIndex: "token_count",
                        width: 120,
                        align: "center",
                        render: (v: number) => <Badge count={v} showZero overflowCount={999} style={{ backgroundColor: token.colorBorderSecondary, color: token.colorTextSecondary }} />,
                      },
                      {
                        title: "账号状态",
                        width: 110,
                        render: (_: unknown, row: GatewayUser) => (
                          <Switch
                            checked={row.enabled}
                            size="small"
                            checkedChildren="启用"
                            unCheckedChildren="封禁"
                            onChange={async (on) => {
                              await api.updateUser(row.id, { enabled: on });
                              message.success(`${row.username} 已${on ? "解锁并启用" : "停锁并封禁"}`);
                              await load();
                            }}
                          />
                        ),
                      },
                      {
                        title: "创建时间",
                        width: 170,
                        render: (_: unknown, row: GatewayUser) => (
                          <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                            {row.created_at ? dayjs.unix(row.created_at).format("YYYY-MM-DD HH:mm") : "—"}
                          </Typography.Text>
                        ),
                      },
                      {
                        title: "操作",
                        width: 220,
                        align: "center",
                        render: (_: unknown, row: GatewayUser) => (
                          <Flex justify="center" gap={0}>
                            <Button
                              type="link"
                              size="small"
                              icon={<KeyOutlined />}
                              onClick={() => {
                                setResetting(row);
                                passwordForm.resetFields();
                              }}
                            >
                              改密码
                            </Button>
                            <Button
                              type="link"
                              size="small"
                              onClick={() => {
                                setRecharging(row);
                                rechargeForm.setFieldsValue({
                                  yuan: Number(row.balance_yuan || 0),
                                  note: "",
                                });
                              }}
                            >
                              改余额
                            </Button>
                            <Popconfirm
                              title={`确定删除用户 ${row.username}？`}
                              description="将同时删除该用户的登录态、API 令牌和账本，不可恢复。"
                              okText="删除"
                              cancelText="取消"
                              okButtonProps={{ danger: true }}
                              onConfirm={async () => {
                                await api.deleteUser(row.id);
                                message.success("已删除");
                                if (me?.id === row.id) {
                                  await logout();
                                  nav("/login", { replace: true });
                                  return;
                                }
                                await load();
                              }}
                            >
                              <Button type="link" size="small" danger>
                                删除
                              </Button>
                            </Popconfirm>
                          </Flex>
                        ),
                      },
                    ]}
                  />
                </Card>
              </Flex>
            ),
          },
          {
            key: "cards",
            label: (
              <span>
                <GiftOutlined />
                充值卡密
              </span>
            ),
            children: (
              <Flex vertical gap={16} style={{ marginTop: 8 }}>
                {/* Cards Stats Row */}
                <Flex gap={12} wrap="wrap">
                  <Card style={{ flex: "1 1 180px", borderRadius: 10 }} size="small" styles={{ body: { padding: "12px 16px" } }}>
                    <Flex align="center" gap={12}>
                      <span style={{ fontSize: 24, padding: 8, background: `${token.colorPrimaryBg}`, borderRadius: 8, color: token.colorPrimary }}>
                        <GiftOutlined />
                      </span>
                      <div>
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>总发行卡密</Typography.Text>
                        <div style={{ fontSize: 18, fontWeight: 600 }}>{loading ? "…" : cardStats.total} 张</div>
                      </div>
                    </Flex>
                  </Card>
                  <Card style={{ flex: "1 1 180px", borderRadius: 10 }} size="small" styles={{ body: { padding: "12px 16px" } }}>
                    <Flex align="center" gap={12}>
                      <span style={{ fontSize: 24, padding: 8, background: "#f0fdf4", borderRadius: 8, color: "#16a34a" }}>
                        <Badge status="success" />
                      </span>
                      <div>
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>未使用卡密</Typography.Text>
                        <div style={{ fontSize: 18, fontWeight: 600, color: "#16a34a" }}>{loading ? "…" : cardStats.unused} 张</div>
                      </div>
                    </Flex>
                  </Card>
                  <Card style={{ flex: "1 1 180px", borderRadius: 10 }} size="small" styles={{ body: { padding: "12px 16px" } }}>
                    <Flex align="center" gap={12}>
                      <span style={{ fontSize: 24, padding: 8, background: "#fafafa", borderRadius: 8, color: token.colorTextDescription }}>
                        <Badge status="default" />
                      </span>
                      <div>
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>已兑换卡密</Typography.Text>
                        <div style={{ fontSize: 18, fontWeight: 600, color: token.colorTextDescription }}>{loading ? "…" : cardStats.redeemed} 张</div>
                      </div>
                    </Flex>
                  </Card>
                  <Card style={{ flex: "1 1 180px", borderRadius: 10 }} size="small" styles={{ body: { padding: "12px 16px" } }}>
                    <Flex align="center" gap={12}>
                      <span style={{ fontSize: 24, padding: 8, background: `${token.colorPrimaryBg}`, borderRadius: 8, color: token.colorPrimary }}>
                        <WalletOutlined />
                      </span>
                      <div>
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>流通总面额</Typography.Text>
                        <div style={{ fontSize: 18, fontWeight: 600 }}>{loading ? "…" : formatYuan(cardStats.totalAmt)}</div>
                      </div>
                    </Flex>
                  </Card>
                </Flex>

                {/* RedeemCards Table */}
                <Card
                  bordered={false}
                  className="premium-card"
                  style={{ borderRadius: 10 }}
                  styles={{ body: { padding: 0 } }}
                >
                  <Flex justify="space-between" align="center" style={{ padding: "12px 16px" }} gap={12} wrap="wrap">
                    <Flex align="center" gap={12} wrap="wrap" style={{ flex: 1, minWidth: 0 }}>
                      <Input
                        allowClear
                        prefix={<SearchOutlined style={{ color: token.colorTextQuaternary }} />}
                        placeholder="搜索卡密、兑换用户或备注…"
                        value={cardQuery}
                        onChange={(e) => setCardQuery(e.target.value)}
                        style={{ width: 260 }}
                      />
                      <Segmented<CardFilter>
                        value={cardFilter}
                        onChange={(v) => {
                          setCardFilter(v);
                          setSelectedCardIds([]);
                        }}
                        options={[
                          { label: `全部 ${cardStats.total}`, value: "all" },
                          { label: `未使用 ${cardStats.unused}`, value: "unused" },
                          { label: `已停用 ${cardStats.disabled}`, value: "disabled" },
                          { label: `已兑换 ${cardStats.redeemed}`, value: "used" },
                        ]}
                      />
                    </Flex>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      已选 {selectedCardIds.length} / {filteredCards.length}
                    </Typography.Text>
                  </Flex>

                  <Flex
                    align="center"
                    gap={8}
                    wrap="wrap"
                    style={{
                      padding: "0 16px 12px",
                      borderBottom: `1px solid ${token.colorSplit}`,
                    }}
                  >
                    <Button
                      size="small"
                      icon={<CopyOutlined />}
                      disabled={!selectedCards.length}
                      onClick={() => copyText(selectedCards.map((c) => c.code).join("\n"), `已复制 ${selectedCards.length} 张`)}
                    >
                      复制已选
                    </Button>
                    <Button
                      size="small"
                      icon={<CopyOutlined />}
                      disabled={!filteredCards.length}
                      onClick={() => copyText(filteredCards.map((c) => c.code).join("\n"), `已复制当前筛选 ${filteredCards.length} 张`)}
                    >
                      复制当前筛选
                    </Button>
                    <Button
                      size="small"
                      icon={<CheckCircleOutlined />}
                      disabled={!selectedCards.some((c) => !c.used_by)}
                      loading={cardBusy}
                      onClick={() =>
                        runCardAction(async () => {
                          const ids = selectedCards.filter((c) => !c.used_by).map((c) => c.id);
                          if (!ids.length) return;
                          await api.updateCards(ids, true);
                          message.success(`已启用 ${ids.length} 张`);
                        })
                      }
                    >
                      批量启用
                    </Button>
                    <Button
                      size="small"
                      icon={<StopOutlined />}
                      disabled={!selectedCards.some((c) => !c.used_by)}
                      loading={cardBusy}
                      onClick={() =>
                        runCardAction(async () => {
                          const ids = selectedCards.filter((c) => !c.used_by).map((c) => c.id);
                          if (!ids.length) return;
                          await api.updateCards(ids, false);
                          message.success(`已停用 ${ids.length} 张`);
                        })
                      }
                    >
                      批量停用
                    </Button>
                    <Popconfirm
                      title={`删除选中的 ${selectedCardIds.length} 张卡密？`}
                      description="已兑换记录也会从列表移除，不会回退用户余额。"
                      disabled={!selectedCardIds.length}
                      onConfirm={() =>
                        runCardAction(async () => {
                          await api.deleteCards(selectedCardIds);
                          message.success("已删除所选卡密");
                        })
                      }
                    >
                      <Button size="small" danger icon={<DeleteOutlined />} disabled={!selectedCardIds.length} loading={cardBusy}>
                        批量删除
                      </Button>
                    </Popconfirm>
                  </Flex>

                  <Table
                    rowKey="id"
                    loading={loading || cardBusy}
                    dataSource={filteredCards}
                    rowSelection={{
                      selectedRowKeys: selectedCardIds,
                      onChange: (keys) => setSelectedCardIds(keys.map(String)),
                    }}
                    pagination={
                      filteredCards.length > 15
                        ? { pageSize: 15, showSizeChanger: false, showTotal: (t) => `共 ${t} 张` }
                        : false
                    }
                    scroll={{ x: 980 }}
                    locale={{
                      emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有匹配的卡密" />,
                    }}
                    columns={[
                      {
                        title: "充值卡密",
                        dataIndex: "code",
                        render: (v: string) => (
                          <Flex align="center" gap={6}>
                            <Typography.Text className="code-monospace" style={{ fontSize: 13, letterSpacing: "0.02em" }}>
                              {v}
                            </Typography.Text>
                            <Tooltip title="复制卡密">
                              <Button size="small" type="text" icon={<CopyOutlined />} onClick={() => copyText(v)} />
                            </Tooltip>
                          </Flex>
                        ),
                      },
                      {
                        title: "面额",
                        dataIndex: "amount",
                        width: 110,
                        render: (v: number) => (
                          <Typography.Text className="code-monospace" style={{ fontWeight: 600 }}>
                            {formatYuan(v)}
                          </Typography.Text>
                        ),
                      },
                      {
                        title: "备注",
                        dataIndex: "note",
                        ellipsis: true,
                        render: (v: string) => <Typography.Text type={v ? undefined : "secondary"}>{v || "—"}</Typography.Text>,
                      },
                      {
                        title: "状态",
                        width: 210,
                        render: (_: unknown, row: RedeemCard) => {
                          if (row.used_by) {
                            return (
                              <Tag color="default" style={{ borderRadius: 4 }}>
                                已兑 {row.used_name || ""}
                              </Tag>
                            );
                          }
                          return (
                            <Flex align="center" gap={8}>
                              <Switch
                                size="small"
                                checked={row.enabled}
                                checkedChildren="启用"
                                unCheckedChildren="停用"
                                onChange={(on) =>
                                  runCardAction(async () => {
                                    await api.updateCard(row.id, { enabled: on });
                                    message.success(on ? "已启用" : "已停用");
                                  })
                                }
                              />
                              <Tag color={row.enabled ? "success" : "warning"} style={{ borderRadius: 4, margin: 0 }}>
                                {row.enabled ? "未使用" : "已停用"}
                              </Tag>
                            </Flex>
                          );
                        },
                      },
                      {
                        title: "时间",
                        width: 160,
                        render: (_: unknown, row: RedeemCard) => (
                          <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                            {row.used_at
                              ? dayjs.unix(row.used_at).format("YYYY-MM-DD HH:mm")
                              : row.created_at
                              ? dayjs.unix(row.created_at).format("YYYY-MM-DD HH:mm")
                              : "—"}
                          </Typography.Text>
                        ),
                      },
                      {
                        title: "操作",
                        width: 80,
                        render: (_: unknown, row: RedeemCard) => (
                          <Popconfirm
                            title="确定删除这张卡密？"
                            description={row.used_by ? "已兑换记录会从列表移除，不会回退余额。" : "删除后无法再兑换。"}
                            onConfirm={() =>
                              runCardAction(async () => {
                                await api.deleteCard(row.id);
                                message.success("已删除");
                              })
                            }
                          >
                            <Button size="small" type="link" danger>
                              删除
                            </Button>
                          </Popconfirm>
                        ),
                      },
                    ]}
                  />
                </Card>
              </Flex>
            ),
          },
          {
            key: "billing",
            label: (
              <span>
                <SettingOutlined />
                计价与注册设置
              </span>
            ),
            children: (
              <Flex vertical gap={16} style={{ marginTop: 8 }}>
                <Card loading={loading} style={{ borderRadius: 10 }}>
                  <Form form={billingForm} layout="vertical" requiredMark={false} onFinish={saveBilling}>
                    <Typography.Title level={5} style={{ margin: "0 0 16px" }}>注册选项</Typography.Title>
                    <SettingRow title="开放用户自主注册" desc="关闭后控制台首页将关闭新用户注册注册链接，仅可由管理员在后台手动“开户”创建。">
                      <Form.Item name="allow_register" valuePropName="checked" noStyle>
                        <Switch checkedChildren="开启自主注册" unCheckedChildren="关闭注册" />
                      </Form.Item>
                    </SettingRow>

                    <Divider style={{ margin: "20px 0" }} />

                    <Typography.Title level={5} style={{ margin: "0 0 4px" }}>默认兜底计价 (每百万 Token)</Typography.Title>
                    <Typography.Paragraph type="secondary" style={{ fontSize: 13, marginBottom: 16 }}>
                      当某个模型未在下方“按模型定价表”进行针对性独立标价时，将默认套用此默认单价进行计费扣款。
                    </Typography.Paragraph>

                    <Flex gap={24} wrap="wrap">
                      <div style={{ flex: "1 1 200px", maxWidth: 300 }}>
                        <Form.Item name="input_price_per_1m" label="默认输入单价 (元 / 100万 Tokens)" rules={[{ required: true }]}>
                          <InputNumber min={0} step={0.1} style={{ width: "100%" }} addonAfter="元" />
                        </Form.Item>
                      </div>
                      <div style={{ flex: "1 1 200px", maxWidth: 300 }}>
                        <Form.Item name="output_price_per_1m" label="默认输出单价 (元 / 100万 Tokens)" rules={[{ required: true }]}>
                          <InputNumber min={0} step={0.1} style={{ width: "100%" }} addonAfter="元" />
                        </Form.Item>
                      </div>
                    </Flex>

                    <Divider style={{ margin: "20px 0" }} />

                    <Flex justify="space-between" align="baseline" style={{ marginBottom: 12 }}>
                      <div>
                        <Typography.Title level={5} style={{ margin: 0 }}>按模型定价</Typography.Title>
                        <Typography.Paragraph type="secondary" style={{ fontSize: 13, margin: "4px 0 0" }}>
                          本站单价用于实际扣费；未单独填写时使用上方默认单价。官方价仅作公开页对照，不参与扣费，留空则不展示对比。
                        </Typography.Paragraph>
                      </div>
                    </Flex>

                    <Table
                      rowKey="id"
                      dataSource={modelPricingData}
                      pagination={false}
                      size="middle"
                      scroll={{ x: 1080 }}
                      columns={[
                        {
                          title: "模型",
                          dataIndex: "id",
                          width: 220,
                          render: (v: string) => (
                            <Typography.Text className="code-monospace" strong style={{ fontSize: 13 }}>
                              {v}
                            </Typography.Text>
                          ),
                        },
                        {
                          title: "本站单价 / 百万 tokens",
                          children: [
                            {
                              title: "输入",
                              width: 180,
                              render: (_: unknown, record: { id: string }) => (
                                <Form.Item name={["model_prices", record.id, "input"]} noStyle>
                                  <InputNumber min={0} step={0.1} style={{ width: "100%" }} addonAfter="元" />
                                </Form.Item>
                              ),
                            },
                            {
                              title: "输出",
                              width: 180,
                              render: (_: unknown, record: { id: string }) => (
                                <Form.Item name={["model_prices", record.id, "output"]} noStyle>
                                  <InputNumber min={0} step={0.1} style={{ width: "100%" }} addonAfter="元" />
                                </Form.Item>
                              ),
                            },
                          ],
                        },
                        {
                          title: "官方参考价 / 百万 tokens",
                          children: [
                            {
                              title: "输入",
                              width: 180,
                              render: (_: unknown, record: { id: string }) => (
                                <Form.Item name={["model_prices", record.id, "official_input"]} noStyle>
                                  <InputNumber min={0} step={0.1} style={{ width: "100%" }} addonAfter="元" placeholder="选填" />
                                </Form.Item>
                              ),
                            },
                            {
                              title: "输出",
                              width: 180,
                              render: (_: unknown, record: { id: string }) => (
                                <Form.Item name={["model_prices", record.id, "official_output"]} noStyle>
                                  <InputNumber min={0} step={0.1} style={{ width: "100%" }} addonAfter="元" placeholder="选填" />
                                </Form.Item>
                              ),
                            },
                          ],
                        },
                      ]}
                    />
                  </Form>
                </Card>
              </Flex>
            ),
          },
        ]}
      />

      {/* Modal: 创建新用户 */}
      <Modal
        title={
          <Flex align="center" gap={8}>
            <UserOutlined style={{ color: token.colorPrimary }} />
            <span>管理员开户</span>
          </Flex>
        }
        open={creating}
        onCancel={() => setCreating(false)}
        onOk={() => userForm.submit()}
        okText="确认开户"
        cancelText="取消"
        destroyOnClose
      >
        <div style={{ height: 10 }} />
        <Form
          form={userForm}
          layout="vertical"
          initialValues={{ yuan: 0, role: "user" }}
          requiredMark={false}
          onFinish={async (values) => {
            try {
              await api.createUser(values);
              setCreating(false);
              userForm.resetFields();
              message.success("用户账户开户成功");
              await load();
            } catch (e) {
              message.error(String((e as Error).message || e));
            }
          }}
        >
          <Form.Item name="username" label="用户名 (Username)" rules={[{ required: true, message: "请填写用户名" }]}>
            <Input placeholder="建议 2–32 位英文字符或数字" />
          </Form.Item>
          <Form.Item
            name="password"
            label="设置登录密码"
            rules={[
              { required: true, message: "请设置初始登录密码" },
              { min: 6, message: "密码安全级别过低，请至少输入 6 位" },
            ]}
          >
            <Input.Password placeholder="至少 6 位字符" />
          </Form.Item>
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="role" label="角色权限分配">
                <Select
                  options={[
                    { value: "user", label: "普通用户" },
                    { value: "admin", label: "管理员" },
                  ]}
                />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="yuan" label="赠送初始余额">
                <InputNumber min={0} step={1} style={{ width: "100%" }} addonAfter="元" />
              </Form.Item>
            </Col>
          </Row>
        </Form>
      </Modal>

      {/* Modal: 修改登录密码 */}
      <Modal
        title={resetting ? `修改 ${resetting.username} 的登录密码` : "修改登录密码"}
        open={!!resetting}
        onCancel={() => setResetting(null)}
        onOk={() => passwordForm.submit()}
        okText="保存密码"
        cancelText="取消"
        destroyOnClose
      >
        <div style={{ height: 10 }} />
        <Form
          form={passwordForm}
          layout="vertical"
          requiredMark={false}
          onFinish={async (values) => {
            if (!resetting) return;
            try {
              await api.updateUser(resetting.id, { password: values.password });
              setResetting(null);
              message.success(`已更新 ${resetting.username} 的密码`);
            } catch (e) {
              message.error(String((e as Error).message || e));
            }
          }}
        >
          <Form.Item
            name="password"
            label="新密码"
            rules={[
              { required: true, message: "请填写新密码" },
              { min: 6, message: "至少 6 位" },
            ]}
          >
            <Input.Password autoComplete="new-password" placeholder="至少 6 位" autoFocus />
          </Form.Item>
          <Form.Item
            name="confirm"
            label="确认新密码"
            dependencies={["password"]}
            rules={[
              { required: true, message: "请再输入一次" },
              ({ getFieldValue }) => ({
                validator(_, value) {
                  if (!value || getFieldValue("password") === value) return Promise.resolve();
                  return Promise.reject(new Error("两次输入的密码不一致"));
                },
              }),
            ]}
          >
            <Input.Password autoComplete="new-password" placeholder="再输入一次" />
          </Form.Item>
        </Form>
      </Modal>

      {/* Modal: 修改用户余额 */}
      <Modal
        title={recharging ? `修改 ${recharging.username} 的余额` : "修改余额"}
        open={!!recharging}
        onCancel={() => setRecharging(null)}
        onOk={() => rechargeForm.submit()}
        okText="保存余额"
        cancelText="取消"
        destroyOnClose
      >
        <div style={{ height: 10 }} />
        {recharging ? (
          <Typography.Paragraph type="secondary" style={{ marginBottom: 12 }}>
            当前余额 {formatYuan(recharging.balance)}。填写目标余额即可上调或下调，差额会记入账本。
          </Typography.Paragraph>
        ) : null}
        <Form
          form={rechargeForm}
          layout="vertical"
          requiredMark={false}
          onFinish={async (values) => {
            if (!recharging) return;
            try {
              await api.setUserBalance(recharging.id, values.yuan, values.note);
              setRecharging(null);
              message.success(`已将 ${recharging.username} 的余额设为 ¥${Number(values.yuan).toFixed(4)}`);
              await load();
            } catch (e) {
              message.error(String((e as Error).message || e));
            }
          }}
        >
          <Form.Item name="yuan" label="目标余额" rules={[{ required: true, message: "请填写余额" }]}>
            <InputNumber min={0} step={1} style={{ width: "100%" }} addonAfter="元" autoFocus />
          </Form.Item>
          <Form.Item name="note" label="备注">
            <Input placeholder="例如：纠错 / 补偿 / 人工扣减" />
          </Form.Item>
        </Form>
      </Modal>

      {/* Modal: 批量发卡密 */}
      <Modal
        title={
          <Flex align="center" gap={8}>
            <GiftOutlined style={{ color: token.colorPrimary }} />
            <span>批量发放充值卡密</span>
          </Flex>
        }
        open={issuing}
        onCancel={() => setIssuing(false)}
        onOk={() => cardForm.submit()}
        okText="立即生成并导出"
        cancelText="取消"
        destroyOnClose
      >
        <div style={{ height: 10 }} />
        <Form
          form={cardForm}
          layout="vertical"
          initialValues={{ count: 5, yuan: 10 }}
          requiredMark={false}
          onFinish={async (values) => {
            try {
              const res = await api.createCards(values.count, values.yuan, values.note);
              setIssued(res.cards);
              setIssuing(false);
              cardForm.resetFields();
              message.success(`成功批量生成 ${res.cards.length} 张充值卡卡密`);
              await load();
            } catch (e) {
              message.error(String((e as Error).message || e));
            }
          }}
        >
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="count" label="本次发行张数" rules={[{ required: true, message: "请填写发行张数" }]}>
                <InputNumber min={1} max={100} style={{ width: "100%" }} addonAfter="张" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="yuan" label="每张卡充值额度" rules={[{ required: true, message: "请填写卡面额度" }]}>
                <InputNumber min={0.01} step={10} style={{ width: "100%" }} addonAfter="元" />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item name="note" label="卡批次备注/渠道标记">
            <Input placeholder="例如：淘宝 10元套餐 / Q3社群赠品" />
          </Form.Item>
        </Form>
      </Modal>

      {/* Modal: 展示生成的卡密并提供一键复制 */}
      <Modal
        title="充值卡密批量生成完毕"
        open={issued.length > 0}
        onCancel={() => setIssued([])}
        width={480}
        footer={[
          <Button key="close" onClick={() => setIssued([])}>
            关闭
          </Button>,
          <Button
            key="copy"
            type="primary"
            icon={<CopyOutlined />}
            onClick={() => copyText(issued.map((c) => `${c.code}\t${c.amount}元`).join("\n"))}
            style={{ background: "linear-gradient(135deg, #0d9488, #0f766e)", border: "none" }}
          >
            复制全部 (带金额)
          </Button>,
        ]}
      >
        <div style={{ height: 10 }} />
        <Alert
          type="success"
          showIcon
          message="卡密生成成功！请发给买家或分销渠道。一次性兑换后立即失效。"
          style={{ marginBottom: 16, borderRadius: 6 }}
        />
        <div style={{ maxHeight: 280, overflowY: "auto", border: `1px solid ${token.colorSplit}`, padding: 12, borderRadius: 8 }}>
          {issued.map((c) => (
            <Flex key={c.id} justify="space-between" align="center" style={{ marginBottom: 8, paddingBottom: 6, borderBottom: `1px dashed ${token.colorSplit}` }}>
              <Typography.Paragraph className="code-monospace" copyable={{ text: c.code }} style={{ margin: 0, fontSize: 13, fontWeight: 500 }}>
                {c.code}
              </Typography.Paragraph>
              <Typography.Text type="success" strong className="code-monospace">
                {formatYuan(c.amount)}
              </Typography.Text>
            </Flex>
          ))}
        </div>
      </Modal>
    </div>
  );
}
