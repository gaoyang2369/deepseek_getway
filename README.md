# 实验室大模型 API 网关

管理员配置项目组、上游凭证和双层预算；学生首次登录改密后领取个人 Key，公共脚本使用服务身份 Key。支持 `/v1/models`、Chat Completions 非流式/SSE 和客户端函数工具。技术栈为 FastAPI、OpenAI Python SDK（连接 DeepSeek）、PostgreSQL 权威账本与 Redis 限流，单进程运行。

## 文档入口

- [部署](docs/deployment.md)：干净目录启动、正式配置、网络入口。
- [管理员](docs/admin-guide.md)：开通、预算、密钥、价格与核对。
- [客户端](docs/client-guide.md)：学生登录、SDK、Harness 与错误处理。
- [运维](docs/operations.md)：备份恢复、故障、升级和隔离验证。
- [架构](docs/implementation-decisions.md)：模块职责、计费与并发约束。

历史性能与重构验证集中在[归档记录](docs/archive/verification.md)。当前运行配置以部署实例为准：`GATEWAY_ENV=demo` 允许演示登录/HTTP，**不保证上游免费**，需核对 `UPSTREAM_URL`。本轮重构未部署到在线实例。

## 开发验证

先按[部署说明](docs/deployment.md#独立模拟环境)初始化本地依赖和秘密。测试必须使用独立 Compose 项目：

```bash
export GATEWAY_COMPOSE_PROJECT=lab-gateway-test
docker compose -p "$GATEWAY_COMPOSE_PROJECT" -f compose.yaml -f compose.test.yaml up -d --wait postgres redis
.venv/bin/playwright install chromium
.venv/bin/python scripts/local.py .venv/bin/pytest -q
.venv/bin/ruff check gateway scripts tests migrations
```

测试数据库每次创建并删除；Redis DB 15 要求测试串行执行。浏览器可由 `GATEWAY_CHROMIUM_PATH` 指定。Harness 回归需设置 `PI_AI_TEST_ROOT`，安装命令见[运维说明](docs/operations.md#隔离验证工具)。新截图、JSON 和运行日志统一写入被 Git 忽略的 `artifacts/`，不再向 `docs` 追加单次测试产物。
