# 社区 / 学习 / 线索数据源规划

## 定位与证据边界

本清单用于学习 Web Research、RAG、OSINT、Search Agent 的工程实践，以及发现
半导体行业的新话题、新技术和潜在线索。它只为后续正式数据源搜索提供 query、
entity 和 keyword，不是正式事实证据源。

统一证据策略 `community_lead_only_v1`：

> 社区内容只能作为线索，不允许单独作为最终事实证据。
> 如果内容影响最终报告的重要结论，必须继续搜索 A/B 级官方或权威来源进行验证。

所有来源在 Demo V0 中均为 `enabled_for_demo: false`，不得进入默认行业报告或竞品报告的
evidence 集合。当前 Demo 的正式来源保持不变，包括政府官网、BIS、Federal Register、
企业官网、巨潮资讯等。

未来如启用社区搜索，必须遵循以下单向流程：

```text
社区发现线索
  -> 提取实体 / 事件 / 关键词
  -> 搜索官方或权威来源
  -> 找到验证证据
  -> 才进入正式事件集合
```

未找到 A/B 级验证证据的内容只能留在线索队列，并标记为未验证；不能进入正式事件集合，
也不能生成确定性结论。

## Metadata 约定

每条来源至少包含以下字段：

| 字段 | 约束 |
|---|---|
| `source_id` | 稳定、唯一的 snake_case 标识 |
| `source_name` | 来源公开名称 |
| `url` | 公开入口 URL |
| `usage_role` | `learning` 或 `lead`；用于区分学习资料和行业线索 |
| `category` | `engineering_learning`、`industry_lead` 或 `technical_forum` |
| `purpose` | 允许的发现或学习用途 |
| `trust_level` | `community` 或 `lead_only` |
| `evidence_policy` | 固定为 `community_lead_only_v1` |
| `enabled_for_demo` | Demo V0 固定为 `false` |
| `crawling_policy` | 下方定义的受限采集策略 |

`usage_role` 不改变证据等级：`learning` 和 `lead` 都不能绕过
`community_lead_only_v1`。

## Crawling Policy

`public_community_discovery_only`：未来启用时，只允许低频访问公开、无需登录的页面，
并遵守站点 robots、服务条款和速率限制。只提取线索所需的 URL、标题、发布时间、作者
（如公开）、关键词和短摘要；不得绕过反爬、抓取登录态、大规模回溯历史内容或永久存储全文。

`public_repository_index_only`：未来启用时，只允许从公开仓库的索引/README 和公开元数据中
提取论文、benchmark、项目链接、标题和关键词，并遵守 GitHub 服务条款和速率限制；不得镜像
仓库、批量下载链接目标或永久存储社区内容全文。被索引的论文或项目如影响正式结论，仍须回到
原始论文、官方项目或其他 A/B 级来源验证。

## 来源清单

```yaml
sources:
  - source_id: reddit_osint
    source_name: Reddit r/OSINT
    url: https://www.reddit.com/r/OSINT/
    usage_role: learning
    category: engineering_learning
    purpose: 公开情报采集、公司研究、多来源验证、信息可信度和 OSINT workflow 的工程学习。
    trust_level: community
    evidence_policy: community_lead_only_v1
    enabled_for_demo: false
    crawling_policy: public_community_discovery_only

  - source_id: reddit_rag
    source_name: Reddit r/RAG
    url: https://www.reddit.com/r/Rag/
    usage_role: learning
    category: engineering_learning
    purpose: RAG、Web Search、Agentic RAG、Deep Research、citation、文档解析和检索评测的工程学习。
    trust_level: community
    evidence_policy: community_lead_only_v1
    enabled_for_demo: false
    crawling_policy: public_community_discovery_only

  - source_id: hacker_news
    source_name: Hacker News
    url: https://news.ycombinator.com/
    usage_role: learning
    category: engineering_learning
    purpose: Deep Research、Search Agent、AI Agent 的工程经验、架构取舍和失败案例学习。
    trust_level: community
    evidence_policy: community_lead_only_v1
    enabled_for_demo: false
    crawling_policy: public_community_discovery_only

  - source_id: reddit_localllama
    source_name: Reddit r/LocalLLaMA
    url: https://www.reddit.com/r/LocalLLaMA/
    usage_role: learning
    category: engineering_learning
    purpose: 本地模型、工具调用、Search Agent 和本地部署的工程学习。
    trust_level: community
    evidence_policy: community_lead_only_v1
    enabled_for_demo: false
    crawling_policy: public_community_discovery_only

  - source_id: awesome_search_agent_papers
    source_name: Awesome Search Agent Papers
    url: https://github.com/YunjiaXi/Awesome-Search-Agent-Papers
    usage_role: learning
    category: engineering_learning
    purpose: 发现 Search Agent、Deep Search、Agentic RAG、Deep Research 论文与 benchmark。
    trust_level: lead_only
    evidence_policy: community_lead_only_v1
    enabled_for_demo: false
    crawling_policy: public_repository_index_only

  - source_id: awesome_deep_research
    source_name: Awesome Deep Research
    url: https://github.com/DavidZWZ/Awesome-Deep-Research
    usage_role: learning
    category: engineering_learning
    purpose: 发现 Deep Research 开源实现、多源检索和研究型 Agent 架构。
    trust_level: lead_only
    evidence_policy: community_lead_only_v1
    enabled_for_demo: false
    crawling_policy: public_repository_index_only

  - source_id: reddit_semiconductors
    source_name: Reddit r/Semiconductors
    url: https://www.reddit.com/r/Semiconductors/
    usage_role: lead
    category: industry_lead
    purpose: 发现半导体产业、供应链、晶圆厂、设备、政策和公司动态线索。
    trust_level: community
    evidence_policy: community_lead_only_v1
    enabled_for_demo: false
    crawling_policy: public_community_discovery_only

  - source_id: semiwiki
    source_name: SemiWiki
    url: https://semiwiki.com/
    usage_role: lead
    category: industry_lead
    purpose: 发现半导体制造、EDA、IP、Foundry、先进封装、产业链和企业路线线索。
    trust_level: lead_only
    evidence_policy: community_lead_only_v1
    enabled_for_demo: false
    crawling_policy: public_community_discovery_only

  - source_id: reddit_chipdesign
    source_name: Reddit r/chipdesign
    url: https://www.reddit.com/r/chipdesign/
    usage_role: learning
    category: technical_forum
    purpose: 学习芯片设计、模拟 IC、工艺、Datasheet 和设计工程实践，并提取待验证关键词。
    trust_level: community
    evidence_policy: community_lead_only_v1
    enabled_for_demo: false
    crawling_policy: public_community_discovery_only

  - source_id: eevblog_forum
    source_name: EEVblog Forum
    url: https://www.eevblog.com/forum/
    usage_role: learning
    category: technical_forum
    purpose: 学习实际器件使用、Datasheet、运放、电源、替代器件和工程参数讨论，并提取待验证关键词。
    trust_level: community
    evidence_policy: community_lead_only_v1
    enabled_for_demo: false
    crawling_policy: public_community_discovery_only
```

## 默认集合隔离

- 默认行业报告 evidence 集合：不包含本清单任何来源。
- 默认竞品报告 evidence 集合：不包含本清单任何来源。
- 社区搜索输出：只能进入独立的未验证线索队列，携带来源 URL 和发现时间用于内部追溯。
- 正式事件集合：只接收已经绑定 A/B 级官方或权威验证证据的事件。
- 报告生成：不得把社区热度、单个帖子、论坛经验或 Awesome 清单本身表述为已验证事实。

## Demo V0 与实现范围

本规划不创建 Adapter，不注册运行时 `SyncSource`，也不改变当前正式来源、检索、证据或报告
生成路径。以下能力明确不在本次范围内：

- Reddit 登录自动化或登录态抓取；
- 绕过 Reddit、Hacker News 或论坛反爬；
- 大规模历史抓取；
- 社区内容全文永久存储；
- 使用社区内容直接生成确定性结论。
