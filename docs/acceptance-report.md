# 开发与模拟验收报告

日期：2026-11-14，时区 Asia/Shanghai。初次验收及模拟测试未使用真实付费调用；本轮经用户明确授权切换真实 DeepSeek，并完成两笔短消息联调，详见文末。未执行真实学生迁移或旧官方 Key 撤销；已按用户要求开放校园网 IP 入口，未新建公网入口。

## 结果与交付状态

已交付可维护源码、锁定依赖与镜像、Alembic 初始及学生自助账户迁移、运行中的单机 Compose 模拟环境、管理界面、备份恢复工具及操作文档。初次29项集成测试通过；延迟优化后37项测试通过。另执行真实套接字断连、实际进程 SIGKILL、真实数据库/缓存容器故障、完整数据库与秘密恢复、浏览器验收及 SDK 压测，各轮范围与边界见对应章节。

初次验收使用回环地址。随后按用户要求，当前入口已改为 `0.0.0.0:18081`，管理地址：<http://10.108.13.250:18081/admin>，并同步更新 `PUBLIC_URL`。实际确认监听地址、IP 访问的管理页 HTTP 200 和就绪检查通过；Chromium 经该 IP 完成登录、全部八个管理页、预算修改及退出，无页面错误。该验证从服务器发起，尚未直接验证用户另一台电脑的网络路径。管理员账号 admin；演示密码只在本机 `secrets/demo_admin_password`。本次没有在聊天、日志或文档中输出完整凭证。原有模拟 A/B 两组、三名学生及独立性能服务身份继续保留；另新增三名自助模拟学生和浏览器验收身份；初次验收所用上游凭证均为 MOCK 标识；之后用户自行配置的官方凭证及本轮真实调用见文末。

目前完成了两笔真实 API 联调，尚未完成 HTTPS 与官方账单核对，不作为完整生产验收。最新完整回归 79 项通过。后续延迟优化的独立突发准入已达到 p95 <100ms，持续高频仍受逐笔结算吞吐限制；见文末“延迟优化复验”。没有配置异地备份、系统级备份定时器或外部通知。

## 验收表

| 编号 | 结果 | 实际证据与边界 |
| --- | --- | --- |
| A01 | 模拟通过 | 管理开通姓名、字符串学号、组绑定；Key 可调用；凭证数量不随学生增加 |
| A02 | 模拟通过 | 同组两人上游实际收到 A-credential；个人/组汇总及 188 微元样例费用一致 |
| A03 | 模拟通过 | A/B 上游实际收到各自标识；客户端组、渠道、地址、user、Key 参数均拒绝 |
| A04 | 模拟通过 | 官方模拟凭证轮换后个人 Key 不变，新请求换凭证，Call 保存旧/新凭证 ID |
| A05 | 模拟通过 | 禁用、过期 Key 在调用前拒绝；团队停用也在调用前拒绝 |
| A06 | 模拟通过 | Key 轮换仍为同一成员预算周期，旧 Key 拒绝；已用不刷新 |
| A07 | 模拟通过 | 未授权模型、其他协议、图像内容块、内置工具与未知参数拒绝；客户端函数调用完成往返 |
| A08 | 模拟通过 | 个人与组分别用 1.5 倍单次预留余额竞争 30 个事务，只允许 1 笔；另验证 30 个准入与结算交叠，无死锁 |
| A09 | 边界模拟通过 | 北京时间自然日/月与闰月；历史请求按固定周期/价格结算。没有等待真实跨月运行 |
| A10 | 模拟通过 | 输入100、输出20、缓存命中40、推理细项10，按2/0.2/3元每百万计费188微元，不重复加推理量 |
| A11 | 模拟及网络通过 | CRLF、17字节拆包、保活、结束状态 chunk usage、空 choices usage、DONE；真实首有效增量 0.238 秒早于慢上游完成 |
| A12 | 真实本机网络通过 | SSE 提前关闭与非流式客户端超时均取消上游连接、进入 pending、保留预留 |
| A13 | 模拟通过 | 十个并发结算只有一笔生效；重复结算/恢复不重复扣款；人工原因与审计同事务 |
| A14 | 实际进程演练通过 | 新隔离库中途 SIGKILL，再启动恢复 pending，预留不变，只有双 reserve 账目；重启 0.682 秒 |
| A15 | 实际容器故障及模拟通过 | 停止本项目 Redis/PostgreSQL，新收费请求503、存活检查正常；恢复后预算/请求计数一致；Redis flush 不重置持久化速率和并发 |
| A16 | 模拟通过 | 伪造 X-Forwarded-For 不改变未信任连接 IP；可信链 IPv6/IPv4 地址解析验证。生产代理链尚未配置 |
| A17 | 通过 | 匿名、学生 Bearer Key 无管理权限；Cookie HttpOnly/SameSite，修改必须 CSRF，错误来源拒绝 |
| A18 | 通过（本项目交付物） | 签发 Key 不在列表/审计/CSV；上游凭证密文；错误不回显输入；对交付文件与本地运行秘密做匹配扫描。初始目录没有 Git 仓库，未创建提交 |
| A19 | 模拟通过 | 时间/组/学生/Key/模型筛选、统计缓存量与费用、pending单列；CSV用户字段公式转义 |
| A20 | 实际恢复通过 | 数据库各表逐行一致，含待核对、Key摘要、预算、账本、审计；秘密包恢复到受限临时目录逐文件一致，备份解密密钥未打包 |
| A21 | SDK/独立突发目标达成，持续容量有限 | 初次41笔样本后补充新旧各1380笔独立突发及10rps测试，零错误、正确结算；新版完整准入p95均低于100ms。连续高频另有78笔并发限制拒绝，详见文末。未验证真实上游与生产HTTPS |
| A22 | 模拟通过 | 相同开通操作并发重试10次，只保留一个成员和一个Key，重试不再次返回完整Key；不同载荷冲突 |
| A23 | 集成及真实浏览器通过 | 管理批量导入学号、保留前导零、幂等与全批事务；重复导入不覆盖已有密码或预算 |
| A24 | 集成及真实浏览器通过 | 学号登录，服务端强制先改密；未改密不能领 Key；改密轮换会话；禁止公开注册 |
| A25 | 集成及真实浏览器通过 | 学生只能为自己的成员关系领 Key；原文单次显示；并发只签发一把；显式轮换吊销旧 Key，已用预算不变 |
| A26 | 集成及真实浏览器通过 | 管理员重置生成随机临时密码，撤销该学生全部会话及 Key，再次登录强制改密；临时密码不落审计 |
| A27 | 集成通过 | CSRF、Origin、管理角色和学生间隔离；登录按 IP 与账号限流；生产拒绝学号初始密码；自助签发遵守到期上限 |
| A28 | 实际恢复通过 | 新迁移后的学生密码摘要及会话与全部既有预算、账本逐表逐行恢复一致 |

## 初次验收性能（优化前，保留历史证据）

使用同机模拟上游，一条 user 消息 `hello`，最大输出128，标准 SDK，关闭自动重试。模拟每20ms输出一次有效增量，共3段。直连和网关使用同样输入/输出参数，1/10/30各为一轮突发样本；不是长期容量或真实 DeepSeek 体验承诺。

| 路径 | 并发 | 首有效增量 p50 / p95 ms | 总耗时 p50 / p95 ms | 错误数 |
| --- | --- | --- | --- | --- |
| direct_mock | 1 | 113.66 / 113.66 | 153.08 / 153.08 | 0 |
| gateway | 1 | 194.2 / 194.2 | 231.95 / 231.95 | 0 |
| direct_mock | 10 | 31.29 / 34.69 | 81.45 / 84.18 | 0 |
| gateway | 10 | 159.7 / 320.41 | 201.24 / 363.45 | 0 |
| direct_mock | 30 | 44.15 / 46.47 | 95.99 / 98.83 | 0 |
| gateway | 30 | 586.04 / 949.65 | 630.73 / 992.5 | 0 |

网关准入 p50 **375 ms**，p95 **831 ms**；正确结算 **41/41**。当前按组串行化准入并保留完整事务持久化，30并发突发时会排队；未达到交接文档建议的 p95 <100ms，不能宣传已达到该目标。首版适合受限并发试点，实际规模应按配置与体验复核。预留/结算账本不因性能目标被省略。

原始非敏感结果：`performance-results.json`、`network-results.json`、`browser-results.json`、`dependency-results.json`、`crash-results.json`。

## 备份恢复与安全验证

实际恢复到 `lab_gateway_restore_20261114_071109`，各表逐行一致。样本含 8 个身份、6 个Key、158条请求、622条账目和 129条审计；另包含 4 个学生密码摘要及 1 条学生会话。恢复命令至逐行比较耗时 2.393 秒；这是本机小数据样本，不是生产 RTO。

备份使用 pg_dump 一致性快照；演练使用导出的相同快照避免并发写入造成错误比较。另恢复加密秘密包并验证8个受限文件一致。认证加密测试验证篡改与截断会失败。原始 IP 保留可按 RETENTION_DAYS 清理，记账记录保留。已生成的备份都在本机受限目录；异地恢复能力未验收。

## 可复现命令

```bash
uv sync --locked
docker compose -f compose.yaml -f compose.test.yaml up -d --wait postgres redis
.venv/bin/python scripts/local.py .venv/bin/pytest -q
.venv/bin/ruff check gateway scripts tests migrations
node --check gateway/static/app.js
.venv/bin/python scripts/check_secrets.py
```

运行 Compose 模拟网关后，以下均只使用 MOCK 凭证；压测直连基线需要先在另一终端启动本机 mock：

```bash
.venv/bin/uvicorn scripts.mock_upstream:app --host 127.0.0.1 --port 18082 --no-access-log
# 在另一终端运行：
.venv/bin/python scripts/benchmark.py
.venv/bin/python scripts/network_check.py
.venv/bin/python scripts/browser_check.py --base-url http://10.108.13.250:18081
.venv/bin/python scripts/student_browser_check.py
.venv/bin/python scripts/local.py .venv/bin/python scripts/crash_drill.py
.venv/bin/python scripts/local.py .venv/bin/python scripts/dependency_drill.py
.venv/bin/python scripts/local.py .venv/bin/python scripts/restore_drill.py
.venv/bin/python scripts/secrets_restore_drill.py
```

浏览器脚本使用本机已安装的 Chromium，生产依赖不包含浏览器。首次开发环境可用锁定的 Playwright 安装 Chromium，并按实际安装位置调整脚本。`dependency_drill.py` 会短暂停止本项目数据库和Redis，只能在隔离测试环境执行；`crash_drill.py`只对自己新建的测试进程发送SIGKILL。

## 生产剩余配置

用户已允许测试名单与预算使用模拟数据，已配置三名新自助学生，每人每月10元。聊天中出现的官方Key未写入项目或接入上游，建议先作废换新，再通过受保护的后台录入。官网模型及人民币价格已导入，按官方高峰价保守估算。可信HTTPS入口（域名或IP证书均可）、校外路由、异地备份和维护计划仍待安排。现已完成两笔真实付费请求；官方账单比对、HTTPS代理流式验证、小范围真实试点和旧官方Key撤销尚未执行。当前上游已切换为真实 DeepSeek；校外网络方案见 access-and-accounts.md。


## 学生自助入口部署

学生页面为 <http://10.108.13.250:18081/student>，根地址跳转至此。模拟账号 2026001001、2026001002、2026001003，首次密码为各自学号，改密后领取 Key。Chromium 在实际 IP HTTP 入口验证管理员导入表单、强制改密、领 Key 与轮换、重置密码及旧会话/Key 失效，无页面错误；结果见 `student-browser-results.json`，截图 `student-preview.png` 不包含完整 Key。

本次只开放已有网关入口；未安装校园 VPN、组网软件或公网隧道。学生门户初次验收未重测性能；后续优化结果见下节。

## 延迟优化复验（2026-11-14）

37项集成/回归测试通过，含新增的批内独立预算拒绝、提交后取消退款、整批提交失败回滚、排队期间权限复核、完整提交计时、SSE保活与取消、准入/结算公平调度、导入密码计算不持组锁。旧数据加密备份恢复到独立库后，执行升级→回退→再次升级，各应用表逐行一致（158条请求、622条账目）；真实SIGKILL后预留保留，恢复为pending，无自动退款或重复结算。

使用 `scripts/latency_benchmark.py`，旧版源码快照和新版依次在独立临时库运行，直连/网关指向同一个本机mock进程，Python SDK关闭重试。每条路径预热50笔，每种身份分布、1/10/30并发各10轮，轮次交替直连/网关顺序。**这是独立突发测试，每轮结束等待结算排空；排空时间另行记录，不计入首输出时间。** 并发1每档只有10个样本，仍不构成长期容量承诺。

| 场景 | 旧网关首有效输出p95 ms | 新网关首有效输出p95 ms | 本轮直连p95 ms | 新完整准入p95 ms |
| --- | ---: | ---: | ---: | ---: |
| 同一身份，1并发 | 72.16 | 47.85 | 26.60 | 17 |
| 同一身份，10并发 | 276.55 | 99.48 | 37.57 | 44 |
| 同一身份，30并发 | 862.36 | 141.23 | 62.00 | 72 |
| 同组30名不同学生 | 1058.96 | 146.27 | 50.49 | 74 |

分散3组的30并发，新网关首有效输出p95为133.26ms，完整准入47ms。额外固定10请求/秒、100笔测试，新网关首有效输出p95为60.00ms，直连25.84ms，完整准入31ms，零错误。上述p95之间的差值不是配对的逐请求额外延迟分位数；新旧准入计时也不同，旧值不含提交，故主要比较客户端首输出指标。

新旧两版各1380笔网关模拟请求全部以188微元正确结算，各5520条账目，预留归零，数据库/上游/结算错误均为0。新版本同组30人首输出p95下降约86%，独立突发准入达成100ms目标；30并发下首输出仍高于直连，尚不能声称所有负载下额外延迟都低于50ms或完全无感。原始数据为 `latency-baseline-results.json`、`latency-optimized-results.json`。

另运行 `--continuous`，不等待每轮结算排空，保留持续突发的容量边界：1280笔网关尝试（含预热），1202笔接受并正确结算，78笔因并发上限返回429，预留最终归零。数据库、上游、结算错误为0；此轮**不记为零错误性能通过**。组并发上限100包含尚未完成结算的请求，不能靠调大上限或忽略预留掩盖结算积压。见 `latency-continuous-results.json`。批量结算改动被自动审批拒绝后未实施，保留原有逐笔结算事务。

复现使用独立测试依赖，避免重启正在运行的演示数据库：

```bash
docker compose -p lab-latency-test -f compose.yaml -f compose.test.yaml up -d --wait postgres redis
.venv/bin/python scripts/local.py .venv/bin/pytest -q
.venv/bin/python scripts/local.py .venv/bin/python scripts/latency_benchmark.py --rounds 10 --steady
# 单独进行连续高频容量测试；有429会保存结果并以非零状态退出：
.venv/bin/python scripts/local.py .venv/bin/python scripts/latency_benchmark.py --rounds 10 --continuous --output docs/latency-continuous-results.json
```

新旧对比可通过 `--source-root` 指向对应完整源码快照。全部为模拟调用，真实供应商、生产HTTPS、校园终端网络与长时间容量验收仍未执行。

已部署到现有校园网模拟入口，数据库版本为 `9d31b47fa820`，容器代码与工作区摘要一致。live/ready/学生页/管理页均HTTP 200；真实套接字断连与SDK函数调用复验通过。部署后排除一次客户端预热的6笔单请求首有效输出为41.48～54.18ms；首次网络检查的冷路径为678ms，不能以热路径数据承诺冷启动体验。详见 `latency-deployment-results.json`。本次独立测试容器、测试数据卷及迁移恢复临时库已清理，旧镜像与加密备份保留。


## Harness 兼容修复与真实上游联调

本轮使用 Harness 当前依赖的 `@earendil-works/pi-ai@1.3.0` 构造自定义提供商的默认请求，实际复现四处 422：`developer` 指令、`store: false`、`max_completion_tokens: 128000` 及 `reasoning_effort: max`。原请求未保存，不能根据旧 request_id 还原用户输入；复现仅使用合成测试消息。

网关现在将 developer 转换为 system，接受并移除 store:false，接收客户端较大的输出额度并在预算准入前压到项目组上限，保留原组输出限制。按 DeepSeek 官网接受 none/low/high/max，并将 minimal 映射到 low、medium/xhigh 映射到 high。模型列表返回该 Key 所属组的实际输出上限。未知参数、store:true、未开放多模态和客户端选路参数仍拒绝。422 响应和日志仅增加受控字段路径及固定错误码，不回显输入、密钥、Pydantic 原始消息或未知字段名称。

隔离测试累计 78 项通过，含实际 pi-ai 客户端经网关与模拟上游的两次流式请求、输出截断、usage 结算、推理别名及错误脱敏。可选依赖检查的复现方式（npm 仅安装到临时目录，不加入网关运行时依赖）：

```bash
npm install --prefix /tmp/harness-client-check --ignore-scripts --no-audit --no-fund @earendil-works/pi-ai@1.3.0
PI_AI_TEST_ROOT=/tmp/harness-client-check .venv/bin/python scripts/local.py .venv/bin/pytest -q tests/test_harness_compatibility.py
```

经用户明确选择真实 DeepSeek 后，仅将 UPSTREAM_URL 改为 `https://api.deepseek.com/v1`，保留现有校园网入口与项目组/成员/Key/预算。升级后、真实调用前，逐行指纹核对原 12 张业务表记录全部一致，且未新增学生账号。已生成数据库和独立加密配置备份。

以现有“707实验室”个人 Key 对 deepseek-flash 执行两笔真实请求，每笔最多输出 64 Token，显式关闭思考；普通请求与实际 pi-ai 客户端流式请求均返回“连接成功”。请求 ID 分别为 `286dbf2a-b5f7-4c62-938e-a2f45844c17e`、`29b62edc-dbcc-4fd6-bcf5-8fc145438b40`；每笔官方 usage 为输入 22、输出 2 Token，上游 HTTP 200，网关状态 settled，按配置估算每笔 60 微元，合计 ¥0.000120。仅对应两笔新调用及正常预算/账本发生记账，没有清空历史数据。实际 HTTP 校园网路径已验证；完整 Harness 应用（含 UI、会话持久化及全部工具）未整体验收。

证据：`harness-client-results.json`、`harness-compatibility-results.json`；官方参数依据：[DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)。

## 管理弹窗中文输入修复

取消原因字段统一最少 5 字的前端限制，预算调整、密码重置与人工核对均允许“测试”等简短原因；后端去除首尾空白后要求非空，上限仍为 500 字，审计保留。弹窗不再使用浏览器原生校验气泡，只在明确点击保存时显示行内错误；隐式表单提交被阻止，不拦截输入法选词、空格或文本框回车换行。

累计 79 项隔离测试通过。Chromium 使用 CDP 输入法组合事件验证拼音组合、空格、提交候选文字及回车换行，未产生预算请求或原生 invalid 事件；空白原因在点击保存后行内提示，两字原因保存成功且写入审计。另回归密码重置短原因及其他管理流程。当前运行页面仅做打开、输入与关闭验证，没有保存预算、重置密码或发起模型调用。证据：`admin-input-preview.png`、`admin-input-results.json`。
