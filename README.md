# ohMyRhine

ohMyRhine 让大模型扮演作者编写的角色，在聊天中与人对话。角色的身份、经历、说话方式、知识范围和不能透露的内容写在几个纯文本文件里；记忆、群聊中的接话判断和知识边界由 ohMyRhine 处理。角色可以在终端或浏览器中使用，也可以通过连接器接入聊天软件。

## 特点

- **角色包是纯文本文件。** 人设、经历、人际关系、世界观和说话示例都是 YAML 和 Markdown，可以用 Git 管理。秘密的内容不进入提示词，模型无从泄露；世界观可以区分「真相」和「角色所知的版本」。
- **接近真人的聊天节奏。** 回复长度随场合变化，可以分条发送，也可以不回复。群聊中先由小模型判断角色是否会开口，没有人找角色时通常保持沉默。
- **长期记忆。** 角色自行记录笔记，例如答应过的事、对方希望的称呼；旧消息写入滚动摘要。笔记的每一次改动都有记录，不会被静默删除。
- **常见的聊天操作。** 终端和网页都支持引用、撤回、拍一拍、图片和表情包。
- **查资料。** 角色可以把「翻阅以前的聊天记录」和「上网查资料」交给本机的命令行 agent（Claude Code、Codex、Antigravity）。
- **多家模型。** 支持 Anthropic、OpenAI、DeepSeek、Gemini 和 OpenAI 兼容服务，每个任务、每个角色可以单独选择模型。

## 快速开始

依赖 [uv](https://docs.astral.sh/uv/)，由它安装 Python 3.14 和项目依赖。

```sh
git clone <仓库地址> ohMyRhine
cd ohMyRhine
uv sync
cp config.example.yaml config.yaml
export ANTHROPIC_API_KEY=sk-...    # 其他供应商见 docs/configuration.md
uv run rhine doctor                # 检查配置、密钥和角色包
```

与自带角色对话：

```sh
uv run rhine chat dafeiyu          # 终端；输入 / 查看命令
uv run rhine web                   # 浏览器
```

没有密钥时，可以把 `config.yaml` 中的整个 `tasks` 替换为以下内容来验证流程，角色会原样复述收到的消息：

```yaml
tasks:
  roleplay: {provider: echo}
```

如果你什么都不会，在该目录打开 agent 并让它帮你完成配置。Claude Code、Codex 等编程 agent 均可，配置步骤见[agent 上手指南](docs/agent-quickstart.md)。

## 自带的角色

| ID | 角色 | 用途 |
|---|---|---|
| `dafeiyu` | 大肥鱼，化成女孩的小蓝鲸，在海边小镇的海洋馆打工 | 完整示例，用到了角色包的全部部分 |
| `sample` | 小雾，住在网络里的数字生命 | `rhine new` 生成的模板，每个文件都带注释 |
| `raw` | 不扮演任何角色的模型本身 | 对照组，显示不加扮演规则时模型的表现 |

## 编写角色

```sh
uv run rhine new mychar --name 名字    # 按推荐写法生成角色包，依注释修改
uv run rhine chat mychar               # 试聊；修改文件后在聊天中输入 /reload
uv run rhine eval mychar               # 运行评测场景，检查角色是否保持一致
```

写法和注意事项见[写角色包](docs/characters.md)。

## 接入聊天软件

本项目不涉及任何聊天软件的桥接，但支持接口，可以接入这类软件。`rhine-gateway` 是一个 WebSocket 服务：聊天软件一侧的连接器（需自行编写或另行获取）按[连接器协议](docs/protocol.md)把消息交给网关，再把角色的回复发回原会话。支持的消息类型和操作见[接入聊天软件](docs/gateway.md)。

## 文档

- [agent 上手指南](docs/agent-quickstart.md)：供编程 agent 完成安装、配置和首次运行
- [写角色包](docs/characters.md)：各文件的内容、推荐写法、评测场景
- [配置](docs/configuration.md)：模型、供应商、价格、按角色配置、查资料的 agent
- [聊天：终端和网页](docs/chat.md)：命令、快捷键、多角色对话、笔记和系统指令
- [接入聊天软件](docs/gateway.md)：支持的接口、连接器的职责、运行网关；字段细节见[连接器协议](docs/protocol.md)
- [内部设计](docs/design.md)：面向修改代码的开发者

## 许可证

代码以 [Apache License 2.0](LICENSE) 发布。
