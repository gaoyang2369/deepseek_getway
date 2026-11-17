# 运行维护

## 服务与故障

```bash
docker compose ps
curl --noproxy '*' --fail http://127.0.0.1:18081/health/live
curl --noproxy '*' --fail http://127.0.0.1:18081/health/ready
```

命令沿用目标实例的 Compose 项目与配置文件。就绪不包含付费上游探测。管理 metrics 包含拒绝原因、上游/数据库/结算失败、进行中与待核对请求数；累计 HTTP 计数保存在进程内，重启清零，资金统计与历史保存在 PostgreSQL。请求详情提供准入、首有效增量与总耗时。没有向外部邮件或聊天发送通知。

延迟优化后，metrics 另含 `admission_batches`（批数、请求数、最大批大小、取消清理失败数）和 `admission_queue`。`admission_ms` 包含读取请求、排队和准入提交，随结算或待核对写入，进行中/强制崩溃的请求可能暂缺该指标；`total_ms` 不含后续结算提交耗时，客户端收到SSE结束也不代表资金事务已提交。新请求在结束前保持 `running`，与历史 `reserved` 一样计入持久化并发。

检查持续容量时同时观察结算排空时间及 `concurrency_limit`，不要仅看首输出或将其解释为供应商限速。历史测试的条件及持续负载边界见[归档记录](archive/verification.md)。尝试限流在资金锁前执行，已认证的拒绝尝试也计入Redis频率。

数据库/Redis不可用时拒绝新收费请求。结算失败有限重试；持久化调用记录保留。依赖恢复后重启网关，将遗留 running/reserved 转入 pending，再按官方账单与证据核对。pending 金额未知，不占预算及已结束请求的并发名额；上游仍可能有未计入的费用。

单进程/单副本是明确限制。数据库 advisory lock 防止第二实例启动，不允许 `--workers 2` 或增加副本。服务器和容器的单机故障仍会导致停机。

## 备份

初始化生成独立的 `secrets/backup_key`。它是备份解密密钥，必须额外离线保管，不包含在秘密备份包中。数据库认证加密主密钥、HMAC 密钥与数据库备份分别管理。

```bash
.venv/bin/python scripts/backup.py
.venv/bin/python scripts/backup.py --secrets
```

数据库使用 `pg_dump -Fc` 的事务一致性快照。备份按 1 MiB 分块 AES-256-GCM 认证加密，帧绑定序号并有认证结束帧，检测篡改、调序和截断。秘密另存加密归档。输出目录 0700、文件 0600；中途失败不留下声称成功的文件。

每天运行 `scripts/backup_schedule.py` 可保留最近 7 个日快照和 4 个周快照。可在管理员实际选择的系统定时器或 cron 中设置，例如每天北京时间 02:30，工作目录设为本项目目录，解释器使用项目 `.venv/bin/python`。脚本不会自动安装系统定时任务或配置异地复制。 成功后将加密文件复制到管理员指定的异地存储；同盘备份不抵御整机丢失。备份目的地和定时维护窗口由部署负责人填写。

## 恢复

恢复脚本只允许新建 `lab_gateway_restore_<suffix>` 数据库，防止覆盖现有数据库。先验证整个密文再调用 pg_restore，认证失败不会向 PostgreSQL 导入不完整数据。

```bash
.venv/bin/python scripts/restore.py backups/实际时间-database.enc \
  --target lab_gateway_restore_drill
```

秘密归档由授权运维人员在独立受限目录解密恢复；不得覆盖在线目录。必须同时找回原有认证加密和 HMAC 密钥，否则上游密文不可解密、个人 Key 摘要不可验证。不要更换 HMAC 主密钥后声称旧 Key 仍有效。

开发环境完整恢复演练（先启动独立 Compose 项目，见下方隔离验证工具）：

```bash
GATEWAY_COMPOSE_PROJECT=lab-gateway-test .venv/bin/python scripts/local.py .venv/bin/python scripts/restore_drill.py
```

该脚本导出 PostgreSQL repeatable-read 快照并让 pg_dump 使用同一快照，恢复到新库后逐表逐行比较身份、摘要 Key、加密凭证、预算、未结算请求、账本与审计。验证后删除演练恢复库；实际耗时见 `artifacts/restore-results.json`。该耗时只包含本机小样本恢复，不作为生产恢复时间承诺。RPO 为最近一次成功备份；真实 RTO 取决于数据量、异地下载和秘密恢复。

确认恢复完整后，在维护窗口停止唯一网关，更新受限数据库连接指向经验证的新库，再启动并检查恢复事件。生产切换与销毁旧库由管理员在维护窗口明确执行。

## 升级和回滚

1. 安排维护窗口，停止新收费流量并等待进行中请求排空；不确定请求保留待核对记录。
2. 备份数据库和秘密，记录当前镜像 ID/摘要、Alembic revision、配置版本和可恢复快照。
3. 在隔离数据库恢复一份备份，测试新镜像的迁移、核心验收和与旧数据的兼容。
4. 构建新镜像，单实例启动，核查 live/ready、请求隔离、少量限额调用和账本。
5. 回滚不能只换镜像。若数据库迁移不可向后兼容，使用匹配旧版本的经验证备份并说明自备份后数据如何核对；不直接在生产运行 `alembic downgrade base`。

升级依赖时同步维护 `pyproject.toml`、`uv.lock` 和 `requirements.lock`，重新构建按哈希安装的镜像。依赖安装/镜像拉取需要网络；不要因拉取失败临时改成未锁定 latest。

## 隐私维护与泄露

`RETENTION_DAYS` 默认 90，维护脚本清理过期管理员及学生会话并将旧请求的原始 IP 置空，保留计量、账本和审计。下列命令只验证隔离测试库：

```bash
.venv/bin/python scripts/local.py .venv/bin/python scripts/housekeeping.py
```

在线维护需在目标实例的运行环境中执行 `scripts/housekeeping.py`，注入该实例的配置/秘密；不要使用会改写连接地址的 `scripts/local.py`。容器执行时需按受控方式提供 scripts 目录；脚本无需付费上游。当前没有自动删除财务明细；可按时间查询聚合历史。长期数据归档策略应按实验室保留要求部署，避免删账本影响核对。

个人 Key 泄露：吊销、核查对应时间/费用/来源，再为原成员轮换，保持预算。官方 Key 泄露：管理员在官方平台轮换并更新组凭证，同时检查服务器、日志和备份。

迁移时先发新的服务器专用官方 Key，完成试点与客户端迁移后，再按批准的批次/窗口撤销旧共享 Key。撤销会影响仍在使用旧 Key 的程序，不能作为开发测试执行。

预算消费事件阈值可通过 `BUDGET_THRESHOLDS` 配置为逗号分隔的百分比（默认 `80,100`，范围1–100）；事件按预算周期去重。更改受限配置后重启唯一网关生效，不会变更资金账本。

## 记录与追溯升级

`d58b6f2a901e` 将预算策略改为请求前检查已结算费用，取消资金预留。停下旧网关、等待进行中请求结束后再迁移并启动新版。迁移将旧周期未释放的预留写成 `reserve_release` 账本记录并清零，同时清零未结算调用的 `reservation`；历史已结算费用、原 reserve/settle 账本及待核对请求保留。旧字段仍用于兼容历史报表，新请求的预留恒为 0。回滚此迁移不会重新冻结已经解除的旧预留。

聊天请求体和 Nginx 聊天 location 的限制同步提高到 48 MiB；管理接口仍保持 256 KiB。使用已有反向代理时也需更新该聊天路径的限制，否则仍可能由代理返回 413。图片数据仅转发，不落库存储。没有 usage 的请求需要人工核对，费用未计入前可能继续放行其他请求，这是取消预留后的预期行为。

发布时同时更新 `gateway/` 与 `migrations/`，启动前执行 `alembic upgrade head`（容器启动流程已执行）。`c42e8a910d65` 增加独立调用结果、拒绝记录及告警处理字段，并更新准入函数以返回命中的限额归属。历史费用与预算不变；历史调用结果保守标为未知，历史告警次数从 1 起算。不要只替换静态页面或只更新 ORM。

`GET /admin/api/metrics` 的 `rejection_record_failures` 是本进程拒绝记录写入失败次数；对应安全日志为 `rejection_record_failed request_id=...`。数据库故障期间拒绝记录可能缺失，进程重启会重置此运行指标，应结合服务器日志核查。拒绝持久化失败不改变原拒绝响应。

`scripts/housekeeping.py` 同时清理调用和拒绝记录的过期原始 IP，保留用量、拒绝元数据和审计历史。新增拒绝逐次落库，应将该表纳入原有数据库备份和容量监控。

## 隔离验证工具

开发/故障演练必须使用独立 Compose 项目；禁止将 `compose.test.yaml` 叠加到在线项目。`scripts/local.py` 默认使用 `lab-gateway-test`，读取本地秘密并将数据库/Redis 指向测试端口，固定上游为本地 MOCK。测试和故障演练会核对项目发布端口与实际连接地址。不运行测试时可删除该项目的容器和卷；不要对在线项目执行同样操作。

```bash
export GATEWAY_COMPOSE_PROJECT=lab-gateway-test
docker compose -p "$GATEWAY_COMPOSE_PROJECT" -f compose.yaml -f compose.test.yaml up -d --wait postgres redis
.venv/bin/python scripts/local.py .venv/bin/python scripts/latency_benchmark.py --rounds 10
.venv/bin/python scripts/local.py .venv/bin/alembic upgrade head
.venv/bin/python scripts/local.py .venv/bin/python scripts/restore_drill.py
.venv/bin/python scripts/secrets_restore_drill.py
```

`latency_benchmark.py` 自行创建/关闭网关和 MOCK、创建/删除临时数据库，统一承接原 `benchmark.py` 的直连/网关对比，并支持同 Key、同组、跨组分布。旧入口已移除。不同压测版本的统计口径以各自报告为准。数据库恢复演练要求隔离项目的 `lab_gateway` 已执行迁移，可先运行 `.venv/bin/python scripts/local.py .venv/bin/alembic upgrade head`。

以下工具需要手动启动的独立 MOCK 网关，示例地址为 `http://127.0.0.1:19082`。按顺序在不同终端启动 MOCK 和网关：

```bash
.venv/bin/uvicorn scripts.mock_upstream:app --host 127.0.0.1 --port 18082
# 另一个终端，显式覆盖 PUBLIC_URL 使 Origin 校验与浏览器地址一致：
.venv/bin/python scripts/local.py env PUBLIC_URL=http://127.0.0.1:19082 \
  .venv/bin/uvicorn gateway.main:create_app --factory --host 127.0.0.1 --port 19082
# 第三个终端（沿用上文的 GATEWAY_COMPOSE_PROJECT）：
export GATEWAY_TEST_URL=http://127.0.0.1:19082
.venv/bin/python scripts/network_check.py
.venv/bin/python scripts/browser_check.py
.venv/bin/python scripts/student_browser_check.py
.venv/bin/python scripts/local.py .venv/bin/python scripts/crash_drill.py
.venv/bin/python scripts/local.py .venv/bin/python scripts/dependency_drill.py
```

这些客户端工具拒绝隐式地址、外部地址及在线默认端口 18081，并验证目标配置允许 MOCK 清理，创建自己的项目组/身份。`dependency_drill.py` 只停止显式测试项目的依赖，同时核验目标网关写入了同一隔离数据库；不要与 pytest/压测并行执行。`crash_drill.py` 只杀死自己创建的 19081 端口进程并清理自己的临时库；它依赖 18082 端口 MOCK。浏览器使用 `GATEWAY_CHROMIUM_PATH` 或 Playwright 默认 Chromium。需要测试密码覆盖时设置 `GATEWAY_TEST_PASSWORD`，默认读取本地演示管理员密码文件。

所有运行报告默认写到 `artifacts/`，可设置 `GATEWAY_ARTIFACT_DIR`。归档仅保留[验证记录](archive/verification.md)列出的基线和边界数据。备份/恢复命令未设置 `GATEWAY_COMPOSE_PROJECT` 时仍针对原在线项目；执行演练时必须设置为独立项目，备份文件同名冲突会报错并保留原文件。

Harness 回归使用独立安装的客户端依赖，不加入网关运行依赖：

```bash
npm install --prefix /tmp/harness-client-check --ignore-scripts --no-audit --no-fund @earendil-works/pi-ai@1.3.0
PI_AI_TEST_ROOT=/tmp/harness-client-check .venv/bin/python scripts/local.py .venv/bin/pytest -q
```


## 可选容量保护和图片文件升级

`e91c24d8b630` 将成员旧默认 RPM=20、并发=2 分别改为 0；非默认自定义值保留，已有组配置和预算、费用、Key 均保持。0 表示关闭该层容量限制。升级须停止旧进程后运行迁移再启动新版，避免旧准入函数把 0 当成全部拒绝。回滚会把 0 恢复为旧默认值，并移除 Files 归属表；已上传的上游文件不会随数据库回滚自动删除，回滚前应导出归属记录并安排清理。

新增 `uploaded_files` 元数据表（没有图片原文）和拒绝记录 `details` 字段，应纳入现有备份。上传只开放图片 `purpose=user_data`，不会开启收费文档解析；模型引用依旧按官方 usage 和已配置价格结算。文件永久保存或按调用者指定期限过期；删除走原上传凭证，项目组换凭证后引用需重传。

反向代理除聊天 48 MiB 外，还须放行 `/v1/files`（上传体 65 MiB）和 `/v1/files/{id}`；示例配置已同步。服务使用临时文件处理 multipart，需保证 `/tmp` 容量；不在日志记录请求体、图片、密钥或上游原始错误。`file_cleanup_failed` 表示上传成功后的本地保存失败，且自动清理也未成功，应核查上游文件列表。

`capacity_queue` 指标为正在等待容量的请求数；`current_concurrency` 是已经准入的模型请求数。当前 HTTP 连接池最多 200 条连接，保留现有组容量保护，不能把上游额度直接当作本机承载能力。扩大组并发前应在隔离环境压测。
