# cursor-gateway

把原来的 `sand_gateway` 做成带管理台的多账号网关：**一个 Cursor 账号 = 一个节点**，每个节点有独立配置、独立端口，以及原来的 bot / account / sand-direct 三条推理链路。

## 为什么用 SQLite，不用一堆 settings.json

| | 多文件 JSON | SQLite（本项目） |
|---|---|---|
| 多账号 | 每个账号一个文件，容易撞名、难检索 | 一行一个节点 |
| 换 token / provision 状态 | 并发写文件容易丢更新 | 事务写入 |
| 控制台列表、启停、日志 | 自己扫目录 | 直接查询 |
| 密钥 | 散落多个 0600 文件 | 单个 `data/gateway.db`（0600） |

原来的 `settings.json` 字段都还在，只是按节点存进数据库。首次启动如果库是空的，会自动把 `sand_gateway/settings.json` 导入成第一个节点。

## 启动

后端（仓库根目录的 `sand_server.py` / `probe_runinference.py` 仍会被复用）：

```bash
cd cursor-gateway
pip install -r requirements.txt
python -m cursor_gateway
```

默认管理台：`http://127.0.0.1:8788`

前端开发：

```bash
cd cursor-gateway/web
npm install
npm run dev
```

生产环境把前端打进后端一起提供：

```bash
cd cursor-gateway/web && npm install && npm run build
cd .. && python -m cursor_gateway
```

环境变量：

- `CURSOR_GATEWAY_HOST` / `CURSOR_GATEWAY_PORT`：管理台监听（默认 `0.0.0.0:8788`）
- `CURSOR_GATEWAY_HOME`：数据目录（默认 `cursor-gateway/data`）
- `CURSOR_GATEWAY_ADMIN_TOKEN`：可选，锁住 `/api/*`

## 节点怎么用

管理台新建节点只需填两项：**API Key** 和 **推理模式**。节点名称用换票后的账号邮箱，端口和机器码自动生成。

请求入口：

1. **负载均衡（推荐）**  
   `http://127.0.0.1:8788/v1/chat/completions`  
   在「已启用且已有凭据」的节点间做最少连接分发。响应头 `x-cursor-gateway-node` 标明实际打到的节点。
2. **固定账号**  
   `http://127.0.0.1:8788/n/<slug>/v1/chat/completions`  
   或该节点自动分配的独立端口，例如 `http://127.0.0.1:8799/v1/chat/completions`

对外分发走管理台「令牌」页：生成 `sk-…`，对方用 `Authorization: Bearer sk-…` 调 `/v1`。只要库里有令牌，没带密钥的推理请求会被拒绝。

用量账本在「用量」页：成功推理会记下输入/输出 Token，按日、令牌、节点、模型汇总，可导出 CSV 自行乘单价计费。明细日志只保留最近 2000 条，汇总表不会裁掉。

模式路径与原来一致：`/bot/v1`、`/account/v1`、`/sand-direct/v1`，以及节点 `default_mode` 对应的 `/v1`。

## 旧入口

`python -m sand_gateway` 仍是单账号、无管理台的旧服务。新项目请用本目录。
