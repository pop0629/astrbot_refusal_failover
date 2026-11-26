# Changelog

本项目的所有重要变更都记录在此。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [1.0.0] - 2026-11-27

首个稳定版。版本号变更，代码与前版一致。

### 里程碑
- 转交链路完整验证通过：主模型拒绝 → 备用提供商接管 → 历史同步 → 用户无感知
- 250 项测试全部通过

---

## [0.10.0] - 2026-11-27

### 修复
- **转交后同步修正会话历史**（关键修复）

  AstrBot 的 `ToolLoopAgentRunner._complete_with_assistant_response` 执行顺序为：
  `_append_context_messages(record_response=True)` 先于 `agent_hooks.on_llm_response()`。
  也就是说主模型的原始回复在插件钩子执行之前就已写入会话历史，
  插件改 `completion_text` 只影响展示层，历史里仍是主模型的拒绝内容。

  症状：后台日志看到主模型回复、其他插件读到拒绝内容、下一轮模型错误地记得自己在拒绝。

  修复方式：转交成功后回写历史 ——
  优先就地修改 `run_context.messages` 里的 text part（按能力匹配，不按类名），
  回退到 `conversation_manager.update_conversation` 覆盖存储。

- 新增配置 `sync_history`（默认开，仅供对比复现时关闭）

---

## [0.9.0] - 2026-11-26

### 变更
- **`/n` 触发后持续接管**，解除规则与手动切换一致

  原行为：`/n` 进入 sticky 后仍受「连续 3 轮正常」约束，子代理答得好反而自动放手。

  现行为：`StickyState` 新增 `release_enabled` 字段
  - `/n` 触发 → `release_enabled=False`，连续正常不解除
  - 自动拒绝触发 → `release_enabled=True`，行为不变
  - 两者共同受 `/fm` 手动解除 + 硬上限约束
  - `/n` 使用独立的 `marker_sticky_max_turns`（默认 50）

### 测试
- 新增 `test_marker_sticky.py`（19 项），覆盖状态机与集成两层

---

## [0.8.0] - 2026-11-26

### 新增
- **显式标记 `/n` / `/nsfw`**

  词表对含蓄的亲密表达天然无能（「宝宝 我想要你」这类加词必然误伤正常对话），
  改为让用户直接声明。

  - 行尾 `/n` 或 `/nsfw` 强制本轮转交，优先级高于所有自动判定
  - 自动从消息中剥离，模型看不到标记
  - 只认行尾独立标记：`/n/abc`、`/normal`、句中 `/n` 均不误触
  - 新增 `marker.py` 与配置 `marker_enabled`

### 测试
- 新增 `test_marker.py`（23 项）

---

## [0.7.0] - 2026-11-26

### 新增
- **请求侧意图预判**

  主模型遇到成人内容请求常改话题而不明确拒绝（「好的，那就接着写第三章的对峙」），
  回应侧判定抓不到。改为在请求阶段判断用户要什么。

  - 新增 `request_intent.py`：敏感词评分 + 生成动词要求 + 询问式排除
  - `on_llm_request` 阶段预判，命中则标记，回应阶段直接转交
  - 区分「要求生成」与「讨论怎么写」：后者 0.20 分不转交
  - 配置：`request_intent_check/high/low/llm_judge/judge_provider_id`、`request_sensitive_patterns`

### 测试
- 新增 `test_request_intent.py`（36 项）与 `test_request_intent_integration.py`（10 项）

---

## [0.6.1] - 2026-11-26

### 变更
- **扳机 C 默认完全关闭**：`sticky_judge_every` 默认从 3 改为 0，代码内后备默认值同步

  原因：启用 C 会周期性插入判定请求，打断主对话的 prompt cache 前缀，
  缓存命中率下降可能让总成本反升。扳机 A 已能解掉绝大多数会话。

### 测试
- 新增 2 项验证 C 关闭时 10 轮内不触发

---

## [0.6.0] - 2026-11-26

### 新增
- **sticky 保留模式**：自动转交命中后，后续几轮继续由子代理接管

  三个解除扳机，按成本排序：
  | 扳机 | 成本 | 条件 |
  |---|---|---|
  | A 连续正常 | 0 | 连续 N 轮无拒绝迹象 |
  | B 手动 | 0 | `/fm` |
  | C 抽样判定 | 每 N 轮一次 | 便宜模型判定（默认关） |
  | 硬上限 | 0 | 超过 `sticky_max_turns` |

  安全兜底：sticky 期间子代理不可用时立即自动释放。

- 新增 `sticky.py` 状态机（纯逻辑，可独立测试）

### 测试
- 新增 `test_sticky.py`（31 项）与 `test_sticky_integration.py`（15 项）

---

## [0.5.0] - 2026-11-26

### 新增
- **手动切换注入与自动转交一致的连续性脚手架**

  `/fa` 走 AstrBot 原生 `set_provider`，不经过插件，切过去后风格样本、
  记忆卡、接缝提示全部丢失，用户能感知到语气断层。

  新增 `on_llm_request` 钩子：会话处于 alt 状态时注入
  - 主模型真实文风样本（最高优先级）
  - 风格要求
  - 记忆卡（含未闭合线索）
  - 连续性要求 + 不要提及切换

- 配置 `manual_switch_continuity`

### 测试
- 新增 `test_continuity.py`（11 项）

---

## [0.4.0] - 2026-11-26

### 新增
- **手动切换命令** `/fa`（切到子代理）、`/fm`（切回主模型）、`/fs`（查看状态）

  走 AstrBot 原生会话级覆盖 `provider_manager.set_provider`，只影响当前会话。
  切换时在 KV 记录原 provider 作为切回目标，处理了重复切换、未配置、
  provider 已删除、无记录等边界。

### 测试
- 新增 `test_switch.py`（12 项）

---

## [0.3.0] - 2026-11-26

### 新增
- **推迟语 + 请求关联度判据**

  有的软拒绝整段没有一个否定词，而是用撒娇/寒暄/推迟替换掉请求。
  单纯把推迟语加进词表会误伤「这个功能先放一放，明天再改」，
  所以判据改为 `推迟语 × 请求关联度`。

  - 新增 `_DEFERRAL_CUES` 推迟语组，从强线索中移出「先放一放」
  - 新增 `request_overlap()`：字符 bigram 重叠率（中文整词匹配不可用）
  - 推迟语权重 `= 0.95 × (1 - overlap/0.22)²`，overlap ≥ 0.22 归零
  - 配置 `refusal_context_check`

### 测试
- `test_soft_refusal` 扩充至 46 项

---

## [0.2.0] - 2026-11-26

### 新增
- **三层拒绝判定**，大幅提升软拒绝识别

  主模型的拒绝大多不明确（帮不上忙/换个话题/跳过吧），单纯正则抓不住，
  放宽正则又会误伤正常技术回答。

  - Tier 0 结构化标记，命中即转交
  - Tier 1 软拒绝评分：强/中/弱三级线索 × 位置 × 实质内容惩罚
  - Tier 2 灰区（0.45~0.85）交 LLM 二次判定 REFUSED/OK
  - 新增 `soft_refusal.py`
  - 配置：`soft_refusal_check/high/low/llm_judge`、`judge_provider_id`

### 测试
- 新增 `test_soft_refusal.py`、`test_judge_integration.py`

---

## [0.1.0] - 2026-11-26

### 新增
- **首个版本**：主模型拒绝时自动转交备用子代理，结果无缝写回同一对话

  - 结构化拒绝判定（marker / regex / 混合）
  - 文风样本：从主模型真实前文抽取，供子代理模仿
  - 风格漂移检测 + 定向重写（相似度归一化，偏离点归因）
  - 共享记忆卡 + 未闭合线索，保证跨模型上下文连续
  - 接缝标记与四条连续性硬约束
  - 审计标记 `produced_by: alt`
  - `failover.py` / `style_check.py` 零依赖实现
  - 提供商配置改为原生 `select_provider` 下拉
  - `style_brief` 默认值改为只做禁令，真实前文样本优先级更高

### 测试
- `test_engine.py`、`test_style.py`、`test_integration.py`
