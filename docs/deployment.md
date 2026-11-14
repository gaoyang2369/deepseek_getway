# 部署说明

当前服务器环境：Ubuntu 24.04、x86_64、约 125 GiB 内存、Docker Compose v5.1.4。80/443 已由现有服务占用，常见数据库端口也已使用。开发使用独立 Compose 项目 `lab-llm-gateway` 与 18081 端口；不覆盖现有站点。

## 模拟运行

按 README 初始化并启动：

```bash
uv sync --locked
.venv/bin/python scripts/init_secrets.py --demo
docker compose -f compose.yaml -f compose.demo.yaml up -d --build
.venv/bin/python scripts/seed_demo.py
```

已存在 `.env` 和 `secrets/` 时初始化脚本拒绝覆盖。若在已有部署继续操作，不再次执行初始化或 seed。生产与模拟必须使用独立目录、独立 Compose 项目、独立数据卷。

当前服务器按用户要求开放 IPv4 监听 `0.0.0.0:18081`，管理页面 <http://10.108.13.250:18081/admin>。这是校园网地址，客户端须与服务器网络可达。配置为 `.env` 中 `GATEWAY_BIND_HOST=0.0.0.0`、`GATEWAY_PORT=18081`、`PUBLIC_URL=http://10.108.13.250:18081`。修改后执行 `docker compose -f compose.yaml -f compose.demo.yaml up -d --no-deps --wait gateway`。

`PUBLIC_URL` 与浏览器实际来源必须一致，不能填 `http://0.0.0.0:18081`。换 IP 或端口时同时更新它，否则登录和修改请求会被来源校验拒绝。新初始化默认回环监听；需要恢复回环时设置 `GATEWAY_BIND_HOST=127.0.0.1`、`PUBLIC_URL=http://127.0.0.1:18081` 并重建网关容器。

## 生产配置

在新的受控目录使用 `scripts/init_secrets.py`（不带 `--demo`），通过终端交互设置至少 16 字符管理员密码。脚本生成数据库、Redis、认证加密、HMAC 和备份秘密，文件权限为 0600，目录 0700。`.env` 包含数据库/Redis连接凭证，同样作为秘密保护。

必填清单：

- 可由客户端信任的 HTTPS 入口及证书，`PUBLIC_URL` 为实际 HTTPS 地址；可用域名，也可用配置正确且被客户端信任的 IP 证书。域名不会使校园私网地址自动变为公网可达。
- 各组的名称、允许模型、预算、RPM、并发、输出上限。
- 学生姓名、字符串学号、项目组、个人周期预算、Key 允许模型与到期。
- 管理员确认的 DeepSeek 模型 ID、价格版本、币种/时段换算和保守估算说明。
- 每组新的服务器专用 DeepSeek 官方 Key；在管理页录入，不粘贴到聊天。
- 实际管理访问网段、可信反向代理地址、校园网/VPN/公网访问边界。
- 可选 `SUPPORT_EMAIL`：学生忘记密码时显示的管理员联系邮箱；系统不自动发送邮件。
- 单独保存并异地备份认证加密主密钥、HMAC 密钥和备份解密密钥。

`UPSTREAM_URL` 生产模式固定为 `https://api.deepseek.com/v1`。没有配置价格时就绪检查返回 503，收费 API 拒绝未计价模型，但管理员仍可登录初始化。新成员预算默认 0。不要把演示环境切一个环境变量就当成生产：演示学生、Key、模拟凭证和价格必须与生产隔离。

```bash
docker compose up -d --build
docker compose ps
```

应用启动自动执行 Alembic 迁移。网关按 `GATEWAY_BIND_HOST` 发布 18081；生产同机反向代理建议使用 `127.0.0.1`。PostgreSQL、Redis 在内部网络，无主机发布端口。容器重启策略为 unless-stopped；主机 Docker 守护进程需按服务器既有策略开机启动。

镜像锁定 PostgreSQL/Redis/Python 摘要，依赖锁定并按哈希安装。容器用户默认取初始化时的主机 UID/GID，以读取 0600 secrets；迁移部署机器时确认所有权。网关根文件系统只读、删除全部 capabilities；仅 `/tmp` 为临时可写。

## HTTPS 入口

`proxy/nginx.conf.example` 是待集成模板，不能替换当前服务器站点。它精确放行 `/v1/models` 与 `/v1/chat/completions`，管理路径默认 deny all，待填写实际访问范围。部署时配置正确的证书路径、域名、管理员允许网段；其余路径 404。把头配置复制到模板引用的目标路径。

流式路径关闭缓冲、缓存和 gzip，使用 HTTP/1.1，读取超时 240 秒。边缘覆盖 X-Forwarded-For，应用仅信任 `TRUSTED_PROXIES` 中明确的代理地址。Docker 容器中看到的代理来源可能是 bridge 网关地址，须用实际网络核验，不能仅填 127.0.0.1 后假定可信链正确。

生产管理页面按管理员网段限制访问；浏览器会话在生产模式使用 Secure Cookie。HTTP 只用于 demo 环境；当前校园网 IP 入口用于模拟验证，生产模式仍强制 HTTPS。

## 就绪与检查

- `/health/live`：进程存活，不检查付费上游。
- `/health/ready`：数据库、Redis 和至少一个有效价格配置；不代表官方 Key 可用。
- 管理 `/admin/api/metrics`：请求/拒绝、依赖与上游错误、当前并发、待核对数量和金额。

官方连通测试需要管理员录入真实凭证后，明确限定输出与次数；本次开发没有发送真实付费请求。确认真实接入、HTTPS、来源 IP 和账单后再安排试点与旧凭证撤销。

## 学生入口升级

本次增加迁移 `7ac260319e01`：为成员增加自助 Key 的到期上限，创建学生账号与独立会话表。已有身份、Key、账目与预算保留；已有成员的到期上限由现有 Key 的权限边界回填。升级前已执行加密数据库备份。新生导入不创建 Key，学生改密后自行领取。

对现有模拟环境运行 `.venv/bin/python scripts/seed_student_demo.py`，可补齐三名模拟学生，每人每月 10 元，重复执行不重置账号。部署后验证：`.venv/bin/python scripts/student_browser_check.py`（仅模拟环境），结果见 `student-browser-results.json`。

校外部署方案、IP/HTTPS 与密码策略说明见 [访问与账户说明](access-and-accounts.md)。本次未安装 VPN、隧道或云端中转。

## 延迟优化升级（2026-11-14）

迁移 `9d31b47fa820` 新增3个请求查询索引及2个准入数据库函数，保留全部既有身份、预算和账目。需要同时发布 `gateway/` 与 `migrations/`；仅用 ORM 建表不能代替 Alembic。大数据部署应单独评估建索引的维护时间，本次既有模拟库158条请求，已完成恢复副本上的升级、降级和再次升级，应用数据逐行一致。

当前演示网关已更新，地址及MOCK上游不变。升级前加密备份为 `backups/20261114T081819Z-database.enc`，旧镜像保留为 `lab-llm-gateway:pre-latency-20261114`。新版仍为单进程，不支持直接增加 workers 或副本。

本次迁移是增量函数/索引变更。若需回滚本次升级，先停止唯一网关并核对未结束请求，使用**新镜像**执行 `alembic downgrade 7ac260319e01`，再将旧镜像恢复到服务标签并启动；该降级不删除请求或账目。不要让新旧锁顺序的进程同时服务，也不要把旧数据库备份直接覆盖升级后已发生的调用。后续再有迁移时，需重新评估回滚路径。
