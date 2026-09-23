import { useEffect, useMemo } from "react";
import { Link } from "react-router-dom";
import {
  ApiOutlined,
  ArrowRightOutlined,
  CheckCircleFilled,
  CodeOutlined,
  DollarOutlined,
  MoonOutlined,
  SafetyCertificateOutlined,
  SunOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import { useAuth } from "../auth";
import { useThemeMode } from "../theme";
import { productNameOf, type ShopModel } from "../types";
import "./landing.css";

const FAMILIES: [RegExp, string][] = [
  [/^claude/i, "Claude"],
  [/^(gpt|o\d)/i, "GPT"],
  [/^grok/i, "Grok"],
  [/^gemini/i, "Gemini"],
  [/^deepseek/i, "DeepSeek"],
  [/^kimi/i, "Kimi"],
  [/^qwen/i, "Qwen"],
];

function familyOf(id: string) {
  return FAMILIES.find(([re]) => re.test(id))?.[1] || "";
}

function fmtPrice(n: number) {
  return Number(n || 0).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}

function officialOf(m: ShopModel) {
  return {
    input: Number(m.official_input_price_per_1m || 0),
    output: Number(m.official_output_price_per_1m || 0),
  };
}

function PriceCell({ value, official }: { value: number; official?: number }) {
  return (
    <div className="landing-price">
      <span className="landing-price-ours">
        ¥{fmtPrice(value)}
        <small>/ 1M</small>
      </span>
      {official && official > 0 ? <span className="landing-price-official">官方 ≈ ¥{fmtPrice(official)}</span> : null}
    </div>
  );
}

const FAQ = [
  {
    q: "怎么充值？",
    a: "登录控制台后进入「兑换」页，输入卡密即可。兑换成功后，余额会立即到账。",
  },
  {
    q: "余额会过期吗？有月费吗？",
    a: "余额长期有效。平台不收月费，也没有套餐或最低消费，用完后再充值即可。",
  },
  {
    q: "费用是怎么算的？",
    a: "每次请求按实际消耗的输入 tokens 和输出 tokens 分别计费。模型单价、token 用量和扣费金额都可以在控制台查看。",
  },
  {
    q: "支持哪些客户端？",
    a: "兼容 OpenAI 和 Anthropic 接口。支持自定义 Base URL 与 API Key 的客户端、SDK 和编辑器插件通常都可以直接使用。",
  },
  {
    q: "可以创建多把密钥吗？",
    a: "可以。你可以为不同项目或设备分别创建 API Key，单独启用或停用，并分别查看用量。",
  },
];

export default function Home() {
  const { me, shop } = useAuth();
  const { mode, toggle } = useThemeMode();
  const brand = productNameOf(shop);

  useEffect(() => {
    document.title = `${brand} · 多模型 API 聚合服务`;
    let meta = document.querySelector<HTMLMetaElement>('meta[name="description"]');
    if (!meta) {
      meta = document.createElement("meta");
      meta.name = "description";
      document.head.appendChild(meta);
    }
    meta.content = "一个 API Key 调用多家主流模型。兼容 OpenAI 与 Anthropic 接口，按 token 用量计费。";
  }, [brand]);

  const models: ShopModel[] = useMemo(() => {
    if (!shop) return [];
    if (shop.models?.length) return shop.models;
    return (shop.model_list || []).map((id) => ({
      id,
      input_price_per_1m: shop.input_price_per_1m ?? 0,
      output_price_per_1m: shop.output_price_per_1m ?? 0,
      official_input_price_per_1m: 0,
      official_output_price_per_1m: 0,
    }));
  }, [shop]);

  const families = useMemo(() => {
    const seen: string[] = [];
    for (const m of models) {
      const f = familyOf(m.id);
      if (f && !seen.includes(f)) seen.push(f);
    }
    return seen;
  }, [models]);

  const minInput = models.length ? Math.min(...models.map((m) => m.input_price_per_1m)) : 0;

  const active = models[0];
  const cost = active
    ? (10_000 / 1_000_000) * active.input_price_per_1m + (2_000 / 1_000_000) * active.output_price_per_1m
    : 0;

  const canRegister = shop?.allow_register !== false;
  const consoleTo = me ? (me.role === "admin" ? "/overview" : "/console") : "";
  const primaryCta = me
    ? { to: consoleTo, label: "进入控制台" }
    : canRegister
      ? { to: "/register", label: "免费注册" }
      : { to: "/login", label: "登录" };

  const familyText = families.length ? families.slice(0, 3).join("、") : "主流大模型";
  const previewModels = models.length
    ? models.slice(0, 3).map((model) => model.id)
    : ["claude-sonnet-4", "gpt-5", "gemini-2.5-pro"];

  return (
    <div className="landing">
      <header className="landing-nav">
        <div className="landing-container landing-nav-inner">
          <Link to="/" className="landing-brand">
            <span className="landing-brand-mark">
              <ApiOutlined />
            </span>
            <span>
              {brand}
              <small>UNIFIED MODEL API</small>
            </span>
          </Link>

          <nav className="landing-nav-links">
            <a href="#pricing">模型价格</a>
            <a href="#billing">接入说明</a>
            <a href="#faq">常见问题</a>
          </nav>

          <div className="landing-nav-actions">
            <button type="button" className="landing-icon-btn" onClick={toggle} aria-label="切换主题">
              {mode === "dark" ? <SunOutlined /> : <MoonOutlined />}
            </button>
            {me ? (
              <Link to={consoleTo} className="landing-btn landing-btn-primary">
                进入控制台
              </Link>
            ) : (
              <>
                <Link to="/login" className="landing-btn landing-btn-text">
                  登录
                </Link>
                {canRegister && (
                  <Link to="/register" className="landing-btn landing-btn-primary">
                    注册
                  </Link>
                )}
              </>
            )}
          </div>
        </div>
      </header>

      <main>
        <section className="landing-hero">
          <div className="landing-container">
            <div className="landing-hero-grid">
              <div className="landing-hero-inner">
                <p className="landing-eyebrow">
                  <span className="landing-live-dot" />
                  按量计费 · 余额长期有效
                </p>
                <h1 className="landing-h1">
                  <em>{brand}</em>
                </h1>
                <p className="landing-lead">
                  兼容 OpenAI 和 Anthropic 接口。已有项目只需替换 Base URL 和 API Key，即可调用{" "}
                  {familyText} 等模型。
                </p>
                <div className="landing-hero-actions">
                  <Link to={primaryCta.to} className="landing-btn landing-btn-primary landing-btn-lg">
                    {primaryCta.label}
                    <ArrowRightOutlined />
                  </Link>
                  <a href="#pricing" className="landing-btn landing-btn-ghost landing-btn-lg">
                    查看模型价格
                  </a>
                </div>
                <div className="landing-assurances">
                  <span>
                    <CheckCircleFilled /> OpenAI 接口兼容
                  </span>
                  <span>
                    <CheckCircleFilled /> Anthropic 接口兼容
                  </span>
                  <span>
                    <CheckCircleFilled /> 调用明细可查
                  </span>
                </div>
              </div>

              <div className="landing-console-wrap" aria-label="API 请求示例">
                <div className="landing-console-glow" />
                <div className="landing-console">
                  <div className="landing-console-bar">
                    <span className="landing-window-dots">
                      <i />
                      <i />
                      <i />
                    </span>
                    <span>API 请求示例</span>
                    <span className="landing-secure">
                      <SafetyCertificateOutlined /> HTTPS
                    </span>
                  </div>
                  <div className="landing-console-body">
                    <div className="landing-request-title">
                      <span className="landing-method">POST</span>
                      <code>/v1/chat/completions</code>
                    </div>
                    <div className="landing-code">
                      <span>{"{"}</span>
                      <span>
                        &nbsp;&nbsp;<b>&quot;model&quot;</b>: <em>&quot;{previewModels[0]}&quot;</em>,
                      </span>
                      <span>
                        &nbsp;&nbsp;<b>&quot;messages&quot;</b>: <i>[...]</i>,
                      </span>
                      <span>
                        &nbsp;&nbsp;<b>&quot;stream&quot;</b>: <strong>true</strong>
                      </span>
                      <span>{"}"}</span>
                    </div>

                    <div className="landing-route">
                      <div className="landing-route-head">
                        <span>模型状态</span>
                        <span>{models.length || "20+"} 个可用模型</span>
                      </div>
                      <div className="landing-model-stack">
                        {previewModels.map((model, index) => (
                          <div className={index === 0 ? "is-active" : ""} key={model}>
                            <span className="landing-model-logo">{familyOf(model).slice(0, 1) || "AI"}</span>
                            <code>{model}</code>
                            {index === 0 ? <span className="landing-routing">已选择</span> : <span>可用</span>}
                          </div>
                        ))}
                      </div>
                    </div>

                    <div className="landing-response">
                      <span>
                        <i /> 200 OK
                      </span>
                      <span>843 ms</span>
                      <span>¥{active ? Math.max(cost, 0.0032).toFixed(4) : "0.0032"}</span>
                    </div>
                  </div>
                </div>
                <div className="landing-float-card landing-float-card-top">
                  <ThunderboltOutlined />
                  <span>
                    <b>流式响应</b>
                    支持 SSE 输出
                  </span>
                </div>
                <div className="landing-float-card landing-float-card-bottom">
                  <CheckCircleFilled />
                  <span>
                    <b>调用成功</b>
                    本次费用已记录
                  </span>
                </div>
              </div>
            </div>

            <div className="landing-facts">
              <div className="landing-fact">
                <span className="landing-fact-value">{models.length || "20+"}</span>
                <span className="landing-fact-label">当前可用模型</span>
              </div>
              <div className="landing-fact">
                <span className="landing-fact-value">{models.length ? `¥${fmtPrice(minInput)} 起` : "按量计费"}</span>
                <span className="landing-fact-label">每百万输入 tokens 单价</span>
              </div>
              <div className="landing-fact">
                <span className="landing-fact-value">¥0</span>
                <span className="landing-fact-label">月费 / 最低消费</span>
              </div>
              <div className="landing-fact landing-fact-models">
                <span className="landing-fact-label">支持模型</span>
                <div>
                  {(families.length ? families : ["Claude", "GPT", "Gemini", "Grok"]).slice(0, 4).map((family) => (
                    <span key={family}>{family}</span>
                  ))}
                </div>
              </div>
            </div>
          </div>
        </section>

        <section id="pricing" className="landing-section">
          <div className="landing-container">
            <div className="landing-section-head">
              <div>
                <span className="landing-kicker">模型价格</span>
                <h2>输入、输出分别计费</h2>
              </div>
              <p>
                价格单位为元 / 百万 tokens。每次请求按实际用量扣本站单价。填了官方参考价的模型会一并对照，方便比较。
              </p>
            </div>

            <div className="landing-pricing">
              <div className="landing-card">
                <div className="landing-card-title">
                  <div>
                    <span className="landing-card-icon">
                      <CodeOutlined />
                    </span>
                    <span>
                      <b>当前可用模型</b>
                      <small>本站价 / 官方参考价 · 元 / 百万 tokens</small>
                    </span>
                  </div>
                  <span className="landing-status-pill">
                    <i /> 正常
                  </span>
                </div>
                <table className="landing-table">
                  <thead>
                    <tr>
                      <th>模型</th>
                      <th className="num">输入</th>
                      <th className="num">输出</th>
                    </tr>
                  </thead>
                  <tbody>
                    {models.length === 0 ? (
                      <tr>
                        <td colSpan={3} className="landing-table-empty">
                          {shop ? "暂未开放模型，请稍后再来。" : "价格信息加载失败，请刷新重试。"}
                        </td>
                      </tr>
                    ) : (
                      models.map((m) => {
                        const family = familyOf(m.id);
                        return (
                          <tr key={m.id}>
                            <td>
                              <div className="landing-model">
                                <span className="landing-model-id">{m.id}</span>
                                {family && <span className="landing-model-family">{family}</span>}
                              </div>
                            </td>
                            <td className="num">
                              <PriceCell value={m.input_price_per_1m} official={officialOf(m).input} />
                            </td>
                            <td className="num">
                              <PriceCell value={m.output_price_per_1m} official={officialOf(m).output} />
                            </td>
                          </tr>
                        );
                      })
                    )}
                  </tbody>
                </table>
                {models.length > 0 && (
                  <div className="landing-table-foot">参考：1,000 tokens 约等于 750 个英文单词，或 500–700 个汉字。</div>
                )}
              </div>

            </div>
          </div>
        </section>

        <section id="billing" className="landing-section">
          <div className="landing-container">
            <div className="landing-section-head">
              <div>
                <span className="landing-kicker">接入说明</span>
                <h2>三步开始调用</h2>
              </div>
              <p>注册账户、兑换余额、创建 API Key。已有项目可以直接替换接口地址和密钥。</p>
            </div>

            <ol className="landing-steps">
              <li>
                <span className="landing-step-icon">
                  <span>01</span>
                  <SafetyCertificateOutlined />
                </span>
                <h3>注册并登录</h3>
                <p>创建账户并登录控制台。</p>
              </li>
              <li>
                <span className="landing-step-icon">
                  <span>02</span>
                  <DollarOutlined />
                </span>
                <h3>兑换余额</h3>
                <p>在「兑换」页输入卡密，兑换成功后余额立即到账。</p>
              </li>
              <li>
                <span className="landing-step-icon">
                  <span>03</span>
                  <CodeOutlined />
                </span>
                <h3>创建 API Key</h3>
                <p>复制 API Key 和 Base URL，填入客户端或项目配置。</p>
              </li>
            </ol>

            <ul className="landing-rules">
              <li>
                <span className="landing-rule-icon">
                  <DollarOutlined />
                </span>
                <div>
                  <strong>按实际用量扣费</strong>
                  <span>输入、输出 tokens 分别乘以对应单价，从账户余额中扣除。</span>
                </div>
              </li>
              <li>
                <span className="landing-rule-icon">
                  <SafetyCertificateOutlined />
                </span>
                <div>
                  <strong>余额长期有效</strong>
                  <span>不收月费，没有最低消费。余额用完后再充值即可。</span>
                </div>
              </li>
              <li>
                <span className="landing-rule-icon">
                  <CodeOutlined />
                </span>
                <div>
                  <strong>每一笔都可查</strong>
                  <span>控制台可以按日期、模型和 API Key 查询 token 用量与费用。</span>
                </div>
              </li>
              <li>
                <span className="landing-rule-icon">
                  <ThunderboltOutlined />
                </span>
                <div>
                  <strong>用完即停，不欠费</strong>
                  <span>余额不足时请求会被拒绝，不会产生欠费。</span>
                </div>
              </li>
            </ul>
          </div>
        </section>

        <section id="faq" className="landing-section">
          <div className="landing-container">
            <div className="landing-faq-layout">
              <div className="landing-section-head">
                <div>
                  <span className="landing-kicker">常见问题</span>
                  <h2>充值、计费与接口说明</h2>
                </div>
                <p>这里整理了开始使用前经常遇到的问题。</p>
              </div>
              <div className="landing-faq">
                {FAQ.map((item, index) => (
                  <details key={item.q} open={index === 0}>
                    <summary>{item.q}</summary>
                    <p>{item.a}</p>
                  </details>
                ))}
              </div>
            </div>
          </div>
        </section>

        <section className="landing-cta">
          <div className="landing-container">
            <div className="landing-cta-inner">
              <div>
                <span className="landing-kicker">开始使用</span>
                <h2>注册后即可创建 API Key</h2>
                <p>充值到账后，就可以调用全部已开放模型。</p>
              </div>
              <Link to={primaryCta.to} className="landing-btn landing-btn-primary landing-btn-lg">
                {primaryCta.label}
                <ArrowRightOutlined />
              </Link>
            </div>
          </div>
        </section>
      </main>

      <footer className="landing-footer">
        <div className="landing-container landing-footer-inner">
          <span>© {new Date().getFullYear()} {brand}</span>
          <nav>
            <a href="#pricing">模型价格</a>
            <Link to="/login">登录</Link>
            {canRegister && !me && <Link to="/register">注册</Link>}
          </nav>
        </div>
      </footer>
    </div>
  );
}
