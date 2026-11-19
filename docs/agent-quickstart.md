# agent 上手指南

本文档供编程 agent 使用：用户在本目录中启动 agent，请它完成 ohMyRhine 的安装、配置和首次运行。用户可能不熟悉命令行，因此每完成一步，agent 应简要说明做了什么、结果如何。

目标是让用户能与自带角色「大肥鱼」（`dafeiyu`）对话；用户需要时，再协助编写自己的角色。

## 约束

- **密钥**：由用户提供，不在终端输出、不复述、不写入仓库内的文件。用户提供密钥后，写入 `~/.config/ohmyrhine/<供应商>.key`（权限 600），在 `config.yaml` 中用 `api_key_file` 引用，只告知用户「已保存」。
- **费用**：使用真实模型聊天、`rhine eval`、`rhine doctor --ping` 都会按用量计费，执行前需告知用户并取得同意。
- **系统软件**：安装 uv 等系统软件前需取得用户同意。
- **自带角色包**：不修改 `characters/` 下已有的目录。用户的角色用 `rhine new` 新建。
- **Git**：`config.yaml`、`data/` 和密钥文件已被 `.gitignore` 忽略，不修改这些规则。

## 1. 检查环境

```sh
uv --version
```

未安装 uv 时，取得用户同意后按 <https://docs.astral.sh/uv/> 的说明安装。Python 3.14 由 uv 自动安装。

## 2. 安装依赖

```sh
uv sync
```

## 3. 编写配置

```sh
cp -n config.example.yaml config.yaml   # 已有 config.yaml 时不覆盖
```

询问用户持有哪家供应商的 API 密钥，再按情况修改 `config.yaml`：

- **Anthropic**：示例配置的默认供应商。密钥放在环境变量 `ANTHROPIC_API_KEY` 中，或按上文写入密钥文件，并在 `config.yaml` 中添加：

  ```yaml
  providers:
    anthropic:
      api_key_file: ~/.config/ohmyrhine/anthropic.key
  ```

- **OpenAI、DeepSeek、Gemini**：环境变量分别为 `OPENAI_API_KEY`、`DEEPSEEK_API_KEY`、`GEMINI_API_KEY`，也可以使用 `api_key_file`。`uv run rhine models` 列出内置模型；将 `tasks` 中每个任务的 `provider` 和 `model` 改为该供应商的模型，`vision` 需选择支持图片输入的模型。
- **其他 OpenAI 兼容服务**：在 `providers` 中添加一项 `type: openai_api`，填写 `base_url` 和密钥，见[配置](configuration.md)。
- **暂无密钥**：将整个 `tasks` 替换为以下内容以验证流程。角色原样复述收到的消息，不产生费用。

  ```yaml
  tasks:
    roleplay: {provider: echo}
  ```

各字段的含义见[配置](configuration.md)。

## 4. 检查配置

```sh
uv run rhine doctor
```

反复修改，直到输出中没有 ✗；每个 ✗ 后都附有原因和修改方法。需要验证密钥是否可用时，在用户同意后加 `--ping`，它向每个任务的模型发送一句话，费用很低。

## 5. 开始对话

`rhine chat` 需要交互式终端，agent 通常无法驱动，因此由用户自行运行以下命令：

```sh
uv run rhine chat dafeiyu    # 终端；输入 / 查看命令
uv run rhine web             # 浏览器
```

agent 需要先确认角色能够回复时，可以非交互地运行一个评测场景（使用真实模型时产生少量费用，需先征得同意）：

```sh
uv run rhine eval dafeiyu daily
```

## 6. 编写用户的角色（按需）

1. 向用户了解：角色的名字、身份、所在的地方、出现在聊天中的原因；说话方式；不愿提及的事。
2. 确定一个由小写字母、数字和下划线组成的 ID，生成角色包：

   ```sh
   uv run rhine new <ID> --name <名字>
   ```

3. 依照[写角色包](characters.md)和生成文件中的注释，把用户的回答写入角色包。不确定的内容向用户确认，不自行编造重要设定。
4. 运行 `uv run rhine eval <ID>`（产生费用，需先征得同意），把对话展示给用户，按用户的意见修改。修改后，用户在聊天中输入 `/reload` 即可生效。

## 7. 可选功能

- **翻阅聊天记录、上网查资料**：需要本机已登录的命令行 agent，见[配置](configuration.md#查资料的-agentlookup)。
- **接入聊天软件**：本项目不包含任何聊天软件的连接器，只提供接口，见[接入聊天软件](gateway.md)。连接器需由用户自行编写或另行获取。

## 完成后告知用户

- 开始对话的方法：第 5 步的两条命令。
- 配置文件为 `config.yaml`；密钥的位置（环境变量或密钥文件路径）。
- 查看费用：聊天中输入 `/cost`，或查看 `data/logs/usage.jsonl`。
- 修改角色：编辑 `characters/<ID>/` 下的文件，然后在聊天中输入 `/reload`。
