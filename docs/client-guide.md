# 学生接入指南

登录学生入口 `/student`，首次修改密码后领取所属项目组的个人 Key。登录后的“接入指南”按所选项目组、模型和终端环境生成可复制示例，覆盖聊天客户端、Python、Node.js、Agent 项目、DeepSeek Harness 和 HTTP 验证。先领取 Key，再选择同一组的模型；网页登录密码不是 API Key。不同项目组的 Key 分别归因和计量。

Key 仅在领取或轮换成功时完整显示一次。保存后可以退出网页继续调用，遗失时由学生本人轮换。管理员重置登录密码会使该学生旧登录会话和所有旧 Key 失效，学生改密后重新领 Key。校外调用需要运行客户端或 Harness 的设备能访问网关，例如保持校园 VPN 连接。

## 接口格式与范围

本网关提供 **OpenAI Chat Completions 兼容接口**。这是请求和响应格式，模型及用量归属实验室配置的 DeepSeek 上游，不使用学生的 OpenAI 账号。

| 配置 | 填写内容 |
| --- | --- |
| 提供商类型 | OpenAI 兼容 / Chat Completions |
| Base URL | 学生页面显示的完整接入地址，末尾为 `/v1` |
| API Key | 当前项目组的完整个人 Key |
| 模型 ID | 当前项目组允许的模型，或用该 Key 查询 `/v1/models` 得到的 ID |
| 完整聊天地址 | Base URL 后追加 `/chat/completions` |

SDK 的 `base_url` / `baseURL` 填到 `/v1` 即可，不要再追加 `/chat/completions`。本网关支持文本、SSE 流式和客户端 `function` 工具调用；目前不提供 OpenAI Responses、Anthropic Messages、图片/文件上传、Embeddings 和 DeepSeek 官方内置联网接口。不要把上游官网的全部功能当成本网关的功能。

DeepSeek 官方的 [首次调用说明](https://api-docs.deepseek.com/zh-cn/) 提供 OpenAI 格式示例；以下示例已按本网关接口范围改写。核对日期：2026-11-14。

## 先设置环境变量

学生页面会自动填入实际地址和模型。下面的占位符需要替换；Key 通过隐藏输入设置，不写入脚本或仓库。

```bash
export LAB_LLM_BASE_URL='https://实际网关地址/v1'
export LAB_LLM_MODEL='本组开放的模型 ID'
read -rsp '粘贴本组个人 Key（输入隐藏）: ' LAB_LLM_API_KEY
printf '\n'
export LAB_LLM_API_KEY
```

Windows PowerShell：

```powershell
$env:LAB_LLM_BASE_URL = 'https://实际网关地址/v1'
$env:LAB_LLM_MODEL = '本组开放的模型 ID'
$secret = Read-Host '粘贴本组个人 Key' -AsSecureString
$env:LAB_LLM_API_KEY = [System.Net.NetworkCredential]::new('', $secret).Password
Remove-Variable secret
```

环境变量属于当前终端，在同一终端运行脚本或启动 Harness 才能读取。页面同时提供 PowerShell HTTP 验证示例。

## 先验证地址和 Key

查询模型列表不生成模型回答：

```bash
curl --fail-with-body "$LAB_LLM_BASE_URL/models" \
  -H "Authorization: Bearer $LAB_LLM_API_KEY"
```

返回 `data` 中的模型 ID 才是该 Key 实际可用的模型。随后可运行下方短消息；消息请求会实际计量。

## Python

安装 `python -m pip install openai`，保存为 `demo.py`，运行 `python demo.py`：

```python
import os
from openai import OpenAI

client = OpenAI(
    api_key=os.environ['LAB_LLM_API_KEY'],
    base_url=os.environ['LAB_LLM_BASE_URL'],
    max_retries=0,
    timeout=180,
)
reply = client.chat.completions.create(
    model=os.environ['LAB_LLM_MODEL'],
    messages=[{'role': 'user', 'content': '你好，请用一句话介绍你能帮我做什么。'}],
    max_tokens=256,
    extra_body={'thinking': {'type': 'disabled'}},
)
print(reply.choices[0].message.content)
```

流式调用沿用 `client`：

```python
with client.chat.completions.create(
    model=os.environ['LAB_LLM_MODEL'],
    messages=[{'role': 'user', 'content': '解释什么是 API。'}],
    max_tokens=512,
    stream=True,
    stream_options={'include_usage': True},
    extra_body={'thinking': {'type': 'disabled'}},
) as chunks:
    for chunk in chunks:
        for choice in chunk.choices:
            if choice.delta.content:
                print(choice.delta.content, end='', flush=True)
```

## Node.js

安装 `npm install openai`，保存为 `demo.mjs`，运行 `node demo.mjs`：

```javascript
import OpenAI from 'openai';

const client = new OpenAI({
  apiKey: process.env.LAB_LLM_API_KEY,
  baseURL: process.env.LAB_LLM_BASE_URL,
  maxRetries: 0,
  timeout: 180_000,
});
const reply = await client.chat.completions.create({
  model: process.env.LAB_LLM_MODEL,
  messages: [{role: 'user', content: '你好，请用一句话介绍你能帮我做什么。'}],
  max_tokens: 256,
  thinking: {type: 'disabled'},
});
console.log(reply.choices[0].message.content);
```

## 在 Agent 项目里使用

将项目的 `base_url` / `baseURL`、`api_key` / `apiKey`、`model` 分别映射到上面三个环境变量。如果框架区分 Chat Completions 与 Responses，选择前者，调用 `client.chat.completions.create(...)`。只把 Key 换成实验室 Key、地址仍指向官方服务，无法通过网关调用。

工具执行由 Agent 项目负责。把工具声明放进 `tools` 的 `function` 定义；模型返回 `tool_calls` 后执行对应的本地函数，再通过 `role: tool`、对应 `tool_call_id` 和字符串形式的结果继续请求模型。学生页面的“Agent 项目”页签提供可复制的最小循环，最多执行四轮，并严格构造本网关支持的消息字段。

每轮模型请求单独计量。先用请求并发 1 验证，再按管理员允许的个人/组并发提高。不要无限循环或无条件重试：429 需要降低并发并等待；网络超时后上游可能已经执行，重复发送可能再计一次用量。

## DeepSeek Harness

先按 [官方项目说明](https://github.com/deepseek-ai/deepseek-harness) 安装并启动。下面采用官方的 [自定义模型 API 配置流程](https://deepseek-harness.github.io/deepseek-harness/guide/providers)，不通过默认的 DeepSeek 官网提供商登录。

1. 打开“设置 → 模型 → 添加模型提供商 → 自定义模型 API”。
2. Provider ID 填 `lab-gateway`，显示名称填“实验室网关”。
3. API 协议选择 **OpenAI Chat Completions**（`openai-completions`）；Base URL 填学生页面的 `/v1` 地址，API Key 填本组个人 Key。
4. 选择“获取可用模型”，或手动添加当前组的模型 ID；输入类型只选 `text`。
5. 基础对话保存后即可使用；需要配置思考、重试或工具回放时，可将下方兼容配置合并到当前 profile 的 `cordis.patch.yml`。保留已有其他提供商与字段，不要直接覆盖整个文件。常规 `dsh web` 的路径是 `$DSH_HOME/profiles/web/cordis.patch.yml`。
6. 使用环境变量凭据时，在启动 Harness 的终端设置 `LAB_LLM_API_KEY` 再运行 `dsh web`。若使用模型设置页保存的 Key，保留该页面生成的 `apiKeyEnv` 凭据引用，不要用下面的环境变量引用覆盖它。
7. 新建会话，选 `lab-gateway` 下的模型，先发短消息，再试读项目文件或终端工具。

```yaml
# 将本组实际地址、模型和输出上限填入；学生页面会自动生成。
- id: llm-pi-ai
  config:
    providers:
      lab-gateway:
        displayName: 实验室网关
        api: openai-completions
        baseURL: https://实际网关地址/v1
        apiKeyEnv: LAB_LLM_API_KEY
        reasoning: "off"
        retryPolicy:
          mode: normal
          maxRetries: 0
        compat:
          supportsStore: false
          supportsDeveloperRole: false
          maxTokensField: max_tokens
          supportsUsageInStreaming: true
          thinkingFormat: deepseek
          requiresReasoningContentOnAssistantMessages: true
        models:
          - id: 本组开放的模型ID
            input: [text]
            maxTokens: 2048
            reasoningEfforts:
              "off": null
              low: low
              high: high
```

网关已兼容 Harness 默认参数：接受 `store: false` 并在转发前移除；`developer` 指令转换为 DeepSeek 的 `system`；`max_completion_tokens` 转换为 `max_tokens`，较大的客户端额度会在预算准入前压到项目组上限。`store: true` 不支持。模型列表会返回该组的输出上限，重新获取模型可更新客户端默认值。

可选兼容项依据 Harness 的 [配置字段参考](https://deepseek-harness.github.io/deepseek-harness/en/reference/config-catalog)：`supportsStore: false`、`supportsDeveloperRole: false` 和 `maxTokensField: max_tokens` 可减少转换；`thinkingFormat: deepseek` 对接思考开关。推理等级支持 `none / low / high / max`，并依据 [DeepSeek 官方参数说明](https://api-docs.deepseek.com/api/create-chat-completion/) 将 `minimal` 映射到 `low`、`medium / xhigh` 映射到 `high`。项目组原有输出上限不变。

本地文件、终端、函数工具与模型请求是不同环节。此网关可承载相应的文本/函数调用请求，但未开放 DeepSeek 官网额外的上传、图片、联网和 Responses API。不要直接按默认官网能力开启这些功能。

验证范围：Python、HTTP 与工具调用可用模拟上游验证；Node.js 示例做语法检查；Harness 配置按官方字段和本网关协议适配，尚未完成 Harness 完整应用的端到端验收。

## 排错

| 现象 | 处理 |
| --- | --- |
| 连接失败 | 检查运行脚本或 Harness 的设备能否访问网关，校外检查 VPN |
| 401 | 检查个人 Key 是否错误、到期或已轮换 |
| 403 | 检查组/成员状态、模型白名单及是否使用对应组的 Key |
| 402 | 组或个人预算不足以覆盖预留，降低输出上限或联系管理员 |
| 429 | 降低请求频率和并发，等待后再试 |
| 404 / 405 | 检查是否重复 `/v1`，是否误用 Responses / Anthropic 模式 |
| 413 / 422 | 413 请缩短上下文；422 查看 `error.details` 中的字段路径与固定错误码。未知参数、`store: true` 和未开放多模态仍会被拒绝 |
| 502 / 503 | 保留 `X-Request-ID` 联系管理员，避免无条件重复请求 |

本机示例使用环境变量保存 Key，页面复制的配置中不会包含真实 Key。不要把个人 Key 写进项目仓库或公开日志。客户端断开或超时不代表上游没有执行，缺少最终 usage 的请求可能进入待核对。
