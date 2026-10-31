# nonebot-plugin-iinfo

发送 `iinfo`，返回当前会话所有可获取的信息。纯 NoneBot 核心实现，无第三方插件依赖，任何适配器下都能直接加载。

## 功能

- **多模式输出**：精简 / 标识符 / 消息结构 / API 探测 / 完整版，按需取用
- **适配器无关**：只用 NoneBot 核心抽象 + `getattr` 字段探测，不 import 任何适配器模块
- **QQ 官方 Bot（openid 体系）增强**：识别 `nonebot-adapter-qq` 事件，集中展示 `group_openid` / `member_openid` / `user_openid` / `union_openid`、`member_role`（member/admin/owner）、附件、提及、引用消息等
- **同值标识符折叠**：同值的多个标识（如 C2C 下三种 openid 同值、群聊下 `group_id` 与 `group_openid` 同值）合并为一行「XX、YY同上」
- **msg_idx 补捞**：顶层 `msg_idx` 为空时自动从 `message_scene.ext` 中解析 `msg_idx=REFIDX_...`（被动回复序号）

## 使用

| 命令 | 版本 | 说明 |
|---|---|---|
| `iinfo` | 精简版（默认） | 单条回复：标识符去重清单 + 成员身份 + 事件名 + 纯文本预览 |
| `iinfo ids` | 标识符专版 | 全部标识符全量展示（openid / 消息 ID / msg_idx / event_id），配置迁移用 |
| `iinfo msg` | 消息结构专版 | 消息段列表、原始 content、mentions、reply、attachments、message_scene |
| `iinfo api` | API 探测专版 | `me()` / `get_group_info` / `get_guild` / `get_login_info` 探测结果 + 当前 bot 支持的 API 清单 |
| `iinfo full` / `iinfo完整` | 完整版 | 完整摘要 + 分段完整事件 JSON |

命令支持 `/`、`。` 等常见前缀。完整版会分段发送 JSON，注意 QQ 官方 Bot C2C 场景被动回复每条最多 4 次。

## 安装

```
nb plugin install nonebot-plugin-iinfo
```

或直接把 `nonebot_plugin_iinfo` 目录放入 bot 的插件目录（如 `plugins/`）。

## 在 QQ 官方 Bot 下的输出边界

| 信息 | 可得性 |
|---|---|
| `group_openid` / `member_openid` / `union_openid` | ✅ 事件自带 |
| 成员身份（admin/owner） | ✅ 事件自带 `member_role` |
| 消息附件（含 URL）、提及、引用 | ✅ 事件自带 |
| 机器人自身信息 | ✅ `GET /users/@me` |
| 群名 / 群信息 | ❌ 官方无群信息 API，探测静默跳过 |
| 成员列表 / 昵称 | ❌ 成员端点暂未开放 |

## License

MIT
