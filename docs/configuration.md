# 配置

ohMyRhine 读取当前目录的 `config.yaml`，也可以用 `-c <路径>` 指定。仓库中的 [`config.example.yaml`](../config.example.yaml) 是推荐的起点：复制为 `config.yaml` 并填入密钥即可使用。配置中的相对路径以配置文件所在目录为基准。

修改配置后，可用以下两个命令检查：

```sh
uv run rhine doctor          # 配置、密钥、角色包和查资料工具是否就绪；--ping 实际发送一句话验证
uv run rhine config          # 实际生效的配置：各任务使用的模型及价格
```

## 最小配置

只需指定各任务使用的供应商和模型：

```yaml
tasks:
  roleplay:    {provider: anthropic, model: claude-opus-5-5}
  reply_judge: {provider: anthropic, model: claude-haiku-5-5}
  vision:      {provider: anthropic, model: claude-haiku-5-5}
  summary:     {provider: anthropic, model: claude-opus-5-5}
```

密钥默认从环境变量读取，例如 `ANTHROPIC_API_KEY`。其余字段均有默认值，按需填写，见下文。

没有密钥时，可以用 `echo` 验证流程，角色原样复述收到的消息：

```yaml
tasks:
  roleplay: {provider: echo}
```

## 任务（`tasks`）

一次回复涉及几个分工不同的模型调用，每种调用称为一个任务：

| 任务 | 作用 | 对模型的要求 | 未配置时 |
|---|---|---|---|
| `roleplay` | 生成角色的回复 | 支持工具调用；`media.live_images` 大于 0 时还需支持图片输入 | 必须配置 |
| `reply_judge` | 群聊中预先判断角色是否会开口，再交给 `roleplay` | 低价、快速 | 群聊中只在被 @、被引用或提到名字时回复 |
| `vision` | 为图片和表情包生成一句描述 | 支持图片输入 | 角色无法得知图片内容 |
| `summary` | 旧消息移出上下文时生成滚动摘要，也为超长消息生成摘要 | — | 移出上下文的消息不再保留 |

推荐 `roleplay` 和 `summary` 使用能力最强的模型，`reply_judge` 和 `vision` 使用小模型。每个任务可设置以下字段：

| 字段 | 说明 |
|---|---|
| `provider`、`model` | 必填（`echo` 无需 `model`）。模型名见 `rhine models` |
| `max_tokens` | 输出上限，含思考 |
| `effort` | 推理档位：`low`、`medium`、`high`、`xhigh`、`max`，须为该模型支持的档位 |
| `thinking` | `false` 关闭思考。对无法关闭思考的模型设置此项会报错 |
| `fallbacks` | `default`：被安全分类器拒绝时，由服务端换模型重试（只有 Anthropic 的部分模型支持） |
| `temperature`、`top_p` | 采样参数，未设置时不发送 |
| `stream_timeout_s` | 流式回复连续多少秒未收到数据即视为失败，默认 60 |
| `max_retries` | 请求失败且尚未收到响应时的重试次数 |
| `extra_body`、`extra_headers` | 原样合并进请求，用于传递特定服务独有的参数 |

各任务的默认值：`roleplay` 和 `summary` 为 effort `medium`、输出上限 8000；`reply_judge` 和 `vision` 为 effort `low`，模型支持时关闭思考，输出上限分别为 150 和 500。

配置在启动时校验，无法满足的配置直接报错，而不是等到发送请求时才失败。例如为需要图片输入的任务配置了不支持图片的模型、对无法关闭思考的模型设置 `thinking: false`、设置了模型不支持的 effort。

## 供应商（`providers`）

内置五个供应商，无需在 `providers` 中声明：

| 名字 | 协议 | 密钥的环境变量 |
|---|---|---|
| `anthropic` | Anthropic Messages | `ANTHROPIC_API_KEY` |
| `openai` | OpenAI Chat Completions | `OPENAI_API_KEY` |
| `deepseek` | OpenAI 兼容 | `DEEPSEEK_API_KEY` |
| `gemini` | OpenAI 兼容 | `GEMINI_API_KEY` |
| `echo` | 原样回显，测试用 | — |

只需写出与默认值不同的部分，例如从文件读取密钥，或接入其他兼容服务：

```yaml
providers:
  anthropic:
    api_key_file: ~/.config/ohmyrhine/anthropic.key
  qwen:
    type: openai_api
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key_env: DASHSCOPE_API_KEY
```

| 字段 | 说明 |
|---|---|
| `type` | `anthropic_api`、`openai_api` 或 `echo`；内置供应商无需填写 |
| `base_url` | 服务地址 |
| `api_key_env`、`api_key_file` | 密钥来源：先查环境变量，再查文件。内置供应商设置了文件后，不再读取其默认环境变量 |
| `max_tokens_field` | 输出上限的字段名，`max_tokens` 或 `max_completion_tokens`。服务端不接受时自动改用另一个 |

常见的 OpenAI 兼容服务（地址以各服务的文档为准）：

| 服务 | `base_url` |
|---|---|
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| Kimi | `https://api.moonshot.cn/v1` |
| 智谱 | `https://open.bigmodel.cn/api/paas/v4/` |
| OpenRouter | `https://openrouter.ai/api/v1` |
| Ollama（本机） | `http://localhost:11434/v1` |

DeepSeek 也提供 Anthropic 兼容接口：`base_url: https://api.deepseek.com/anthropic`，配合 `type: anthropic_api` 使用。

两种协议的差别：Anthropic 协议功能最完整，支持一小时的人设缓存、思考摘要、服务端换模型重试和精确的 token 计数。OpenAI 兼容协议同样支持流式回复、工具调用、图片输入和结构化输出；服务端不支持某个参数（结构化输出格式、输出上限字段名、`reasoning_effort`）时，自动改用其他写法重试，并在日志中记录一条警告。

## 模型（`models`）

`rhine models` 列出内置模型及其能力：是否支持图片输入、是否支持工具调用、输出上限和当前价格。以下限制在加载配置时检查：

- `deepseek-v4-pro` 不支持图片输入。
- `gpt-6.1-sol`、`gpt-6.1-terra` 在 Chat Completions 中不支持工具调用，不能用于 `roleplay`。
- `gpt-6-sol`、`gpt-6-luna` 只有关闭思考时才支持工具调用，用于 `roleplay` 时需设置 `thinking: false`。
- Opus、Fable 和 Gemini 无法关闭思考。

内置表中没有的模型，需在 `models` 中声明其能力；内置模型的个别字段也可以在此覆盖：

```yaml
models:
  qwen-plus:
    vision: false
    tools: yes                 # yes / no / no_reasoning（关闭思考时才支持）
    structured: json_object    # json_schema / json_object / prompt
    max_output: 8192
    efforts: [low, medium, high]
    thinking_off: {extra_body: {enable_thinking: false}}
    price: {input: 0.8, output: 2.0, cache_read: 0.16}
```

不在内置表中且未声明 `vision` 的模型，无法确定是否支持图片输入，因此不能用于需要图片输入的任务。

**价格**：用量日志和 `/stats` 中的费用按内置价格表估算，单位为每百万 token 的美元价格。已公布的调价按日期生效，DeepSeek 按峰谷时段计价，超长提示按长提示价格计算。价格变动或模型不在表中时，在 `models.<模型>.price` 中设置。没有价格的模型，费用记为空。实际费用以各供应商控制台的账单为准。

## 其他设置

```yaml
characters_dir: characters   # 角色包目录
data_dir: data               # 数据库、图片和日志的目录
history_tokens: 24000        # 交给模型的聊天记录约多少 token，更早的部分由滚动摘要概括
log_thinking: false          # 将每次回复的思考摘要和原文记录到 data/logs/

group:
  judge: true                # 配置了 reply_judge 时，群聊消息先经过筛选
  judge_context: 12          # 筛选时参考的最近消息条数
  wait_short_s: 6            # 对方分多条发送时，等待多少秒再判断
  wait_long_s: 15            # 对方话未说完时，等待多少秒
  dice: false                # 角色账号为群成员代掷骰子时启用

media:
  max_edge: 1024             # 图片缩放后的最长边
  live_images: 4             # 以原图发给 roleplay 的最近图片数；0 表示只提供描述

memory:
  summary_chars: 1500        # 滚动摘要的长度上限
  notes_chars: 4000          # 笔记总长超过此值时，提醒角色整理
```

## 每个角色单独配置（`characters`）

`characters.<角色 ID>` 中写出该角色与全局配置不同的部分，该角色的持久化实例同样适用：

```yaml
characters:
  dafeiyu:
    tasks:
      roleplay: {provider: deepseek, model: deepseek-flash}   # 整项替换全局的 roleplay
      reply_judge: null                                        # 该角色不使用筛选
    group: {dice: true}          # 只覆盖这一项，其余沿用全局配置
```

- 可覆盖的段：`tasks`、`lookup`、`group`、`media`、`memory`、`history_tokens`、`log_thinking`。
- 段内字段逐个覆盖全局配置；单个任务、`lookup` 中的单个工具整项替换。设为 `null` 可去掉全局配置中的对应项。
- `providers`、`models`、`characters_dir`、`data_dir` 只能在全局配置中设置，密钥只需配置一次。
- `rhine config -C <角色 ID>` 显示该角色实际生效的配置。

## 查资料的 agent（`lookup`）

角色可以获得两个工具：翻阅本会话以前的记录（`history`），上网或查阅本地资料（`search`）。角色先告知对方（例如「我翻翻」），任务交给本机已登录的命令行 agent 执行，查询完成后角色继续回答。agent 使用其账号的订阅额度，不需要 API key。

```yaml
lookup:
  history: {harness: claude, model: sonnet, effort: medium}
  search:  {harness: codex, effort: medium}
  timeout_s: 900          # 超时后终止卡住的进程
  concurrency: 2          # 两个工具合计的最大并发数
  reference:              # 可选：优先查阅的本地资料
    description: 活动的日程和场地说明
    path: data/reference
    format: text          # text：直接读取目录；git_html：先更新已有的 Git 仓库，再将 HTML 转为文本
```

| 字段 | 说明 |
|---|---|
| `harness` | 必填：`claude`（Claude Code）、`codex`（Codex）或 `agy`（Antigravity） |
| `model` | 该程序中的模型名；未设置时 Claude Code 使用 `sonnet`，其余两个使用各自的默认模型 |
| `effort` | `low` 到 `max` |
| `command` | 程序不在 PATH 中或名称不同时，填写其路径 |

只有写入配置且对应程序已安装时，角色才拥有相应的工具。每次查询时 agent 只获得必要的权限：翻阅记录时只能写入自己的索引文件、不能上网；查资料时可以上网和读取资料目录、不能写文件。三个程序对权限的执行程度不同：

| | 读 | 写 | 上网 |
|---|---|---|---|
| Claude Code | 仅工作目录和资料目录 | 仅允许写入的文件 | 仅查资料时 |
| Antigravity | 仅工作目录和资料目录 | 仅允许写入的文件 | 仅查资料时 |
| Codex | 整个磁盘 | 翻阅记录时为整个工作目录 | 仅查资料时，且只能使用其内置的网页搜索 |

Codex 能够读取其他会话的记录，有此顾虑时不应用它翻阅记录。Antigravity 遇到未授权的操作时直接退出，本次查询视为失败。

## 插件（`plugins`）

`plugins` 列出启动时导入的 Python 模块，用于接入项目之外的代码，例如把聊天软件导出的旧记录提供给翻阅记录的 agent。接口见 `src/ohmyrhine/plugins.py`。

```yaml
plugins: [my_site.history]
```
