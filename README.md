# astrbot_plugin_major_tournament

> AstrBot「Major 赛制」锦标赛插件：群内报名 → 管理员人工判胜 → 自动生成 **32/16/8/4/2 强** 单败淘汰赛程 → 渲染 Major 风格对阵图。

## ✨ 功能

- **报名**：群成员发送 `major 报名` 即可参赛，支持自定义显示名、管理员代报名。
- **人工判胜**：每一场由管理员手动指定胜者（支持按位置 `1`/`2` 或输入选手名字），并可选记录比分。
- **自动排赛**：开赛时按报名人数自动选择 **32/16/8/4/2 强** 规模，采用**标准种子排位**（`1vN`、`2v(N-1)` …），高种子不会提前相遇；人数不足时自动**轮空晋级**。
- **自动晋级**：判定一场胜负后，胜者自动进入下一轮；决赛判定后自动产生冠军。
- **对阵图渲染**：参考 `astrbot_plugin_qq_group_daily_analysis` 的 T2I 方案，用 Jinja2 渲染 HTML 后交给 AstrBot 的 `html_render` 转成图片。
- **按钮面板（QQ 官方机器人）**：发送 `major` 即可收到 Markdown + 按钮面板，报名 / 退赛 / 名单 / 赛程 / 判胜全部可点击完成，无需记指令。
- **持久化**：每个群一份 JSON，多平台/多群互不干扰。

## 📦 安装

1. 把本仓库克隆/复制到 AstrBot 的 `data/plugins/` 目录下：

   ```bash
   cd AstrBot/data/plugins
   git clone https://github.com/li-shi-ning/astrbot_plugin_major_tournament.git
   ```

2. 重启 AstrBot 或在 WebUI 插件页重载插件。
3. 依赖：`jinja2>=3.1`（AstrBot 已内置；若缺失请 `pip install jinja2`）。

## 🎮 指令

| 指令 | 权限 | 说明 |
| --- | --- | --- |
| `major` / `major面板` | 所有人 | 发送按钮面板（QQ 官方）；其它平台发帮助 |
| `major 帮助` | 所有人 | 查看帮助 |
| `major 报名 [名字]` | 所有人 | 报名参赛 |
| `major 退赛` | 所有人 | 取消报名（仅开赛前） |
| `major 名单` | 所有人 | 查看报名名单 |
| `major 开赛 [规模]` | 管理员 | 开赛，可指定 `32/16/8/4/2` |
| `major 对阵` | 所有人 | 文字版赛程 |
| `major 图` | 所有人 | 渲染 Major 对阵图 |
| `major 胜 <编号> <1\|2\|名字> [比分]` | 管理员 | 判定胜负，例：`major 胜 R16-3 1 2:1` |
| `major 添加 <账号> [名字]` | 管理员 | 代他人报名，支持 `@某人` |
| `major 重置` | 管理员 | 删除当前赛事 |

别名：`锦标赛`、`major赛`、`major比赛`。  
比赛编号形如 `R32-1`（32 强第 1 场）、`R16-3`、`R2-1`（决赛）。

## 🔘 按钮面板（QQ 官方机器人）

在 QQ 官方机器人 / Webhook 平台发送 `major`（或不带参数的 `major面板` / `major按钮` / `major菜单`）会回复一条 **Markdown + 键盘按钮** 消息：

| 按钮 | 说明 |
| --- | --- |
| 📝 报名 / 🚪 退赛 | 所有人可见，开赛前可点击 |
| 👥 名单 / 📋 赛程 / 🖼 对阵图 | 查看类按钮 |
| 🚀 开赛 / 🗑 重置 | 仅管理员面板显示 |
| ⚔ `<编号> <选手>` | 开赛后，管理员面板会为待判定的比赛各生成两个按钮，点谁谁晋级 |

实现要点（参考 `astrbot_plugin_util_official`）：

- 全部使用 `action.type = 2` 的**指令按钮**，点击后 QQ 会把按钮的 `data` 当作一条 @机器人 消息发出，因此 `data` 直接复用现有命令（如 `major 报名`、`major 胜 R16-1 1`），**不需要** `INTERACTION_CREATE` 回调，也不用补 interaction intent。
- 发送时附带被动回复上下文（`msg_id` + `msg_seq`），避免被平台当作主动消息拦截。
- 按钮通过 `event.bot.api.post_group_message(..., msg_type=2, markdown=..., keyboard=...)` 直接发送；发送成功后 `stop_event()`。
- 管理员按钮只是「面板可见性」层面的收敛，真正的权限校验仍在命令处理里（`event.is_admin()`），非管理员即使点到也会被拒绝。
- 相关配置：`buttons_enabled`（总开关，默认开）、`button_auto_refresh`（报名/退赛/开赛/判胜/重置后自动重发面板，默认开）。

## 🖼️ 对阵图渲染说明

渲染链路与 `astrbot_plugin_qq_group_daily_analysis` 一致：

1. `core/renderer.py` 用 **Jinja2** 把赛事对象渲染成完整 HTML（`templates/major_bracket.html`）；
2. 调用 AstrBot 的 `self.html_render(html, {}, return_url=False, options=...)`，由 T2I 服务把 HTML 转成图片字节；
3. 通过 `event.make_result().base64_image(...)` 发送，避免 OneBot / QQ 官方机器人访问不到内部地址。

对阵图列布局使用 flex 等分 + 居中，保证每一轮的比赛卡片正好位于其两个「上游」卡片中点，视觉上形成标准淘汰赛树。

## 🧪 测试

```bash
python -m pytest tests/ -q
```

## 📄 License

MIT
