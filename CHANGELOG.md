# Changelog

本项目的所有重要变更都记录在此。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [1.1.2] - 2026-11-27

### 修复
- **抢占后其他插件完全收不到回复**（v1.1.0 引入的回归）

  抢占通过 `event.stop_event()` 中止请求，`builder.py` 随后 `return None`，
  agent runner 根本不创建 —— 于是 `on_llm_response` / `on_agent_done`
  一个都不会触发。记忆、陪伴、贴纸等依赖该钩子的插件这一轮什么都拿不到。

  修复：抢占产出内容后，主动补两件事

  1. **合成派发** `call_event_hook(OnLLMResponseEvent, resp)`
     - 用子代理内容构造 `LLMResponse`
     - 打 `_failover_synthetic` 标记，自身钩子见到即返回，避免递归
  2. **补写会话历史**
     - runner 没跑，assistant 半轮从未入库
     - 否则下一轮会看到悬空的 user 消息，上下文断裂

### 测试
- `test_preempt` 增至 19 项：验证合成派发携带正确内容、防重入标记、
  历史写入且最后一条是子代理内容

## [1.1.1] - 2026-11-27

### 修复
- **`/n` 后第二轮就切回主模型**

  事后覆盖路径（`on_llm_response`）里，`by_marker` 的判断错误地依赖
  `verdict.matched_by == "request-intent"`：

  ```python
  by_marker = verdict.matched_by == "request-intent" and "显式标记" in verdict.reason
  ```

  但主模型若真的返回了 `{"status":"refused"}`，`_judge_refusal` 会先返回
  `matched_by="marker"`，`refused=True`，于是后面
  `if intent and not verdict.refused` 不成立，`verdict` 保持 marker 来源，
  `by_marker` 变成 `False`。

  结果：`/n` 被当作「自动拒绝」进入 sticky（`release_enabled=True`），
  连续 3 轮正常后自动切回。

  修复：`by_marker` 直接从 intent 的 hits 判断，不依赖 verdict 来源。

### 测试
- 新增回归用例：主模型确实拒绝 + 用户带 `/n`，验证仍判为标记触发，
  4 轮后仍在 sticky。`test_marker_sticky` 增至 21 项

## [1.1.0] - 2026-11-27

### 变更
- **请求阶段抢占，主模型完全不参与**（架构性改进）

  此前所有转交都是「事后补救」：主模型照常被调用、照常拒绝，插件在
  `on_llm_response` 里篡改结果。这带来三个问题：

  | 问题 | 说明 |
  |---|---|
  | 双倍成本 | 每轮主模型 + 子代理各跑一次 |
  | 延迟翻倍 | 要等主模型拒绝完才轮到子代理 |
  | 其他插件看到拒绝 | 拒绝是真实发生过的，记忆/陪伴插件存档时拿到的是原文 |

  第三点正是用户反馈的现象：聊天框正常，但其他插件的对话记录里是主模型的拒绝。

  修复：在 `on_llm_request` 阶段判定要走子代理时，
  - 直接调用 ALT 产出内容
  - `event.set_result()` 交付
  - `event.stop_event()` 中止 —— `call_event_hook` 返回 True 后
    `builder.py` 会 `return None`，主模型请求根本不发出

  新增配置 `preempt_on_intent`（默认开）。

- 抢占路径补齐 sticky 的进入与解除判定（此前只在事后路径实现）

### 测试
- 新增 `test_preempt.py`（13 项），验证主模型未被调用、事件已停止、
  子代理失败时优雅回退
- 全套 268 项通过

## [1.0.2] - 2026-11-27

### 修复
- **钩子优先级设为 100，确保先于记忆类插件改写回复**

  用户装了多个记忆/陪伴类插件（memory_companion、memory_beyond、
  memory_diary、fish_heart），它们同样注册 `on_llm_response` 并存档回复。

  AstrBot 的 `StarHandlerRegistry` 排序规则是
  `sort(key=lambda h: -h.extras_configs["priority"])`，默认全为 0，
  于是按插件加载顺序执行。若记忆插件排在前面，它们存档的永远是
  主模型的拒绝原文，之后插件再改写 `completion_text` 也追不回来。

  修复：两个钩子均设 `priority=100`。
  `on_llm_request` 同步提高，保证连续性注入先于其他插件处理 system_prompt。

  注：priority 是装饰器字面量，需在 import 期确定，因此不做成配置项。

## [1.0.1] - 2026-11-27

### 修复
- **`/n` 标记在中文后紧邻时无法识别**

  真实日志用例：`宝宝 想再做一次吗/n` —— 「吗」与 `/n` 之间没有空格。
  原正则要求标记前必须是空白或行首（`(?:^|\s)`），中文用户习惯不敲空格，
  导致标记被静默忽略，退回词表判定，进入的是「自动拒绝」sticky
  （`release_enabled=True`），3 轮后自动放手 —— 用户观察到「子代理说了一句就
  回到主模型拒绝」。

  修复：标记前导字符扩展为空白、标点、任意 CJK 字符或字母数字，
  同时保留行首独立标记 `/n` 的识别；剥离时不再误删前一个字符。

- 测试补充真实日志用例，`test_marker` 增至 28 项

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
