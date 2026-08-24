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

- **链接抓取**：微信公众号（`wx_fast.py` 纯 HTTP）、即刻、通用网页、飞书文档、纯文本
- **内容质量评分**：`content-scoring` v3.15 先删除标题、作者、日期、URL 与抓取器噪声，再由同一模型一次完成证据、洞察、迁移三维闭卷分档与主张预算；脚本从原文确定性组装逐字引用、应用反证封顶并计算总分；锚点只用于事后回归，仅在相关性可改变路由时隔离计算相关性
- **运行时配置**：每次评分读取「ReadX 精读」多维表格的运行级快照；读取失败时回退本地策略，并在 `scoring_result.policy_source` 标明来源
- **仅评分**：发送 `仅评分 <URL>` 仍执行真实评分与路由计算，但评分卡后不进入精读
- **分层处理**：确定性脚本输出卡片 / 轻量精读 / 深度精读路由
- **长文精读**：`long-read` 编排器，Evidence -> 独立 HTTP 并行解码/文字深度链路 -> Docx XML -> 飞书文档
- **卡片输出**：所有结果以飞书交互卡片回复，`--as bot` 身份发送

## 数据流

```
链接
 ↓
link-card（抓取）
 ↓
content-scoring（quality -> 条件 relevance -> decision）
 ↓
┌──────────────────────┬──────────────────────┐
│ route=card           │ route=long_read      │
│ 状态/一句话/轻量卡片   │ long-read 全流程      │
└──────────────────────┴──────────────────────┘
                                              ↓
                            Evidence -> article-decode + 文字 ljg
                                     （独立 store=false HTTP，并行）
                                     -> Docx XML -> 飞书文档
                                     -> 私聊卡片通知
                            （ljg_card=true 时额外私聊 PNG）
```

## Skill 架构

仓库内自有 4 个 Skill（`.agents/skills/`）：

| Skill | 职责 |
|-------|------|
| `link-card` | **入口编排器**。抓取 -> 调 content-scoring -> 路由 -> 卡片输出 |
| `content-scoring` | **评分引擎**。三维质量、独立相关性与确定性路由；link-card 与 long-read 共用 |
| `long-read` | **深度编排器**。Evidence -> 独立 HTTP 并行 article-decode + 文字 ljg -> 拼接飞书文档 |
| `article-decode` | **X 光解码**。只读原文与 Evidence，产出独立解码原稿 |

调用关系：

- `link-card` 调 `content-scoring`，按脚本返回的 `route` 走卡片或 `long-read`
- `long-read` 调 `article-decode`（X 光），再调度外部 `ljg-*` 文字 Skill
- `content-scoring` 结果传给 `long-read`，long-read 不重评

## 与 ljg-skills 的关系

[`ljg-skills`](https://github.com/lijigang/ljg-skills) 是独立的外部 Skill 仓库，通过 [skills CLI](https://github.com/vercel-labs/skills) 安装到全局 `~/.agents/skills/`，**不在本仓库内**。

`long-read` 通过 `references/routing.md` 调度其中 7 个 Skill；文字分析由独立 HTTP 请求运行，互不可见：

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
| `scripts/fetch_base_config.py` | 读取并校验精读配置 Base，生成运行级 JSON 快照 |
| `scripts/wx_fast.py` | 微信文章抓取（httpx 直连，不启动浏览器） |
| `scripts/test_content_scoring.py` | 评分单元、对抗与 CLI 端到端测试 |
| `scripts/prepare_anchor_view.py` | 生成外部校准审计视图；生产评分不读取 |
| `scripts/validate_long_read_skill.sh` | long-read Skill 校验 |
| `.agents/skills/long-read/scripts/run_isolated_analyses.py` | 独立 HTTP 并行运行 article-decode 与文字 ljg |
| `.agents/skills/long-read/scripts/evaluate_analyses.py` | 对比隔离产物的机械完整性，并以 summary 验证同任务性能非回退 |

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
