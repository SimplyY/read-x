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
  --skill ljg-qa --skill ljg-writes --skill ljg-word -y
```


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
- **内容质量评分**：`content-scoring` 评分结果 v3.18（质量输出仍为 v3.16）先删除标题、作者、日期、URL 与抓取器噪声，再由当前 Codex 模型一次完成证据、洞察、迁移三维质量与独立的大问题思考分；独立 authority resolver 用公开身份包和程序固定的 `tvly` 真实搜索（最多 3 查询/4 页面，`query_count=0` 永远不会显示为已核验）生成实体—专业性—主题匹配证据，权威性缺失时由大问题分承接完整 30% 重要性权重；相关性和兴趣也在隔离上下文中由当前 Codex 模型计算；脚本按 `70%质量 + 30%重要性` 合成决策分
- **运行时配置**：每次评分读取「ReadX 精读」多维表格的运行级快照；路由门槛组可配置芒格后处理门槛（兼容字段 `ChatGPT 芒格门槛`），缺失时回退 8.3；读取失败时回退本地策略，并在 `scoring_result.policy_source` 标明来源
- **仅评分**：发送 `仅评分 <URL>` 仍执行真实评分与路由计算，但评分卡后不进入精读
- **分层处理**：确定性脚本输出卡片 / 轻量精读 / 深度精读路由
- **长文精读**：`long-read` 通过 `long_read_workflow_cli.py submit` 持久接管 `route=long_read`，由 Worker 从文字分析推进到程序装配、Document Compiler 主文档发布、图片插入与私聊发送核验；只有全部必需产物确认后才报告最终成功，进程中断后从检查点恢复
- **工作流留存**：终态输入与产物 30 天后清除并脱敏；未解决任务及未完成报告投递持续保留；逐任务统计与已完成报告快照保留 180 天，之后仅保留无任务标识的生命周期总计
- **格式保真**：芒格洞察由 ChatGPT Bridge 生成规范 Markdown，作为一级主章节「芒格洞察」拼进主文档，统一渲染 Feishu XML；交付卡单链接并注明「含芒格洞察」；编排层只提供原文和最小任务边界，不强制芒格章节套用固定标题模板，临时 Markdown 交付后清理
- **卡片输出**：所有结果以飞书交互卡片回复，快速阅读卡附带速读内容，缺失时失败关闭，`--as bot` 身份发送

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
                   long_read_workflow_cli.py submit -> SQLite + 私有输入快照
                                              ↓
                         持久 Worker：Bridge 分支 -> 程序装配
                                              ↓
                         Document Compiler -> 主文档发布与读回
                                              ↓
                 图片生成 -> 插入读回 -> 私聊发送读回 -> 唯一完成门
                                              ↓
                    周报阈值触发 / 每月报告（同一持久工作流）
```

## Skill 架构

仓库内自有 4 个 Skill（`.agents/skills/`）：

| Skill | 职责 |
|-------|------|
| `link-card` | **入口编排器**。抓取 -> 调 content-scoring -> 路由 -> 卡片输出 |
| `content-scoring` | **评分引擎**。三维质量、权威性与大问题思考、独立相关性与确定性路由；link-card 与 long-read 共用 |
| `long-read` | **持久交付工作流**。Evidence 与评分凭据经 CLI 持久接管；Worker 检查点推进 Bridge 分支、程序装配、主文档发布、必需图片交付和结果核验；从中断恢复并生成周/月可靠性报告 |
| `article-decode` | **X 光解码**。只读原文与 Evidence，产出独立解码原稿 |

调用关系：

- `link-card` 调 `content-scoring`，按脚本返回的 `route` 走卡片或 `long-read`
- `long-read` 在 `route=long_read` 后调用 `long_read_workflow_cli.py submit`，由持久 Worker 调 `article-decode`、文字 ljg 和芒格分支；已有阶段收据通过后续检查点复用
- Worker 统一负责主文档与图片交付、通知及唯一成功门；图片生成、插入和发送共享每图最多两轮额外恢复，未知远端结果只读核验
- 状态通知走持久发件箱；限流或已确认拒绝后至少间隔 60 秒，最多额外尝试两次，沿用同一请求 UUID 并先读完整消息历史核验，超过 Feishu 一小时去重窗口或结果不明确时不自动重发
- 同一状态库支持周日 09:00 阈值周报、每月 1 日 09:00 月报及漏跑周期补处理；报告仍需配置并核验本人私聊投递目标
- `content-scoring` 结果传给 `long-read`，long-read 不重评

## 与 ljg-skills 的关系

[`ljg-skills`](https://github.com/lijigang/ljg-skills) 是独立的外部 Skill 仓库，通过 [skills CLI](https://github.com/vercel-labs/skills) 安装到全局 `~/.agents/skills/`，**不在本仓库内**。

`long-read` 通过 `references/routing.md` 调度其中 6 个 Skill；文字分析由 ChatGPT web-bridge 独立会话运行，互不可见：

| 触发条件 | Skill |
|----------|-------|
| 单一概念、底层机制 | `ljg-think` |
| 多层结构、多维系统 | `ljg-learn` |
| 真实争议、利益冲突 | `ljg-roundtable` |
| 长因果链、逐问推进 | `ljg-qa` |
| 值得独立成文批评 | `ljg-writes` |
| 罕见概念或单词 | `ljg-word` |

文字数量直接消费 `content-scoring` 的 `ljg_range`；相关性只影响边界文章是否进入 long-read，不改变深度。

## 脚本

| 脚本 | 作用 |
|------|------|
| `scripts/content_scoring.py` | content-scoring 评分计算 |
| `scripts/verify_source_authority.py` | 原始出处只读核验与权威性证据产物 |
| `scripts/build_authority_identity.py` | 从公开文章元数据生成 identity_packet v1，供独立搜索桥使用 |
| `scripts/fetch_base_config.py` | 读取并校验精读配置 Base，生成运行级 JSON 快照 |
| `scripts/wx_fast.py` | 微信文章抓取（httpx 直连，不启动浏览器） |
| `scripts/test_content_scoring.py` | 评分单元、对抗与 CLI 端到端测试 |
| `scripts/prepare_anchor_view.py` | 生成外部校准审计视图；生产评分不读取 |
| `scripts/validate_long_read_skill.sh` | long-read Skill 校验 |
| `.agents/skills/long-read/scripts/long_read_workflow_cli.py` | 持久接管、Worker 推进、任务/报告只读状态、恢复、人工处置/修复证据记录与可靠性报告 CLI |
| `.agents/skills/long-read/scripts/long_read_workflow_retention.py` | 终态内容清理、统计脱敏与 180 天留存边界 |
| `.agents/skills/long-read/scripts/long_read_workflow_worker.py` | 长文工作流检查点、分析编排、装配、主文档及图片交付状态机 |
| `.agents/skills/long-read/scripts/long_read_workflow_reports.py` | 固定窗口周/月统计快照、指标和报告正文 |
| `.agents/skills/long-read/scripts/run_isolated_analyses.py` | 经 ChatGPT web-bridge 并行运行独立 article-decode 与文字 ljg 会话 |
| `.agents/skills/long-read/scripts/run_chatgpt_munger.py` | 调用 ChatGPT Bridge 生成芒格洞察原稿（每篇必跑） |
| `.agents/skills/long-read/scripts/run_chatgpt_core_image.py` | 调用 ChatGPT Bridge image 模式对完整精读 markdown 生成核心内容图 PNG |
| `.agents/skills/long-read/scripts/run_long_read_pipeline.py` | 旧临时流水线，仅供历史兼容和局部测试；不作为持久交付入口 |
| `.agents/skills/long-read/scripts/long_read_workflow_scheduler.py` | 固定周/月报告周期与漏跑周期补处理；由常驻 Worker 调用 |
| `.agents/skills/long-read/scripts/markdown_to_feishu_xml.py` | 共享 Feishu Markdown→XML 渲染器的 read-x 兼容入口 |
| `/Users/yuwei/.codex/skills/feishu-doc-renderer` | 跨仓库复用的纯 Markdown→Feishu XML 排版 Skill |
| `scripts/render_long_read_delivery_card.py` | 生成唯一长文交付 Card 2.0 JSON，避免换行转义错误 |
| `.agents/skills/long-read/scripts/evaluate_analyses.py` | 对比隔离产物的机械完整性，并以 summary 验证同任务性能非回退 |

## 飞书文档段落顺序

精读文档结论先行，使用 Docx XML 原生排版：

1. 根 `<title>` 只保留文章标题；正文首个主章节为一级标题 `评分`
2. 顶部：评分表 + 核心结论高亮块
3. 主文：核心 -> 基石/边缘/暗流 -> 值得研究的相关问题（独立问题列表 + 共同上下文列表，总计 ≤300 字） -> 与作者对话 -> 最值得深读之处
4. 芒格洞察（ChatGPT Bridge 分支成功时的一级主章节，失败时整章省略并注明待复核）
5. 附录：导言 + 各文字 ljg 完整原稿
6. 文末：必要事实（若有）

## 运行环境

- 飞书机器人经 lark-channel-bridge 接入，`lark-cli` 创建飞书文档、发送卡片
- 卡片以 bot 身份（`--as bot`）发送
- 抓取/分析临时文件（`.wx_*`）已在 `.gitignore` 忽略

## 相关

- [ljg-skills](https://github.com/lijigang/ljg-skills) - 外部文字 Skill 仓库
- [skills CLI](https://github.com/vercel-labs/skills) - Skill 安装工具
