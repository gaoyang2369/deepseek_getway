# 实验室大模型 API 网关

管理员导入学生学号并配置项目组与预算，学生首次登录强制改密后自行领取个人 Key；也保留管理员直接签发方式。Key 固定绑定学生和项目组，按组使用服务器保管的 DeepSeek 官方凭证。支持 `/v1/models`、Chat Completions 非流式及 SSE 流式。

本项目从交接文档开始开发。已核验 New API `v0.9.9-patch.4` 源码；最终采用 FastAPI + OpenAI Python SDK（连接 DeepSeek）+ PostgreSQL 权威账本 + Redis 限流。选型变化见 [实施决策](docs/implementation-decisions.md)。**不运行 New API，不宣称是 New API 的定制版本。**

## 当前状态

已部署模拟环境，无真实付费请求。按用户要求，当前入口绑定 `0.0.0.0:18081`，同一校园网或可路由网络的电脑访问：<http://10.108.13.250:18081/admin>。初始管理员为 `admin`，演示密码在服务器本机 `secrets/demo_admin_password`，请在终端本地读取，不发送到聊天或提交到版本控制。

学生入口：<http://10.108.13.250:18081/student>（访问根地址也会跳转）。已配置模拟学号 `2026001001`、`2026001002`、`2026001003`，每人每月 10 元模拟预算；未使用的账号初始密码为学号。首次登录必须设置至少 16 字符的新密码，之后领取 Key。完整 Key 只显示一次，遗失时可轮换；网页登录和 API 鉴权相互独立。

已实现：管理员会话与 CSRF、项目组及凭证轮换、人员开通幂等、个人 Key 单次显示和摘要鉴权、成员共享预算、个人和组事务预留、限流与持久化并发控制、流式计量、待核对与人工结算、统计和 CSV、审计和异常事件、加密备份与隔离恢复。

已完成延迟优化与37项回归测试：同组30人独立突发的首有效输出p95从约1059ms降至146ms，完整准入p95为74ms；固定10请求/秒时首输出p95为60ms（直连mock约26ms）。测试全部为模拟调用。持续高频仍可能因逐笔结算积压触发并发限制，详细条件与容量边界见[验收报告](docs/acceptance-report.md#延迟优化复验2026-11-14)。

测试名单和预算已使用模拟数据。正式接入还需核验模型价格、配置有效的服务器专用官方 Key、可信 HTTPS 入口、管理访问网段与异地备份目的地。HTTPS 可使用域名，也可使用受客户端信任的 IP 证书；校外连通性需要另外解决。性能边界和未执行的生产验收见 [验收报告](docs/acceptance-report.md)，校外方案见 [访问与账户说明](docs/access-and-accounts.md)。

## 从干净目录启动模拟环境

需要 Python 3.12、uv、Docker Compose。不要在已有生产目录再次初始化秘密。

```bash
uv sync --locked
.venv/bin/python scripts/init_secrets.py --demo
docker compose -f compose.yaml -f compose.demo.yaml up -d --build
.venv/bin/python scripts/seed_demo.py
.venv/bin/python scripts/seed_student_demo.py
```

新初始化环境默认只发布 `127.0.0.1:18081`；当前服务器已按下文配置允许其他电脑访问。数据库与 Redis 不发布主机端口。模拟模式只连接 Compose 内的免费模拟上游。`seed_demo.py` 创建 A/B 两组、三位模拟同学，将演示个人 Key 写入权限为 0600 的 `secrets/demo_clients.json`。

需要其他电脑通过 IP 访问时，在已有 `.env` 中设置（服务器迁移后替换为实际 IP）：

```dotenv
GATEWAY_BIND_HOST=0.0.0.0
GATEWAY_PORT=18081
PUBLIC_URL=http://10.108.13.250:18081
```

执行 `docker compose -f compose.yaml -f compose.demo.yaml up -d --no-deps --wait gateway` 生效。`PUBLIC_URL` 必须与浏览器实际使用的协议、地址和端口一致，否则管理登录和修改会返回 `invalid_origin`。`0.0.0.0` 是监听地址，浏览器使用服务器实际 IP。当前 HTTP 入口用于模拟验证，正式接入使用 HTTPS。

## 开发与验收

```bash
docker compose -f compose.yaml -f compose.test.yaml up -d --wait postgres redis
.venv/bin/python scripts/local.py .venv/bin/pytest -q
.venv/bin/ruff check gateway scripts tests migrations
```

`compose.test.yaml` 仅为本机开发额外发布数据库 `18543`、Redis `18379`。测试使用独立的 `lab_gateway_test` 数据库和 Redis DB 15，不清空演示数据库。真实套接字、浏览器、压测和恢复命令见验收报告。

操作说明：[部署](docs/deployment.md) · [管理员](docs/admin-guide.md) · [客户端](docs/client-guide.md) · [运维](docs/operations.md) · [验收](docs/acceptance-report.md)
