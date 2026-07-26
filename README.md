# read-x

飞书长文精读系统：链接自动抓取 -> 内容质量评分 -> 分层精读 -> 飞书文档输出。收到任何链接即触发，以飞书交互卡片（bot 身份）回复。

## 使用

依赖 [Codex CLI](https://github.com/openai/codex)、lark-channel-bridge（飞书桥接）和 `lark-cli`（飞书应用）。

**1. 克隆**

```bash
git clone https://github.com/SimplyY/read-x.git
cd read-x
```

**2. 安装 ljg-skills**

`long-read` 调度 7 个文字 Skill，用 [skills CLI](https://github.com/vercel-labs/skills) 装到全局：

```bash
bunx skills add lijigang/ljg-skills -g -a codex \
  --skill ljg-think --skill ljg-learn --skill ljg-roundtable \
  --skill ljg-qa --skill ljg-writes --skill ljg-word --skill ljg-card -y
```

`ljg-card` 需 Playwright：`cd ~/.agents/skills/ljg-card && bun install && bunx playwright install chromium`

**3. 触发**

配置飞书应用与 bridge 后，在飞书发文章链接（公众号 / 网页 / 飞书文档），自动抓取评分精读，卡片回复，高质量内容生成飞书文档。

## 核心资产

- [精读索引](https://ywhome.feishu.cn/base/ASdsbB3Gka9OKNsD7YhcJ9rZnjd)
- [Codex CLI](https://github.com/openai/codex)
- [`ljg-skills`](https://github.com/lijigang/ljg-skills)
- [skills CLI](https://github.com/vercel-labs/skills)
- [GitHub](https://github.com/SimplyY/read-x)

## 核心功能

- **链接抓取**：微信公众号（`wechat-article-to-markdown`）、即刻、通用网页、飞书文档、纯文本
- **内容质量评分**：`content-scoring` v3 四维质量分 + 隔离相关性分，同一正文质量只评一次
- **分层处理**：确定性脚本输出卡片 / 轻量精读 / 深度精读路由
- **长文精读**：`long-read` 编排器，Evidence -> X 光解码 -> 文字深度链路 -> Docx XML -> 飞书文档
- **卡片输出**：所有结果以飞书交互卡片回复，`--as bot` 身份发送

## 数据流

```
链接
 ↓
link-card（抓取）
 ↓
content-scoring（quality / relevance / decision）
 ↓
┌──────────────────────┬──────────────────────┐
│ route=card           │ route=long_read      │
│ 状态/一句话/轻量卡片   │ long-read 全流程      │
└──────────────────────┴──────────────────────┘
                                              ↓
                            Evidence -> article-decode（隔离）
                                     + 文字 ljg（隔离）
                                     -> Docx XML -> 飞书文档
                                     -> 私聊卡片通知
                            （ljg_card=true 时额外私聊 PNG）
```

## Skill 架构

仓库内自有 4 个 Skill（`.agents/skills/`）：

| Skill | 职责 |
|-------|------|
| `link-card` | **入口编排器**。抓取 -> 调 content-scoring -> 路由 -> 卡片输出 |
| `content-scoring` | **评分引擎**。四维质量、独立相关性与确定性路由；link-card 与 long-read 共用 |
| `long-read` | **深度编排器**。Evidence -> article-decode（隔离）+ 文字 ljg（隔离）-> 拼接飞书文档 |
| `article-decode` | **X 光解码**。隔离运行，产出 Evidence 与解码骨架 |

调用关系：

- `link-card` 调 `content-scoring`，按脚本返回的 `route` 走卡片或 `long-read`
- `long-read` 调 `article-decode`（X 光），再调度外部 `ljg-*` 文字 Skill
- `content-scoring` 结果传给 `long-read`，long-read 不重评

## 与 ljg-skills 的关系

[`ljg-skills`](https://github.com/lijigang/ljg-skills) 是独立的外部 Skill 仓库，通过 [skills CLI](https://github.com/vercel-labs/skills) 安装到全局 `~/.agents/skills/`，**不在本仓库内**。

`long-read` 通过 `references/routing.md` 调度其中 7 个 Skill 做文字深度链路，每个在隔离上下文运行，互不可见：

| 触发条件 | Skill |
|----------|-------|
| 单一概念、底层机制 | `ljg-think` |
| 多层结构、多维系统 | `ljg-learn` |
| 真实争议、利益冲突 | `ljg-roundtable` |
| 长因果链、逐问推进 | `ljg-qa` |
| 值得独立成文批评 | `ljg-writes` |
| 罕见概念或单词 | `ljg-word` |
| `ljg_card=true` 生成卡片图 | `ljg-card`（私聊触发者，不进文档） |

文字数量直接消费 `content-scoring` 的 `ljg_range`；相关性只影响边界文章是否进入 long-read，不改变深度。

## 脚本

| 脚本 | 作用 |
|------|------|
| `scripts/content_scoring.py` | content-scoring 评分计算 |
| `scripts/wx_fast.py` | 微信文章抓取（备用，httpx 直连） |
| `scripts/test_content_scoring.py` | 评分单元、对抗与 CLI 端到端测试 |
| `scripts/validate_long_read_skill.sh` | long-read Skill 校验 |

## 飞书文档段落顺序

精读文档结论先行，使用 Docx XML 原生排版：

1. 顶部：评分表 + 核心结论高亮块
2. 主文：核心 -> 基石/边缘/暗流 -> 与作者对话 -> 最值得深读之处
3. 附录：导言 + 各文字 ljg 完整原稿
4. 文末：Evidence 金句 + 原文链接

## 运行环境

- 飞书机器人经 lark-channel-bridge 接入，`lark-cli` 创建飞书文档、发送卡片
- 卡片以 bot 身份（`--as bot`）发送
- 抓取/分析临时文件（`.wx_*`）已在 `.gitignore` 忽略

## 相关

- [ljg-skills](https://github.com/lijigang/ljg-skills) - 外部文字 Skill 仓库
- [skills CLI](https://github.com/vercel-labs/skills) - Skill 安装工具
