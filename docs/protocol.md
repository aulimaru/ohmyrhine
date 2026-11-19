# 连接器协议 v1

外部连接器将聊天软件的事件交给 ohMyRhine 的网关（`rhine-gateway`），再把角色的回复送回原会话。任何聊天平台，只要连接器实现了本协议即可接入。支持范围概览和网关的运行方式见[接入聊天软件](gateway.md)。

## 连接

- 连接器作为 WebSocket 客户端连接网关。默认端口为 8791，路径为 `/v1/ws`。服务端只接受来自回环地址和 `100.64.0.0/10` 地址段的连接。
- 握手请求带 `Authorization: Bearer <token>`。双方使用同一令牌，在部署环境中提供。网关通过 `--token-file` 或 `RHINE_GATEWAY_TOKEN_FILE` 读取文件，要求只有拥有者有权访问。
- 握手被拒时是 HTTP 错误：地址不允许 403，路径不对 404，token 不对 401。
- 每个 WebSocket 文本帧是一个 JSON 对象，都有 `type` 字段。
- 连接建立后，连接器先发 `hello`，服务端回 `welcome`。服务端关闭连接时的 close code：
  - `4001`：协议版本不一致；
  - `4002`：10 秒内没收到 `hello`，或第一帧不是 `hello`；
  - `4003`：同一时间只保留一个连接，新的连接器连上时旧连接被关闭。

## 通用约定

- 所有 id 都是连接器生成的稳定、不透明的值（例如平台 id 的 HMAC），带平台前缀。示例中写作用户 `im:u:…`、群 `im:g:…`、消息 `im:m:…`、事件 `im:e:…`、媒体 `im:media:…`。服务端不需要、也不应获得平台的原始账号。
- 时间 `ts` 都是 Unix 毫秒。
- 同一个群内的事件按顺序发送。服务端处理完一个事件（写入数据库）后才回复 `ack`；处理出错的事件同样回复 `ack` 并在服务端记录日志，以免同一个错误事件被反复补发。只有存储失败（数据库或磁盘错误）时不回复 `ack`，等待连接器重发。连接器按群维护游标，收到 ack 后才推进；断线重连后从游标处补发，超过 60 秒未收到 ack 的事件每 60 秒重发一次，`event_id` 不变。服务端按 `event_id` 去重。
- 补发的消息在轮到回应时如已超过 120 秒，服务端只保存、不回复，以免回复与对话脱节。
- 连接器只上报已绑定角色的会话。`chat.kind` 支持 `group` 和 `private`，接入哪些会话由连接器决定。

## 绑定角色

接入哪些会话、各由哪个角色回应，由连接器决定，例如只接受主人在群里发出的绑定指令。

- 角色为服务端角色包的 ID（用 `rhine list` 查看）。一个会话同一时间只绑定一个角色，一个网关可以承载多个角色。
- 绑定时发送 `chat_update`（`bind` 为该角色），随后可以补发最近的历史消息（`backfill: true`），使角色了解之前的对话；解绑时发送 `bind: null`，之后该会话不再转发。

## 上行：连接器 → 服务端

### hello

```json
{"type": "hello", "protocol_version": 1, "connector": "example",
 "self": {"id": "im:u:…", "nickname": "角色的账号"},
 "features": ["action_dedupe"],
 "limits": {"max_text_chars": 2000, "recall_window_s": 120}}
```

`self` 是角色发送消息所用的账号。

可选的 `features` 列出连接器支持的能力：

- `action_dedupe`：同一个 `action_id` 的 `send_message` 只执行一次（见 send_message）。只有连接器声明了该能力时，服务端才会在重启后用原 `action_id` 补问回复的结果。

可选的 `limits` 声明平台的限制，未声明的项使用默认值：

- `max_text_chars`：单条消息的字数上限，默认 2000。更长的回复由服务端在句末切分，分多条发送。
- `recall_window_s`：平台允许撤回消息的时限（秒），默认 120。服务端生成滚动摘要时只读取已超过该时限的消息，以免记录随后被撤回的内容。平台不支持撤回时设为 0。

### message

已接入会话中的每一条消息都应上报，以便角色理解上下文。

```json
{"type": "message", "event_id": "im:e:…", "ts": 1762617600123,
 "chat": {"id": "im:g:…", "kind": "group", "name": "群名", "bind": "sample"},
 "sender": {"id": "im:u:…", "nickname": "群里的昵称", "is_owner": false},
 "message_id": "im:m:…",
 "mention_self": false,
 "quote": {"message_id": "im:m:…", "sender_id": "im:u:…", "nickname": "…", "text": "被引用消息的前 200 字"},
 "dice": false,
 "from_self": false,
 "source": null,
 "backfill": false,
 "segments": [{"type": "text", "text": "周末见"}]}
```

- `chat.bind`：该会话绑定的角色 id。
- `mention_self` 表示该消息 @ 了连接器账号。优先使用平台提供的提及列表，缺少列表时可根据正文识别。
- `quote`：无引用时为 null 或省略。
- `dice` 是可选标记。连接器可标记代掷指令及其结果，结果另带 `from_self: true` 和 `source: "dice"`。默认只保留为上下文；启用 `group.dice` 后，结果归为角色替人掷出的点数，会经过筛选。点数本身不会触发回复。
- `from_self`：由角色账号发出的消息。`source` 为 `dice`（骰子发出）或 `other`（主人手动用该账号发出）。服务端自己发送的消息不作为 message 回传，只通过 `message_sent` 回报。
- `backfill`：绑定角色时补发的历史消息。服务端将其作为历史记录保存，不回应，也不执行其中的指令码。补发消息与实时消息交错到达时，实时消息照常处理。

segments 的类型如下，按在消息中出现的顺序排列：

| type | 字段 | 说明 |
|---|---|---|
| `text` | `text` | 文字 |
| `mention` | `user_id`, `name` | @ 某人 |
| `emoji` | `code` | 平台内置的小表情，用平台的文字代码表示，比如 `[笑脸]` |
| `image` | `media_id`, `available`, `mime`, `data_b64` | 图片。客户端未下载时 `available: false` |
| `sticker` | `media_id`, `md5`, `desc`, `available`, `mime`, `data_b64` | 表情包。`desc` 为平台提供的描述，没有时省略 |
| `voice` | `duration_ms` | 语音，仅时长 |
| `link` | `title`, `desc`, `url` | 链接或卡片 |
| `forward` | `title` | 合并转发，仅标题 |
| `file` | `name` | 文件，仅文件名 |
| `video` | | 视频，无其他信息 |
| `other` | `text` | 位置、名片、通话等其他消息，仅一行说明 |

图片为缩略图（未取得原图）时另带 `thumbnail: true`，服务端会告知角色这只是缩略图；动图只提供第一帧 PNG，另带 `frames`（帧数）。
链接无法取得描述或地址时可以只传 `title`。

图片和表情包的文件有两种提供方式，服务端均支持：

- 内联：segment 中直接携带 `mime` 和 `data_b64`，单个不超过 5 MB。
- 按需：segment 中只携带 `media_id`，由服务端用 `get_media` 请求。

超过 5 MB 或客户端未下载的文件，标记 `available: false`，不携带数据。

平台提供的字段不完整时按以下约定降级。

- `sender.nickname` 优先使用群昵称，其次使用平台昵称。均无法取得时留空，服务端显示为「群友」。原始账号和备注名应保留在连接器一侧。
- 取不到提及列表时可以省略 `mention` 片段，仍用 `mention_self` 表达是否在叫这个账号。

### system

撤回、拍一拍、进群/退群等系统消息，仅包含原文。

```json
{"type": "system", "event_id": "im:e:…", "ts": 1762617600123,
 "chat": {"id": "im:g:…", "kind": "group", "name": "群名", "bind": "sample"},
 "text": "「成员甲」拍了拍「小雾」"}
```

撤回事件可以带 `recall` 字段，缺少时按普通系统消息处理。

```json
{"type": "system", "event_id": "im:e:…", "ts": 1762617600123,
 "chat": {"id": "im:g:…", "kind": "group", "name": "群名", "bind": "sample"},
 "text": "「成员甲」撤回了一条消息",
 "recall": {"message_id": "im:m:…", "sender_id": "im:u:…", "nickname": "成员甲"}}
```

- `recall.message_id`：被撤回的消息，与原 message 事件（角色自己的消息则为 `message_sent`）的 `message_id` 相同。以下情况为 null：
  - 连接器读到原消息之前消息已被撤回（连接器离线时，或两次轮询之间），此时 `sender_id` 也为 null，`nickname` 取自提示文字中的名字；
  - 原消息本身不转发，例如由连接器自行处理的指令。
- `recall.sender_id`、`nickname`：撤回者。角色账号自己撤回的消息（角色的发言、骰子的结果）同样上报，`sender_id` 即该账号。
- 平台在原记录上标记撤回时，连接器以新的 `event_id` 发送 system 事件，`ts` 为发现撤回的时间。
- 能确定原消息时应填 `recall.message_id`，无法识别撤回对象的系统提示可以只传文字。
- 服务端的处理：
  - 他人撤回的消息原样保留在记录中（角色可能已经看到），其后追加一条撤回提示，引用被撤回的消息。角色可以提及撤回，但不复述原文。
  - 滚动摘要只读取已超过撤回时限（`limits.recall_window_s`）的消息，已撤回的原文及对它的引用均略去。
  - 角色账号撤回的消息从记录中删除，视为从未发送。

### chat_update

绑定或解绑角色时发送；`bind` 为 null 表示解绑，之后该会话不再转发。

```json
{"type": "chat_update", "event_id": "im:e:…",
 "chat": {"id": "im:g:…", "kind": "group", "name": "群名", "bind": "sample"}}
```

### media

回应服务端的 `get_media`。

```json
{"type": "media", "media_id": "im:media:…", "mime": "image/png", "data_b64": "…"}
{"type": "media", "media_id": "im:media:…", "error": "unavailable"}
```

### media_update

以缩略图（`thumbnail: true`）上报的图片，在连接器取得原图后补发。服务端按 `chat` 和 `message_id` 找到原消息，把第 `index` 个片段的缩略图替换为原图；未携带 `data_b64` 时用 `media_id` 发送 `get_media`。找不到原消息时只回复 ack。

```json
{"type": "media_update", "event_id": "im:e:…", "chat": {"id": "im:g:…", "kind": "group", "bind": "sample"},
 "message_id": "im:m:…", "index": 0, "media_id": "im:media:…", "mime": "image/png", "data_b64": "…"}
```

补发原图时应能唯一确定对应消息及片段。

### message_sent / send_failed

回应服务端的 `send_message`。

```json
{"type": "message_sent", "action_id": "act_…", "message_id": "im:m:…", "ts": 1762617600123}
{"type": "send_failed", "action_id": "act_…", "reason": "expired"}
```

`reason`：

- `expired`：到 `expires_at` 时仍未发出。未提供 `expires_at` 时默认 120 秒后过期。
- `chat_not_enabled`：该会话未绑定角色。
- `quote_not_found`：找不到要引用的消息。
- `bad_text`：文字为空或超过连接器的上限（`limits.max_text_chars`）。
- `ui_error`：界面操作失败。另带 `outcome: "unknown"` 时表示消息可能已经发出，连接器不会重试。
- `interrupted`：连接器在输入过程中重启，重启后未在会话中找到这条回复。总是携带 `outcome: "unknown"`。
- `rate_limited`：超过连接器防止失控的发送上限（见限流），消息确定未发出。

`message_sent` 另带 `quoted`：引用失败、改为不带引用发送时为 false。
- 平台已完成发送但暂时取不到消息 ID 时，可以回报 `message_id: null` 的 `message_sent`。连接器随后认领该消息，避免将其作为新的 message 事件回传。
- 界面输入之后发生的任何意外错误，`send_failed` 都携带 `outcome: "unknown"`，服务端保留这条回复。

收到 `send_failed` 时，服务端从记录中删除这条回复，视为角色未曾发言；携带 `outcome: "unknown"` 的除外，因为消息可能已经发出。

服务端将待确认的 `action_id` 随回复存入数据库。服务端重启后，连接器的回报仍能关联到原回复；重复回报只处理一次。服务端已开始发送但未收到回报时，保留这条回复。

服务端收到回报后回复 `{"type": "ack", "action_id": "act_…"}`。连接器将回报保留在待发队列中，收到该 ack 后才删除；断线重连时以及超过 60 秒未收到 ack 时重发。

## 下行：服务端 → 连接器

### welcome

```json
{"type": "welcome", "protocol_version": 1}
```

### ack

对每个带 `event_id` 的上行事件（message / system / chat_update / media_update）回复一次；对每个回报（message_sent / send_failed）按 `action_id` 回复一次。

```json
{"type": "ack", "event_id": "im:e:…"}
{"type": "ack", "action_id": "act_…"}
```

### get_media

图片或表情包的 segment 没有内联数据且为 `available: true` 时，服务端按 `media_id` 请求文件。

```json
{"type": "get_media", "media_id": "im:media:…"}
```

### send_message

```json
{"type": "send_message", "action_id": "act_…", "chat_id": "im:g:…",
 "text": "可以多行的纯文字",
 "quote": "im:m:…",
 "delay_ms": 1200,
 "expires_at": 1762617630000}
```

- 只发送纯文字，可以带引用（`quote` 可省略）。不发送真正的 @，需要指名某人时以引用代替。
- `delay_ms`：收到后最早多久发送。发送在连接器中串行排队，实际发送时间可能更晚。
- `expires_at`：到该时间仍未发出则丢弃，并回报 `send_failed(reason=expired)`。
- `action_id` 去重（`action_dedupe`）：连接器持久化保存最近 500 个 `action_id` 及其回报。已有回报的 `action_id` 再次收到时不再发送，直接重发原回报；仍在排队或正在发送的，不另行回报；连接器在输入前重启的，按原 `expires_at` 正常执行。服务端重启后，对已开始发送但未收到回报的回复，以原 `action_id` 和原文字重新发送一次以获取结果（不带 `quote`）。
- 一次回复最多分为三个 `send_message` 连续发送：服务端收到前一条的 `message_sent` 并间隔数秒后，才发送下一条（`delay_ms` 为 0）；期间有人插话时，剩余部分不再发送。
- 服务端发送 `send_message` 时连接器不在线，这条回复视为未发出；连接器重新连接后，服务端重新回应相应消息（未超过 120 秒时）。
- 角色可以选择不回复（例如道过晚安后再次被叫到），此时即使被 @ 也不会有 `send_message`。这是正常行为，并非消息丢失；相应消息在服务端仍标记为已处理。

## 限流

服务端控制发送节奏：同一会话中两条消息的间隔不少于输入后一条所需的时间（按每分钟约 500 次按键计算，一个汉字计 3 次），最多 5 秒；没有全局上限。

连接器可以另设发送频率上限，防止失控。达到上限时回报 `send_failed(reason=rate_limited)`，表示消息确定未发出。
