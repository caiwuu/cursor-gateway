import { useEffect, useState } from "react";
import { Alert, App, Button, Card, Divider, Form, Input, InputNumber, Select, Switch, Typography, Flex, theme } from "antd";
import {
  SlidersOutlined,
  InfoCircleOutlined,
  SaveOutlined,
  DatabaseOutlined,
  AppstoreOutlined,
} from "@ant-design/icons";
import { api } from "../api";
import { useAuth } from "../auth";
import { PageHeader } from "../components/PageHeader";
import { SettingRow } from "../components/SettingRow";
import {
  DEFAULT_PRODUCT_NAME,
  DEFAULT_MODEL_LIST,
  MODE_HINT,
  MODE_LABEL,
  MODES,
  defaultModeConfig,
  enabledModesOf,
  normalizeModeConfig,
  type AppMeta,
  type AppSettings,
  type Mode,
  type ModeConfig,
  type ModelPrices,
} from "../types";

type SettingsForm = Pick<
  AppSettings,
  | "host"
  | "port"
  | "shop_url"
  | "product_name"
  | "model_list"
  | "mode_defaults"
>;

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

export default function Settings() {
  const { token } = theme.useToken();
  const { message } = App.useApp();
  const { refresh } = useAuth();
  const [meta, setMeta] = useState<AppMeta | null>(null);
  const [form] = Form.useForm<SettingsForm>();
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const modelList = Form.useWatch("model_list", form) || DEFAULT_MODEL_LIST;
  const modeDefaults = Form.useWatch("mode_defaults", form) as ModeConfig | undefined;

  useEffect(() => {
    Promise.all([api.meta(), api.settings()])
      .then(([m, s]) => {
        setMeta(m);
        const list = s.model_list?.length ? s.model_list : DEFAULT_MODEL_LIST;
        form.setFieldsValue({
          host: s.host,
          port: s.port,
          shop_url: s.shop_url || "",
          product_name: s.product_name || DEFAULT_PRODUCT_NAME,
          model_list: list,
          mode_defaults: normalizeModeConfig(s.mode_defaults, list),
        });
      })
      .catch((e) => setError(String(e.message || e)))
      .finally(() => setLoading(false));
  }, [form]);

  function pruneModeDefaults(list: string[]) {
    const current = form.getFieldValue("mode_defaults") as ModeConfig | undefined;
    form.setFieldsValue({
      model_list: list,
      mode_defaults: normalizeModeConfig(current, list, defaultModeConfig(list)),
    });
  }

  function setModeEnabled(mode: Mode, on: boolean) {
    const list = form.getFieldValue("model_list") || DEFAULT_MODEL_LIST;
    const next = normalizeModeConfig(form.getFieldValue("mode_defaults"), list);
    next[mode] = {
      enabled: on,
      models: on && !next[mode].models.length ? [...list] : next[mode].models,
    };
    form.setFieldsValue({ mode_defaults: next });
  }

  async function save(values: SettingsForm) {
    setError("");
    try {
      const list = values.model_list?.length ? values.model_list : DEFAULT_MODEL_LIST;
      const defaults = normalizeModeConfig(values.mode_defaults, list);
      const on = enabledModesOf(defaults);
      if (!on.length) {
        setError("至少启用一种推理模式");
        return;
      }
      for (const mode of on) {
        if (!defaults[mode].models.length) {
          setError(`${MODE_LABEL[mode]} 已启用，请至少选择一个默认模型`);
          return;
        }
      }
      const saved = await api.saveSettings({
        host: values.host,
        port: values.port,
        shop_url: values.shop_url || "",
        product_name: values.product_name || DEFAULT_PRODUCT_NAME,
        model_list: list,
        mode_defaults: defaults,
      });
      form.setFieldsValue({
        host: saved.host,
        port: saved.port,
        shop_url: saved.shop_url || "",
        product_name: saved.product_name || DEFAULT_PRODUCT_NAME,
        model_list: saved.model_list,
        mode_defaults: saved.mode_defaults,
      });
      await refresh();
      message.success("已保存。模型列表和商店地址立即生效；监听地址需重启后端后生效。");
    } catch (e) {
      setError(String((e as Error).message || e));
    }
  }

  return (
    <div style={{ maxWidth: 840, margin: "0 auto", width: "100%", display: "flex", flexDirection: "column", gap: 20 }}>
      <PageHeader
        title="系统设置"
        desc="模型列表和各模式默认项给新建节点用。对外分发请去「令牌」页。"
        extra={
          <Button
            type="primary"
            icon={<SaveOutlined />}
            style={{
              background: "linear-gradient(135deg, #0d9488, #0f766e)",
              border: "none",
              boxShadow: "0 4px 12px rgba(13, 148, 136, 0.15)",
              borderRadius: 8,
              fontWeight: 600,
            }}
            onClick={() => form.submit()}
          >
            保存配置
          </Button>
        }
      />

      {error && (
        <Alert
          type="error"
          showIcon
          closable
          message="配置错误"
          description={error}
          style={{ borderRadius: 10 }}
        />
      )}

      <Form
        form={form}
        onFinish={save}
        requiredMark={false}
        initialValues={{
          model_list: DEFAULT_MODEL_LIST,
          mode_defaults: defaultModeConfig(),
          allow_register: true,
          input_price_per_1m: 2,
          output_price_per_1m: 8,
          model_prices: fillPrices(DEFAULT_MODEL_LIST, undefined, 2, 8),
        }}
      >
        <Card
          loading={loading}
          title={
            <Flex align="center" gap={8}>
              <SlidersOutlined style={{ color: token.colorPrimary }} />
              <span>核心服务参数</span>
            </Flex>
          }
          bordered={false}
          className="premium-card"
          style={{ borderRadius: 12 }}
        >
          <SettingRow title="产品名称" desc="显示在首页、登录页和用户控制台。留空则使用 cursor-gateway。">
            <Form.Item name="product_name" noStyle>
              <Input placeholder={DEFAULT_PRODUCT_NAME} maxLength={40} style={{ width: "100%", maxWidth: 320, borderRadius: 6 }} />
            </Form.Item>
          </SettingRow>
          <Divider style={{ margin: "16px 0" }} />
          <SettingRow title="本地监听地址" desc="管理台及网关分发服务绑定的 HOST 和 PORT。修改后需要重启后端进程才能生效。">
            <div style={{ display: "flex", gap: 8, width: "100%", maxWidth: 320 }}>
              <Form.Item name="host" noStyle>
                <Input placeholder="0.0.0.0" style={{ flex: 1, borderRadius: 6 }} />
              </Form.Item>
              <Form.Item name="port" noStyle>
                <InputNumber style={{ width: 110, borderRadius: 6 }} min={1} max={65535} />
              </Form.Item>
            </div>
          </SettingRow>
          <Divider style={{ margin: "16px 0" }} />
          <SettingRow title="卡密商店地址" desc="用户兑换页会显示购买入口。留空则不显示。">
            <Form.Item name="shop_url" noStyle>
              <Input placeholder="https://shop.example.com" style={{ width: "100%", maxWidth: 360, borderRadius: 6 }} />
            </Form.Item>
          </SettingRow>
        </Card>

        <Card
          loading={loading}
          title={
            <Flex align="center" gap={8}>
              <AppstoreOutlined style={{ color: token.colorPrimary }} />
              <span>模型与模式默认</span>
            </Flex>
          }
          bordered={false}
          className="premium-card"
          style={{ borderRadius: 12, marginTop: 20 }}
        >
          <Typography.Paragraph type="secondary" style={{ marginTop: -4, fontSize: 13 }}>
            完整模型列表会出现在账号配置的下拉里。下面是各模式新建节点时的默认勾选，账号里仍可改。
          </Typography.Paragraph>
          <Form.Item name="model_list" label="模型列表" style={{ marginBottom: 20 }}>
            <Select
              mode="tags"
              tokenSeparators={[",", " ", "\n"]}
              placeholder="输入后回车添加模型 id"
              style={{ width: "100%" }}
              options={(modelList || []).map((id) => ({ value: id, label: id }))}
              onChange={(list) => pruneModeDefaults(list as string[])}
            />
          </Form.Item>
          <Flex vertical gap={12}>
            {MODES.map((mode) => {
              const on = !!modeDefaults?.[mode]?.enabled;
              return (
                <div
                  key={mode}
                  style={{
                    padding: "12px 14px",
                    borderRadius: 8,
                    border: `1px solid ${token.colorSplit}`,
                    background: on ? token.colorBgContainer : token.colorBgLayout,
                    opacity: on ? 1 : 0.78,
                  }}
                >
                  <Flex align="center" justify="space-between" gap={12} wrap="wrap">
                    <div>
                      <Typography.Text strong>{MODE_LABEL[mode]}</Typography.Text>
                      <Typography.Paragraph type="secondary" style={{ margin: "2px 0 0", fontSize: 12 }}>
                        {MODE_HINT[mode]}
                      </Typography.Paragraph>
                    </div>
                    <Form.Item name={["mode_defaults", mode, "enabled"]} valuePropName="checked" noStyle>
                      <Switch checkedChildren="启用" unCheckedChildren="停用" onChange={(v) => setModeEnabled(mode, v)} />
                    </Form.Item>
                  </Flex>
                  <Form.Item name={["mode_defaults", mode, "models"]} style={{ margin: "10px 0 0" }}>
                    <Select
                      mode="multiple"
                      allowClear
                      disabled={!on}
                      placeholder={on ? "默认开放的模型" : "先启用该模式"}
                      options={(modelList || []).map((id) => ({ value: id, label: id }))}
                      style={{ width: "100%" }}
                    />
                  </Form.Item>
                </div>
              );
            })}
          </Flex>
        </Card>

      </Form>

      {meta && (
        <Card
          title={
            <Flex align="center" gap={8}>
              <InfoCircleOutlined style={{ color: token.colorPrimary }} />
              <span>系统运行环境信息</span>
            </Flex>
          }
          bordered={false}
          className="premium-card"
          style={{ borderRadius: 12 }}
        >
          <SettingRow title="存储数据目录" desc="存放密钥、状态及本地路由信息的根目录路径。">
            <Typography.Text copyable style={{ wordBreak: "break-all", fontFamily: "monospace", fontSize: 13 }}>
              {meta.data_dir}
            </Typography.Text>
          </SettingRow>

          <Divider style={{ margin: "16px 0" }} />

          <SettingRow title="数据库路径" desc="本地轻量级持久化 SQLite 数据库文件的物理路径。">
            <Typography.Text copyable style={{ wordBreak: "break-all", fontFamily: "monospace", fontSize: 13 }}>
              {meta.db_path}
            </Typography.Text>
          </SettingRow>

          <Divider style={{ margin: "16px 0" }} />

          <SettingRow title="服务端版本">
            <Flex align="center" gap={6}>
              <DatabaseOutlined style={{ color: token.colorTextDescription }} />
              <Typography.Text strong style={{ fontSize: 14 }}>
                v{meta.version}
              </Typography.Text>
            </Flex>
          </SettingRow>
        </Card>
      )}
    </div>
  );
}
