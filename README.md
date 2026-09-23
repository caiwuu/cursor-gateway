# cursor-gateway

把 Cursor 账号的推理能力转成 OpenAI / Anthropic 兼容接口。

兼容：

- OpenAI：`POST /v1/chat/completions`、`GET /v1/models`
- Anthropic：`POST /v1/messages`

三种推理模式：


| 模式            | 说明                              |
| ------------- | ------------------------------- |
| `account`     | Agent 推理，消耗账号常规额度。客户端工具会桥接回去执行。 |
| `bot`         | Box relay，主要是 grok 系列。          |
| `sand-direct` | 以 sand 身份直连。当前不可用，默认关闭。         |


`/v1` 走节点的默认模式。也可以显式指定：`/account/v1`、`/bot/v1`。

## 启动

```bash
docker compose up -d --build
```

管理台：`http://127.0.0.1:8788`

本地开发：

```bash
pip install -r requirements.txt
python -m cursor_gateway
```

前端单独起：

```bash
cd web && npm install && npm run dev
```

数据在 Docker volume `gateway-data`（本地是 `data/`）。可选环境变量：

- `CURSOR_GATEWAY_HOST` / `CURSOR_GATEWAY_PORT`：管理台监听，默认 `0.0.0.0:8788`
- `CURSOR_GATEWAY_HOME`：数据目录
- `CURSOR_GATEWAY_ADMIN_TOKEN`：设置后，管理接口需要带这个令牌

## 管理员

1. 打开管理台，新建节点，填 **API Key** 和 **推理模式**。保存时会用 API Key 换票，节点名称变成该账号邮箱，端口和机器码自动生成。
2. 在「令牌」页给调用方发 `sk-…`。库里有令牌之后，没带密钥的推理请求会被拒绝。
3. 用量在「用量」页，按日、令牌、节点、模型汇总。

请求入口：

- 负载均衡：`http://127.0.0.1:8788/v1`（在已启用且已有凭据的节点间分发）
- 固定节点：`http://127.0.0.1:8788/n/<slug>/v1`

## 调用

Base URL 不要带 `/v1`。OpenAI 客户端会自己拼上。

```bash
curl http://127.0.0.1:8788/v1/chat/completions \
  -H "Authorization: Bearer sk-你的令牌" \
  -H "Content-Type: application/json" \
  -d '{"model":"模型名","messages":[{"role":"user","content":"你好"}]}'
```

用户登录控制台后可以：

- 在「令牌」页新建自己的 `sk-`
- 在「模型」页给模型起别名。请求里的别名会路由到真实模型，用来避开和客户端内置模型重名

接到 Cursor：

1. 先在控制台「模型」页给要用的模型设置别名并保存。别名会出现在 `/v1/models` 里，请求再路由回真实模型。和 Cursor 内置模型同名时，不设别名会被本地拦下。
2. 打开 **Use OpenAI API Key**，填 `sk-` 令牌
3. 打开 **Override OpenAI Base URL**，填网关地址并带上 `/v1`，例如 `https://你的域名/v1`
4. 新开一段对话，模型选刚才设的别名。只改模型名、不改上面两项，Cursor 会在本地报 `Model name is not valid`，请求到不了网关

