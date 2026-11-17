"use strict";
(() => {
  const make = (tag, text, cls) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  };
  const shellQuote = (value) =>
    "'" + String(value).replaceAll("'", "'\\''") + "'";
  const json = (value) => JSON.stringify(value);
  const sources = [
    ["DeepSeek 官方：首次 API 调用", "https://api-docs.deepseek.com/zh-cn/"],
    [
      "DeepSeek Harness：配置模型",
      "https://deepseek-harness.github.io/deepseek-harness/guide/providers",
    ],
    [
      "Harness：兼容参数参考",
      "https://deepseek-harness.github.io/deepseek-harness/en/reference/config-catalog",
    ],
  ];
  let groupId = "",
    modelId = "",
    selectedTab = "client",
    selectedShell = "bash";

  async function copy(text, button) {
    if (!text) return;
    const original = button.textContent;
    try {
      let copied = false;
      if (navigator.clipboard && window.isSecureContext) {
        try {
          await navigator.clipboard.writeText(text);
          copied = true;
        } catch (_) {
          /* Try selection below. */
        }
      }
      if (!copied) {
        const area = make("textarea", undefined, "copy-buffer");
        area.value = text;
        area.readOnly = true;
        const previous = document.activeElement;
        (document.querySelector("dialog[open]") || document.body).append(area);
        area.select();
        copied = document.execCommand("copy");
        area.remove();
        if (previous) previous.focus({ preventScroll: true });
      }
      button.textContent = copied ? "已复制 ✓" : "请选中文本复制";
    } catch (_) {
      button.textContent = "请选中文本复制";
    }
    setTimeout(() => {
      button.textContent = original;
    }, 2200);
  }

  function code(title, text, id) {
    const block = make("div", undefined, "guide-code");
    const bar = make("div", undefined, "guide-code-bar");
    const button = make("button", "复制", "secondary");
    button.type = "button";
    button.setAttribute("aria-label", "复制" + title);
    button.addEventListener("click", () => copy(text, button));
    bar.append(make("span", title), button);
    const pre = make("pre", text);
    if (id) pre.id = id;
    block.append(bar, pre);
    return block;
  }
  function paragraph(text, cls) {
    return make("p", text, cls || "");
  }
  function list(items, ordered = false) {
    const node = make(ordered ? "ol" : "ul", undefined, "guide-list");
    items.forEach((text) => node.append(make("li", text)));
    return node;
  }
  function mapping(rows) {
    const table = make("table", undefined, "guide-mapping");
    for (const [label, value] of rows) {
      const row = make("tr");
      row.append(make("th", label), make("td", value));
      table.append(row);
    }
    const wrap = make("div", undefined, "guide-table");
    wrap.append(table);
    return wrap;
  }
  function details(title, ...children) {
    const node = make("details", undefined, "guide-details");
    node.append(make("summary", title), ...children);
    return node;
  }

  function examples(base, model, output) {
    const prompt = [
      { role: "user", content: "你好，请用一句话介绍你能帮我做什么。" },
    ];
    const body = {
      model,
      messages: prompt,
      max_tokens: Math.min(output, 256),
      thinking: { type: "disabled" },
    };
    const bash = `export LAB_LLM_BASE_URL=${shellQuote(base)}\nexport LAB_LLM_MODEL=${shellQuote(model)}\nread -rsp '粘贴本组个人 Key（输入隐藏）: ' LAB_LLM_API_KEY\nprintf '\\n'\nexport LAB_LLM_API_KEY`;
    const powershell = `$env:LAB_LLM_BASE_URL = ${shellQuote(base).replaceAll("'\\''", "''")}\n$env:LAB_LLM_MODEL = ${shellQuote(model).replaceAll("'\\''", "''")}\n$secret = Read-Host '粘贴本组个人 Key' -AsSecureString\n$env:LAB_LLM_API_KEY = [System.Net.NetworkCredential]::new('', $secret).Password\nRemove-Variable secret`;
    const python = `import os\nfrom openai import OpenAI\n\nclient = OpenAI(\n    api_key=os.environ["LAB_LLM_API_KEY"],\n    base_url=os.environ["LAB_LLM_BASE_URL"],\n    max_retries=0,\n    timeout=180,\n)\nreply = client.chat.completions.create(\n    model=os.environ["LAB_LLM_MODEL"],\n    messages=[{"role": "user", "content": "你好，请用一句话介绍你能帮我做什么。"}],\n    max_tokens=${Math.min(output, 256)},\n    extra_body={"thinking": {"type": "disabled"}},\n)\nprint(reply.choices[0].message.content)`;
    const stream = `with client.chat.completions.create(\n    model=os.environ["LAB_LLM_MODEL"],\n    messages=[{"role": "user", "content": "解释什么是 API。"}],\n    max_tokens=${Math.min(output, 512)},\n    stream=True,\n    stream_options={"include_usage": True},\n    extra_body={"thinking": {"type": "disabled"}},\n) as chunks:\n    for chunk in chunks:\n        for choice in chunk.choices:\n            if choice.delta.content:\n                print(choice.delta.content, end="", flush=True)`;
    const node = `import OpenAI from "openai";\n\nconst client = new OpenAI({\n  apiKey: process.env.LAB_LLM_API_KEY,\n  baseURL: process.env.LAB_LLM_BASE_URL,\n  maxRetries: 0,\n  timeout: 180_000,\n});\nconst reply = await client.chat.completions.create({\n  model: process.env.LAB_LLM_MODEL,\n  messages: [{ role: "user", content: "你好，请用一句话介绍你能帮我做什么。" }],\n  max_tokens: ${Math.min(output, 256)},\n  thinking: { type: "disabled" },\n});\nconsole.log(reply.choices[0].message.content);`;
    const harness = `# 合并到现有 llm-pi-ai 的 config.providers 下，保留其他提供商配置。\n- id: llm-pi-ai\n  config:\n    providers:\n      lab-gateway:\n        displayName: 实验室网关\n        api: openai-completions\n        baseURL: ${json(base)}\n        apiKeyEnv: LAB_LLM_API_KEY\n        reasoning: "off"\n        retryPolicy:\n          mode: normal\n          maxRetries: 0\n        compat:\n          supportsStore: false\n          supportsDeveloperRole: false\n          maxTokensField: max_tokens\n          supportsUsageInStreaming: true\n          thinkingFormat: deepseek\n          requiresReasoningContentOnAssistantMessages: true\n        models:\n          - id: ${json(model)}\n            input: ${["deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"].includes(model) ? "[text, image]" : "[text]"}\n            maxTokens: ${Math.min(output, 4096)}\n            reasoningEfforts:\n              "off": null\n              low: low\n              high: high`;
    const agent = `# 先执行本页 Python 示例中的 import 和 client 初始化，再运行此段。\nimport json\n\ntools = [{\n    "type": "function",\n    "function": {\n        "name": "get_project_status",\n        "description": "读取当前项目的状态",\n        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},\n    },\n}]\nmessages = [{"role": "user", "content": "请调用工具查询项目状态，然后告诉我结果。"}]\n\n# 每轮模型请求均单独计量；限制循环次数，避免 Agent 一直运行。\nfor step in range(4):\n    reply = client.chat.completions.create(\n        model=os.environ["LAB_LLM_MODEL"],\n        messages=messages, tools=tools,\n        max_tokens=${Math.min(output, 1024)},\n        extra_body={"thinking": {"type": "disabled"}},\n    )\n    message = reply.choices[0].message\n    if not message.tool_calls:\n        print(message.content)\n        break\n    messages.append({\n        "role": "assistant", "content": message.content,\n        "tool_calls": [{"id": t.id, "type": "function",\n                        "function": {"name": t.function.name, "arguments": t.function.arguments}}\n                       for t in message.tool_calls],\n    })\n    for tool in message.tool_calls:\n        if tool.function.name != "get_project_status":\n            raise ValueError("工具未注册")\n        result = {"status": "开发中", "next_step": "运行项目测试"}\n        messages.append({"role": "tool", "tool_call_id": tool.id,\n                         "content": json.dumps(result, ensure_ascii=False)})\nelse:\n    print("已达到 4 轮上限，请检查任务或工具结果。")`;
    const curlModels = `curl --fail-with-body "$LAB_LLM_BASE_URL/models" \\\n  -H "Authorization: Bearer $LAB_LLM_API_KEY"`;
    const curlChat = `curl --fail-with-body "$LAB_LLM_BASE_URL/chat/completions" \\\n  -H "Authorization: Bearer $LAB_LLM_API_KEY" \\\n  -H 'Content-Type: application/json' \\\n  -d ${shellQuote(JSON.stringify(body, null, 2))}`;
    const psModels = `Invoke-RestMethod -Uri "$env:LAB_LLM_BASE_URL/models" \`\n  -Headers @{ Authorization = "Bearer $env:LAB_LLM_API_KEY" }`;
    const psChat = `$body = @{\n  model = $env:LAB_LLM_MODEL\n  messages = @(@{ role = 'user'; content = '你好，请用一句话介绍你能帮我做什么。' })\n  max_tokens = ${Math.min(output, 256)}\n  thinking = @{ type = 'disabled' }\n} | ConvertTo-Json -Depth 8\nInvoke-RestMethod -Uri "$env:LAB_LLM_BASE_URL/chat/completions" \`\n  -Method Post -Headers @{ Authorization = "Bearer $env:LAB_LLM_API_KEY" } \`\n  -ContentType 'application/json; charset=utf-8' \`\n  -Body ([System.Text.Encoding]::UTF8.GetBytes($body))`;
    return {
      bash,
      powershell,
      python,
      stream,
      node,
      harness,
      agent,
      curlModels,
      curlChat,
      psModels,
      psChat,
    };
  }

  function render(profile) {
    const host = document.querySelector("#student-guide");
    host.replaceChildren();
    const heading = make("h2", "接入指南");
    heading.id = "guide-title";
    heading.tabIndex = -1;
    host.append(
      make("div", "CONNECT YOUR WORKFLOW", "eyebrow"),
      heading,
      paragraph(
        "选好项目组和模型，下面的地址与示例会自动更新。把本组个人 Key 填入客户端，即可离开网页继续调用。",
      ),
    );
    const steps = make("div", undefined, "guide-steps");
    for (const [number, title, text] of [
      ["01", "领取个人 Key", "在上方项目组卡片领取；已有 Key 可直接使用。"],
      ["02", "确认组与模型", "不同组的 Key 分别计量，示例必须对应同一组。"],
      ["03", "复制配置并验证", "先查询模型列表，再发一条短消息。"],
    ]) {
      const item = make("div");
      item.append(make("span", number), make("strong", title), paragraph(text));
      steps.append(item);
    }
    host.append(steps);
    const groups = profile.memberships.filter(
      (member) => member.active && member.models.length,
    );
    if (!groups.length) {
      host.append(
        paragraph(
          "当前没有可调用的项目组或模型，请联系管理员开通。",
          "key-warning",
        ),
      );
      return;
    }
    let group = groups.find((member) => member.id === groupId) || groups[0];
    groupId = group.id;
    const controls = make("div", undefined, "guide-controls");
    const groupLabel = make("label", "用于配置的项目组");
    const groupSelect = make("select");
    groupSelect.id = "guide-group";
    for (const member of groups) {
      const option = make("option", member.team);
      option.value = member.id;
      groupSelect.append(option);
    }
    groupSelect.value = group.id;
    groupLabel.append(groupSelect);
    const modelLabel = make("label", "模型名称");
    const modelSelect = make("select");
    modelSelect.id = "guide-model";
    modelLabel.append(modelSelect);
    const shellLabel = make("label", "终端环境");
    const shellSelect = make("select");
    shellSelect.id = "guide-shell";
    for (const [value, label] of [
      ["bash", "Bash · Linux / macOS"],
      ["powershell", "PowerShell · Windows"],
    ]) {
      const option = make("option", label);
      option.value = value;
      shellSelect.append(option);
    }
    shellSelect.value = selectedShell;
    shellLabel.append(shellSelect);
    controls.append(groupLabel, modelLabel, shellLabel);
    host.append(controls);
    const scope = paragraph("", "guide-scope");
    scope.id = "guide-scope";
    host.append(scope);
    const tabs = make("div", undefined, "guide-tabs");
    tabs.setAttribute("role", "tablist");
    tabs.setAttribute("aria-label", "接入方式");
    const content = make("div", undefined, "guide-content");
    const labels = [
      ["client", "聊天客户端"],
      ["python", "Python"],
      ["node", "Node.js"],
      ["agent", "Agent 项目"],
      ["harness", "DeepSeek Harness"],
      ["http", "HTTP 验证"],
    ];
    const buttons = [];
    for (const [id, label] of labels) {
      const button = make("button", label);
      button.type = "button";
      button.id = "guide-tab-" + id;
      button.setAttribute("role", "tab");
      button.setAttribute("aria-controls", "guide-pane-" + id);
      button.addEventListener("click", () => {
        selectedTab = id;
        update();
      });
      button.addEventListener("keydown", (event) => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key))
          return;
        event.preventDefault();
        let index = buttons.indexOf(button);
        index =
          event.key === "Home"
            ? 0
            : event.key === "End"
              ? labels.length - 1
              : (index +
                  (event.key === "ArrowRight" ? 1 : -1) +
                  labels.length) %
                labels.length;
        selectedTab = labels[index][0];
        update();
        buttons[index].focus();
      });
      tabs.append(button);
      buttons.push(button);
    }
    host.append(tabs, content);
    function environment(sample) {
      return [
        paragraph(
          "先在运行项目的终端设置变量，再执行示例。Key 输入不会显示，也不会嵌入示例代码。",
        ),
        code(
          "环境变量 · " + (selectedShell === "bash" ? "Bash" : "PowerShell"),
          sample[selectedShell],
          "guide-environment",
        ),
      ];
    }
    function update() {
      group =
        groups.find((member) => member.id === groupSelect.value) || groups[0];
      groupId = group.id;
      if (!group.models.includes(modelId)) modelId = group.models[0];
      modelSelect.replaceChildren();
      for (const model of group.models) {
        const option = make("option", model);
        option.value = model;
        modelSelect.append(option);
      }
      modelSelect.value = modelId;
      scope.textContent =
        "当前项目组：" +
        group.team +
        "。请使用此组个人 Key；登录密码不能作为 API Key。单次输出上限为 " +
        group.max_output +
        " Token。";
      const sample = examples(
        profile.base_url,
        modelId,
        group.max_output || 2048,
      );
      buttons.forEach((button, index) => {
        const active = labels[index][0] === selectedTab;
        button.setAttribute("aria-selected", String(active));
        button.tabIndex = active ? 0 : -1;
      });
      content.replaceChildren();
      content.id = "guide-pane-" + selectedTab;
      content.setAttribute("role", "tabpanel");
      content.setAttribute("aria-labelledby", "guide-tab-" + selectedTab);
      if (selectedTab === "client")
        content.append(
          paragraph(
            "在支持自定义 OpenAI 兼容服务的聊天客户端中，新增一个提供商，按下表填写。",
          ),
          mapping([
            ["提供商类型 / API 协议", "OpenAI 兼容 · Chat Completions"],
            ["Base URL / API 地址", profile.base_url],
            ["API Key", "粘贴上方“" + group.team + "”项目组的完整个人 Key"],
            ["Model / 模型 ID", modelId],
          ]),
          code(
            "复制接入参数（Key 需自行填写）",
            `协议: OpenAI Chat Completions\nBase URL: ${profile.base_url}\nAPI Key: <本组个人 Key>\nModel: ${modelId}`,
          ),
          list([
            "填写 Base URL 时保留 /v1，不要再追加 /chat/completions；单独填写完整请求地址的客户端才使用下方完整路径。",
            "如果模型未自动出现，手动添加上面的模型 ID；不能选该组未开放的模型。",
            "保存后先发送“你好”。如果软件只有 Responses 或 Anthropic 模式，需要更换兼容模式。",
          ]),
          paragraph(
            "完整聊天请求地址：" + profile.base_url + "/chat/completions",
            "section-note",
          ),
        );
      if (selectedTab === "python")
        content.append(
          ...environment(sample),
          code("安装 SDK", "python -m pip install openai"),
          code(
            "保存为 demo.py · python demo.py",
            sample.python,
            "guide-python",
          ),
          details(
            "需要逐字输出？使用流式调用",
            paragraph("沿用上面的 client，替换请求部分："),
            code("Python 流式请求", sample.stream),
          ),
        );
      if (selectedTab === "node")
        content.append(
          ...environment(sample),
          code("安装 SDK", "npm install openai"),
          code("保存为 demo.mjs · node demo.mjs", sample.node, "guide-node"),
        );
      if (selectedTab === "agent")
        content.append(
          paragraph(
            "已有 Agent 项目通常只需更换模型服务的三项配置；请求仍使用 Chat Completions。项目中的读文件、终端和业务工具由 Agent 自己执行。",
          ),
          mapping([
            ["base_url / baseURL", profile.base_url],
            ["api_key / apiKey", "本组个人 Key"],
            ["model", modelId],
            [
              "调用方式",
              "client.chat.completions.create(...)；不要使用 responses.create(...)",
            ],
          ]),
          list([
            "如果框架区分 OpenAI Chat Completions 和 OpenAI Responses，选择前者。",
            "从单个 Agent、模型请求并发 1 开始跑通，再按任务需要提高；多轮模型请求分别计量。",
            "工具调用使用 tools 中的 function，执行结果通过 role: tool 和对应 tool_call_id 传回。",
            "首次验证关闭自动重试。遇到 429 降低并发并等待；超时后先确认请求结果，避免重复调用。",
          ]),
          details(
            "最小 Agent 工具调用循环（Python）",
            ...environment(sample),
            code("先初始化 client", sample.python.split("reply = ")[0]),
            code("最多 4 轮的本地工具示例", sample.agent, "guide-agent"),
          ),
        );
      if (selectedTab === "harness")
        content.append(
          paragraph(
            "在已打开的 DeepSeek Harness 中按下面四步接入。完成后即可使用，无需执行终端命令或手动编辑配置文件。",
          ),
          list(
            [
              "打开 设置 → 模型 → 添加模型提供商 → 自定义模型 API。",
              "按下表填写网关地址，协议选择 OpenAI Chat Completions，API Key 填本组个人 Key。",
              "点击“获取可用模型”，选择本组模型并保存。",
              "在会话中选择 lab-gateway / " + modelId + "，发送一条消息验证。",
            ],
            true,
          ),
          mapping([
            ["Provider ID / 显示名称", "lab-gateway / 实验室网关"],
            ["API 协议", "OpenAI Chat Completions"],
            ["Base URL", profile.base_url],
            ["API Key", "本组个人 Key，不是登录密码或 DeepSeek 官网 Key"],
            ["模型 ID", modelId],
          ]),
          details(
            "高级配置（可选，界面接入无需操作）",
            paragraph(
              "只有需要手动配置、环境变量凭据或调整高级参数时才使用以下内容。界面已保存 Key 的同学无需再设置环境变量，也不要用示例中的 apiKeyEnv 覆盖界面保存的凭据引用。",
            ),
            mapping([
              ["输入类型", "仅 text"],
              [
                "建议单次输出",
                String(Math.min(group.max_output, 4096)) + " Token",
              ],
            ]),
            paragraph(
              "如选择使用环境变量提供 Key，在启动 Harness 的同一终端设置。下方示例使用页面上方所选的终端环境。",
            ),
            code(
              "环境变量 · " +
                (selectedShell === "bash" ? "Bash" : "PowerShell"),
              sample[selectedShell],
              "guide-environment",
            ),
            paragraph(
              "dsh web 用于启动 Harness Web 界面，不是填写网关配置后的额外接入步骤。使用环境变量凭据时，从设置变量的终端启动才能读取它们。常规配置路径为 $DSH_HOME/profiles/web/cordis.patch.yml；自定义 profile 请使用实际名称。",
              "section-note",
            ),
            paragraph(
              "需要手动调整时，将下方片段合并到当前 profile 的配置中，保留其他提供商及已有字段。",
            ),
            code(
              "Harness 网关配置 · 合并片段",
              sample.harness,
              "guide-harness",
            ),
            paragraph(
              "网关已兼容默认的 store: false、developer 指令及较大的客户端输出额度，仍按项目组输出上限执行；无需为了基础接入添加这些兼容项。thinkingFormat: deepseek 用于显式控制思考。支持 none / low / high / max，minimal / medium / xhigh 按网关兼容规则映射。",
              "section-note",
            ),
            paragraph(
              "文本、Flash 图片及函数工具可走此协议。此网关未开放 DeepSeek 官网额外的搜索、Files 上传和 Responses API。",
              "section-note",
            ),
          ),
        );
      if (selectedTab === "http")
        content.append(
          ...environment(sample),
          paragraph(
            "先查模型列表：此请求不生成模型回答，可用来检查地址、网络和 Key。",
          ),
          code(
            "查询可用模型",
            selectedShell === "bash" ? sample.curlModels : sample.psModels,
            "guide-model-check",
          ),
          paragraph(
            "再发第一条消息：下面会实际调用模型并计入本组用量，最多生成 " +
              Math.min(group.max_output, 256) +
              " Token。",
          ),
          code(
            "发送短消息",
            selectedShell === "bash" ? sample.curlChat : sample.psChat,
            "guide-http",
          ),
        );
    }
    groupSelect.addEventListener("change", () => {
      modelId = "";
      update();
    });
    modelSelect.addEventListener("change", () => {
      modelId = modelSelect.value;
      update();
    });
    shellSelect.addEventListener("change", () => {
      selectedShell = shellSelect.value;
      update();
    });
    update();
    host.append(
      details(
        "兼容范围与常见问题",
        paragraph(
          "当前网关提供 GET /v1/models 和 POST /v1/chat/completions，支持文本、Flash 图片输入、SSE 流式输出及客户端 function 工具调用。OpenAI 兼容描述的是此接口格式，不代表使用 OpenAI 的模型或额度。",
        ),
        paragraph(
          "图片通过 image_url 内容块传入，可用 HTTP(S) 链接或 Base64 data URL，请求体最多 48 MiB；32768 等项目组上限仅限制输出，不是上下文长度。当前不提供 /v1/responses、Anthropic Messages、Files 文件上传、Embeddings 和 DeepSeek 官方内置联网接口。模型名称以当前组和 Key 可访问的 /v1/models 为准。",
        ),
        mapping([
          [
            "连接失败 / 超时",
            "先确认运行客户端或 Harness 的电脑/服务器能访问此网关；校外通常需要校园 VPN。",
          ],
          ["401", "个人 Key 无效、到期或已轮换；重新领 Key 并更新客户端。"],
          ["403", "模型未开放，或成员/项目组已停用；检查选中的组和模型。"],
          ["400 / model_does_not_support_images", "请使用支持图片的 deepseek-flash；deepseek-v4-pro 不支持图片。"],
          ["402", "个人或组已结算费用已达到预算；联系管理员或等待下一个预算周期。"],
          ["429", "频率或并发已达上限；降低 Agent 并发并稍后重试。"],
          [
            "404 / 405",
            "检查 /v1 是否重复，以及是否误选 Responses / Anthropic 协议。",
          ],
          [
            "413 / 422",
            "413 表示请求体超过 48 MiB（包含 Base64 编码）；422 请查看 error.details 中的字段路径。支持 image_url 图片块；文件上传、音视频及 store: true 暂不支持。",
          ],
          [
            "502 / 503",
            "保留响应中的 X-Request-ID 联系管理员，避免无条件重复发送。",
          ],
        ]),
      ),
    );
    const references = make("div", undefined, "guide-sources");
    references.append(
      paragraph(
        "官方参考 · 核对日期 2026-11-14。已使用 Harness 的实际 pi-ai 调用依赖完成模拟流式验证；完整 Harness 应用流程尚未在此平台验收。",
        "section-note",
      ),
    );
    for (const [title, url] of sources) {
      const link = make("a", title);
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      references.append(link);
    }
    host.append(references);
  }
  window.StudentGuide = Object.freeze({ render, copy });
})();
