# 演示 GIF 分镜脚本（30 秒）

> 目标：让观众在 10 秒内看懂"普通聊天 → 自动产生记忆 → 新对话自动召回"这个魔法时刻。
> 用途：README 顶部、Show HN 正文、V2EX 帖子。
> 原则：不展示安装和配置过程（那是文档的工作），只展示结果。

---

## 分镜

### 第 0 幕｜标题卡（0:00 – 0:02，可循环衔接）

- 纯底色画面，居中一行字：
  - 英文版：`Memory Platform — your chat client, now with long-term memory`
  - 中文版：`Memory Platform —— 给你的聊天客户端加上长期记忆`
- 右下角小字：`Local-first · Auditable · Apache-2.0`

### 第 1 幕｜普通聊天里说出偏好（0:02 – 0:09）

- 画面：Chatbox（或任一 OpenAI 兼容客户端），**全新空白对话**。
- 动作：输入并发送一条消息：
  - 英文：`I'm allergic to peanuts — keep that in mind for food suggestions.`
  - 中文：`我对花生过敏，以后推荐吃的要注意。`
- AI 正常回复确认（等待时间在剪辑中剪掉，回复逐字出现保留 2–3 秒即可）。
- 屏幕角落字幕条：`① Just chat normally — no MCP, no "remember this" prompts`（中文版：`① 正常聊天即可——不用 MCP，不用"请记住"提示词`）

### 第 2 幕｜记忆里出现了什么（0:09 – 0:16）

- 画面：切换到浏览器 `http://127.0.0.1:2026/ui/` 的记忆工作室页。
- 动作：记忆列表顶部出现刚生成的记忆（"对花生过敏"），点击展开，**停在"来源 / 召回解释"区域 2 秒**——这是差异化卖点，必须给特写。
- 字幕条：`② Every memory keeps its source — see exactly why it was saved`（中文版：`② 每条记忆都有来源——能看到它为什么被保存`）

### 第 3 幕｜新对话自动召回（0:16 – 0:26，全片重点）

- 画面：切回客户端，** visibly 点"新建对话"**（让观众确认是全新会话）。
- 动作：输入：
  - 英文：`Any restaurant picks for a team dinner this weekend?`
  - 中文：`周末聚餐有什么餐厅推荐？`
- AI 回复中主动避开花生/提及过敏（例如 "…avoiding peanut dishes given your allergy"）。
- 字幕条：`③ New conversation — recalled automatically, nothing copy-pasted`（中文版：`③ 全新对话——记忆自动召回，没有手动提供任何背景`）
- 回复里涉及过敏的那一句可以用高亮框圈出（后期加，1 秒淡入）。

### 第 4 幕｜结尾卡（0:26 – 0:30）

- 画面：回到 Web Console 全景（或品牌横幅）。
- 居中两行：
  - `github.com/SparkHello/Memory_Platform`
  - `Docker 两条命令即可自托管` / `Self-host with two Docker commands`
- 自然停 2 秒后循环回第 0 幕（GIF 首尾帧色调一致，循环不突兀）。

---

## 录制与制作规格

| 项 | 建议 |
| --- | --- |
| 尺寸 | 1440×900 或 1280×720，@2x 录制后缩到 1x |
| 帧率 | 15–24 fps（GIF 够流畅且体积可控） |
| 体积 | 目标 < 8 MB；超了用 `gifski --quality 80` 或降帧率压 |
| 工具（macOS） | 录屏：Kap 或 Screen Studio；压缩：gifski；高亮框/字幕：Screen Studio 内建或 Keynote 导出 |
| 界面 | 全程深色或全程浅色，不要混；浏览器隐藏书签栏；客户端用干净账号 |
| 字幕 | 英文版为主（HN/Reddit/README 通用）；导出时另存一份中文字幕版给 V2EX |
| 语速 | 打字可以 1.5–2x 加速；AI 回复等待全部剪掉；每幕之间留 0.3 秒黑帧或硬切 |

## 录制前准备清单

- [ ] 用一个**全新的演示数据目录**启动栈（不要用真实记忆库；README 已承诺界面均为演示数据）。
- [ ] 预置 3–4 条无害的演示记忆（如"偏好简洁回答""正在学西班牙语"），让 Web Console 列表看起来不空。
- [ ] 模型渠道选一个**首 token 快**的模型，减少要剪掉的等待。
- [ ] 先把第 1、3 幕的消息和预期回复全流程彩排一遍，确认"花生过敏"会被稳定提取和召回，再正式录。
- [ ] 录完后导出两个文件：`docs/images/demo.en.gif` 和 `docs/images/demo.zh-CN.gif`。

## 嵌入位置

录好后替换 README 中 hero 图下方、或"✨ 1 分钟了解"之前的位置：

```markdown
![30 秒演示：普通聊天自动产生可审计的长期记忆，并在新对话中自动召回](docs/images/demo.zh-CN.gif)
```

README.en.md 对应使用 `demo.en.gif`。
