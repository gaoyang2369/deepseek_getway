# 部署说明

## 独立模拟环境

需要 Python 3.12、uv、Docker Compose。在干净目录初始化，已有 `.env`/`secrets/` 时不要重新初始化或 seed。模拟与正式部署使用独立目录、Compose 项目和数据卷。

```bash
uv sync --locked
.venv/bin/python scripts/init_secrets.py --demo
docker compose -f compose.yaml -f compose.demo.yaml up -d --build
.venv/bin/python scripts/seed_demo.py
.venv/bin/python scripts/seed_student_demo.py
```

新环境默认发布 `127.0.0.1:18081`，PostgreSQL/Redis 不发布主机端口。管理员 `/admin`、学生 `/student`，根地址跳转学生入口。演示管理员密码仅保存在 `secrets/demo_admin_password`，seed 生成的客户端资料在 `secrets/demo_clients.json`（0600）。模拟学生首次登录使用学号，随后必须改密。

**`GATEWAY_ENV=demo` 不保证调用免费。** 它允许 HTTP 和演示初始密码；是否使用 MOCK 取决于实际 `UPSTREAM_URL`。不要从历史截图或文档推断在线实例仍是模拟环境。

## 正式配置

在新的受控目录运行 `scripts/init_secrets.py`（不带 `--demo`），交互设置至少 16 字符的管理员密码。秘密文件和 `.env` 为 0600、秘密目录为 0700；`.env` 中的数据库/Redis 凭据同样需要保护。

配置生产 HTTPS `PUBLIC_URL`、各组上游凭证、允许模型、预算、频率/并发与输出上限。生产上游固定为 `https://api.deepseek.com/v1`。通过受保护管理页面录入服务器专用官方 Key，并核验计价；新成员默认预算为 0。`SUPPORT_EMAIL` 可设置管理员联系邮箱，系统不自动发信。

```bash
docker compose up -d --build
docker compose ps
```

生产命令不叠加 `compose.demo.yaml`。在已有实例更新时沿用该实例实际使用的配置文件组合，不直接套用其他环境的命令。应用启动执行 Alembic 迁移；同时发布 `gateway/` 和 `migrations/`，仅 ORM 建表不会安装准入函数。

网关必须单进程/单副本，不能增加 workers。镜像、依赖和哈希已锁定；运行用户默认采用初始化时主机 UID/GID，迁移主机需确认秘密文件所有权。容器根文件系统只读、删除 capabilities，只有 `/tmp` 临时可写。认证加密、HMAC 和备份解密密钥须独立保管并异地备份。

## 网络入口

`GATEWAY_BIND_HOST` 是监听地址；`PUBLIC_URL` 必须与浏览器实际使用的协议、主机和端口一致，否则来源校验返回 `invalid_origin`。`0.0.0.0` 不是浏览器访问地址。局域网模拟示例：

```dotenv
GATEWAY_BIND_HOST=0.0.0.0
GATEWAY_PORT=18081
PUBLIC_URL=http://服务器实际IP:18081
```

修改后使用该实例对应的 Compose 配置重建 gateway。正式同机反向代理可保持 `127.0.0.1` 监听，使用客户端信任的 HTTPS。生产校验要求 HTTPS，不强制域名；IP 证书也需被客户端正确验证，不能通过关闭校验长期运行。

校园私网是否可从校外访问取决于路由，不由域名或 API Key 解决。可以采用学校允许的客户端 VPN、受控组网、公网部署或获准的加密隧道；需验证客户端能访问网关 TCP 端口，网页型 VPN 不一定支持 SDK。运行客户端期间需要持续保持网络可达。

`proxy/nginx.conf.example` 是待集成模板，不要覆盖现有 80/443 服务。配置真实证书、管理网段和 `proxy/lab-gateway-headers.conf`；模板开放文本 API 与学生入口，管理路径默认 deny all，其余路径 404。流式路径禁用缓冲、缓存和 gzip，读取超时 240 秒。`TRUSTED_PROXIES` 只填写实际可信代理地址，容器看到的来源可能是 bridge 网关。

## 上线与回滚

- `/health/live` 仅说明进程存活；`/health/ready` 检查数据库、Redis 与有效价格，不探测付费上游。没有价格时 ready 为 503，但管理员可登录初始化。
- 管理操作和学生流程分别见[管理员指南](admin-guide.md)、[客户端指南](client-guide.md)。禁止把模拟账号、预算、价格和上游凭证直接作为生产配置。
- 上线前备份数据和秘密，在隔离恢复库验证迁移、流式代理与权限；真实供应商试点应明确调用次数和输出上限。
- 记录镜像摘要及 Alembic revision。回滚按版本评估数据兼容，历史某一次迁移的 downgrade 命令不能作为通用回滚命令。详细步骤见[运维说明](operations.md#升级和回滚)。
