import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Alert, App, Button, Card, Flex, Form, Input, Layout, Typography } from "antd";
import { ApiOutlined, ArrowRightOutlined, CheckCircleFilled, MoonOutlined, ReloadOutlined, SafetyCertificateOutlined, SunOutlined, ThunderboltOutlined } from "@ant-design/icons";
import { setUserSession, userApi } from "../api";
import { useAuth } from "../auth";
import { useThemeMode } from "../theme";

export default function Login({ mode }: { mode: "login" | "register" | "setup" }) {
  const { mode: themeMode, toggle: toggleTheme } = useThemeMode();
  const { message } = App.useApp();
  const nav = useNavigate();
  const { shop, setMe } = useAuth();
  const [form] = Form.useForm();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [captcha, setCaptcha] = useState<{ id: string; image: string } | null>(null);
  const needCaptcha = mode !== "setup";

  const loadCaptcha = useCallback(async () => {
    const next = await userApi.captcha();
    setCaptcha({ id: next.id, image: next.image });
    form.setFieldValue("captcha", "");
  }, [form]);

  useEffect(() => {
    if (mode !== "setup" && shop?.need_setup) nav("/setup", { replace: true });
    if (mode === "setup" && shop && !shop.need_setup) nav("/login", { replace: true });
  }, [mode, shop, nav]);

  useEffect(() => {
    document.title =
      mode === "setup" ? "初始化管理台 · Cursor Gateway" : `${mode === "register" ? "注册" : "登录"} · OhMyAPI`;
  }, [mode]);

  useEffect(() => {
    if (!needCaptcha) return;
    loadCaptcha().catch(() => setError("验证码加载失败，请刷新页面"));
  }, [needCaptcha, mode, loadCaptcha]);

  async function submit(values: { username: string; password: string; captcha?: string }) {
    setError("");
    setBusy(true);
    try {
      if (needCaptcha && !captcha?.id) {
        throw new Error("请先获取验证码");
      }
      const user =
        mode === "setup"
          ? await userApi.setup(values.username, values.password)
          : mode === "register"
            ? await userApi.register(values.username, values.password, captcha!.id, values.captcha || "")
            : await userApi.login(values.username, values.password, captcha!.id, values.captcha || "");
      setUserSession(user.session || "");
      setMe(user);
      message.success(mode === "setup" ? "管理员已创建" : mode === "register" ? "注册成功" : "已登录");
      nav(user.role === "admin" ? "/" : "/console", { replace: true });
    } catch (e) {
      setError(String((e as Error).message || e));
      if (needCaptcha) loadCaptcha().catch(() => undefined);
    } finally {
      setBusy(false);
    }
  }

  const title = mode === "setup" ? "创建管理员" : mode === "register" ? "用户注册" : "登录";

  return (
    <Layout className="login-layout">
      <Layout.Content className="login-content">
        <Button
          className="login-theme-toggle"
          icon={themeMode === "dark" ? <SunOutlined /> : <MoonOutlined />}
          onClick={toggleTheme}
          aria-label="切换主题"
        />

        <section className="login-story">
          <div className="login-story-glow login-story-glow-one" />
          <div className="login-story-glow login-story-glow-two" />
          <div className="login-brand">
            <span className="login-brand-mark"><ApiOutlined /></span>
            <span>{mode === "setup" ? "Cursor Gateway" : "OhMyAPI"}</span>
          </div>
          <div className="login-story-copy">
            <span className="login-overline">按用量付费的 AI 模型服务</span>
            <Typography.Title className="login-hero-title">
              让每一次模型调用<br />
              <span>都清晰、稳定、可控。</span>
            </Typography.Title>
            <Typography.Paragraph className="login-hero-desc">
              一把密钥调用主流大模型，余额、用量与账单随时可查。
            </Typography.Paragraph>
            <div className="login-feature-list">
              <div><ThunderboltOutlined /><span>按 tokens 计费，无月费</span></div>
              <div><SafetyCertificateOutlined /><span>多把密钥，分开统计</span></div>
              <div><CheckCircleFilled /><span>实时用量与账单</span></div>
            </div>
          </div>
          <Typography.Text className="login-story-foot">SECURE · OBSERVABLE · EFFICIENT</Typography.Text>
        </section>

        <section className="login-form-panel">
          <Card bordered={false} className="login-card">
          <Flex vertical gap={8} className="login-card-head">
            <span className="login-mobile-mark"><ApiOutlined /></span>
            <Typography.Text className="login-welcome">欢迎使用</Typography.Text>
            <Typography.Title level={2} style={{ margin: 0 }}>{title}</Typography.Title>
            <Typography.Text type="secondary">
              {mode === "setup"
                ? "首次使用，创建一个管理员账号开始配置。"
                : mode === "register"
                  ? "创建账号，开始管理你的 API 令牌。"
                  : "请输入账号信息继续访问工作台。"}
            </Typography.Text>
          </Flex>
          {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 16, borderRadius: 8 }} />}
          <Form form={form} layout="vertical" onFinish={submit}>
            <Form.Item name="username" label="用户名" rules={[{ required: true, message: "请填写用户名" }]}>
              <Input size="large" autoComplete="username" placeholder="请输入用户名" />
            </Form.Item>
            <Form.Item name="password" label="密码" rules={[{ required: true, min: 6, message: "至少 6 位" }]}>
              <Input.Password size="large" autoComplete={mode === "login" ? "current-password" : "new-password"} placeholder="请输入密码" />
            </Form.Item>
            {needCaptcha && (
              <Form.Item label="验证码" required>
                <div className="login-captcha">
                  <Form.Item name="captcha" noStyle rules={[{ required: true, message: "请填写验证码" }]}>
                    <Input size="large" autoComplete="off" placeholder="请输入图中字符" maxLength={6} />
                  </Form.Item>
                  <button
                    type="button"
                    className="login-captcha-refresh"
                    onClick={() => loadCaptcha().catch(() => setError("验证码刷新失败"))}
                    aria-label="刷新验证码"
                    title="看不清，换一张"
                  >
                    {captcha?.image ? <img src={captcha.image} alt="验证码" /> : <ReloadOutlined />}
                  </button>
                </div>
              </Form.Item>
            )}
            <Button type="primary" size="large" htmlType="submit" loading={busy} block iconPosition="end" icon={<ArrowRightOutlined />} className="login-submit">
              {mode === "setup" ? "创建并进入管理台" : mode === "register" ? "注册" : "登录"}
            </Button>
          </Form>
          {mode !== "setup" && (
            <Flex justify="space-between" style={{ marginTop: 16 }}>
              {mode === "login" ? (
                shop?.allow_register === false ? (
                  <Typography.Text type="secondary">未开放注册</Typography.Text>
                ) : (
                  <Link to="/register">没有账号？去注册</Link>
                )
              ) : (
                <Link to="/login">已有账号？去登录</Link>
              )}
            </Flex>
          )}
          </Card>
        </section>
      </Layout.Content>
    </Layout>
  );
}
