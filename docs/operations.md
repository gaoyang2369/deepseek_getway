# 运行维护

## 服务与故障

```bash
docker compose -f compose.yaml -f compose.demo.yaml ps
curl --noproxy '*' --fail http://127.0.0.1:18081/health/live
curl --noproxy '*' --fail http://127.0.0.1:18081/health/ready
```

生产去掉 `compose.demo.yaml`。就绪不包含付费上游探测。管理 metrics 包含拒绝原因、上游/数据库/结算失败、进行中与待核对金额；累计 HTTP 计数保存在进程内，重启清零，资金统计与历史保存在 PostgreSQL。请求详情提供准入、首有效增量与总耗时。没有向外部邮件或聊天发送通知。

延迟优化后，metrics 另含 `admission_batches`（批数、请求数、最大批大小、取消清理失败数）和 `admission_queue`。`admission_ms` 包含读取请求、排队和准入提交，随结算或待核对写入，进行中/强制崩溃的请求可能暂缺该指标；`total_ms` 不含后续结算提交耗时，客户端收到SSE结束也不代表资金事务已提交。新请求在结束前保持 `reserved`，与旧版 `running` 一样计入持久化并发。

检查持续容量时同时观察结算排空时间及 `concurrency_limit`，不要仅看首输出或将其解释为供应商限速。固定10请求/秒模拟测试已通过；连续快速突发仍可能积压并触发并发保护，详见验收报告。尝试限流在资金锁前执行，已认证的拒绝尝试也计入Redis频率。

数据库/Redis不可用时拒绝新收费请求。结算失败有限重试；持久化预留保留。依赖恢复后重启网关，将遗留 running/reserved 转入 pending，再按官方账单与证据核对。不要用 TTL 或 SQL 把全部预留清零。pending 保守占用并发；这是防止上游仍运行时过量放行的策略。

单进程/单副本是明确限制。数据库 advisory lock 防止第二实例启动，不允许 `--workers 2` 或增加副本。服务器和容器的单机故障仍会导致停机。

## 备份

初始化生成独立的 `secrets/backup_key`。它是备份解密密钥，必须额外离线保管，不包含在秘密备份包中。数据库认证加密主密钥、HMAC 密钥与数据库备份分别管理。

```bash
.venv/bin/python scripts/backup.py
.venv/bin/python scripts/backup.py --secrets
```

数据库使用 `pg_dump -Fc` 的事务一致性快照。备份按 1 MiB 分块 AES-256-GCM 认证加密，帧绑定序号并有认证结束帧，检测篡改、调序和截断。秘密另存加密归档。输出目录 0700、文件 0600；中途失败不留下声称成功的文件。

每天运行 `scripts/backup_schedule.py` 可保留最近 7 个日快照和 4 个周快照。可在管理员实际选择的系统定时器或 cron 中设置，例如每天北京时间 02:30，工作目录设为本项目目录，解释器使用项目 `.venv/bin/python`。**本次未安装系统级定时任务，也未配置异地复制。** 成功后将加密文件复制到管理员指定的异地存储；同盘备份不抵御整机丢失。备份目的地和定时维护窗口由部署负责人填写。

## 恢复

恢复脚本只允许新建 `lab_gateway_restore_<suffix>` 数据库，防止覆盖现有数据库。先验证整个密文再调用 pg_restore，认证失败不会向 PostgreSQL 导入不完整数据。

```bash
.venv/bin/python scripts/restore.py backups/实际时间-database.enc \
  --target lab_gateway_restore_drill
```

秘密归档由授权运维人员在独立受限目录解密恢复；不得覆盖在线目录。必须同时找回原有认证加密和 HMAC 密钥，否则上游密文不可解密、个人 Key 摘要不可验证。不要更换 HMAC 主密钥后声称旧 Key 仍有效。

开发环境完整恢复演练：

```bash
.venv/bin/python scripts/local.py .venv/bin/python scripts/restore_drill.py
```

该脚本导出 PostgreSQL repeatable-read 快照并让 pg_dump 使用同一快照，恢复到新库后逐表逐行比较身份、摘要 Key、加密凭证、预算、未结算请求、账本与审计。实际耗时见 `restore-results.json`。该耗时只包含本机小样本恢复，不作为生产恢复时间承诺。RPO 为最近一次成功备份；真实 RTO 取决于数据量、异地下载和秘密恢复。

确认恢复完整后，在维护窗口停止唯一网关，更新受限数据库连接指向经验证的新库，再启动并检查恢复事件。生产切换与销毁旧库由管理员明确执行，本次没有操作。

## 升级和回滚

1. 安排维护窗口，停止新收费流量并等待进行中请求排空；不确定请求保留预留。
2. 备份数据库和秘密，记录当前镜像 ID/摘要、Alembic revision、配置版本和可恢复快照。
3. 在隔离数据库恢复一份备份，测试新镜像的迁移、核心验收和与旧数据的兼容。
4. 构建新镜像，单实例启动，核查 live/ready、请求隔离、少量限额调用和账本。
5. 回滚不能只换镜像。若数据库迁移不可向后兼容，使用匹配旧版本的经验证备份并说明自备份后数据如何核对；不直接在生产运行 `alembic downgrade base`。

升级依赖时同步维护 `pyproject.toml`、`uv.lock` 和 `requirements.lock`，重新构建按哈希安装的镜像。依赖安装/镜像拉取需要网络；不要因拉取失败临时改成未锁定 latest。

## 隐私维护与泄露

`RETENTION_DAYS` 默认 90，运行以下维护清理过期管理员及学生会话并将旧请求的原始 IP 置空，保留计量、账本和审计：

```bash
.venv/bin/python scripts/local.py .venv/bin/python scripts/housekeeping.py
```

容器环境运行同一脚本时需把 scripts 目录按受控方式提供给容器；脚本无需付费上游。当前没有自动删除财务明细；可按时间查询聚合历史。长期数据归档策略应按实验室保留要求部署，避免删账本影响核对。

个人 Key 泄露：吊销、核查对应时间/费用/来源，再为原成员轮换，保持预算。官方 Key 泄露：管理员在官方平台轮换并更新组凭证，同时检查服务器、日志和备份。当前开发没有撤销任何真实官方 Key。

迁移时先发新的服务器专用官方 Key，完成试点与客户端迁移后，再按批准的批次/窗口撤销旧共享 Key。撤销会影响仍在使用旧 Key 的程序，不能作为开发测试执行。

预算消费事件阈值可通过 `BUDGET_THRESHOLDS` 配置为逗号分隔的百分比（默认 `80,100`，范围1–100）；事件按预算周期去重。更改受限配置后重启唯一网关生效，不会变更资金账本。
