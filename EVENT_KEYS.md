# 效果键登记表（EVENT_KEYS）

随机事件 `effects` / 选择事件 `outcomes[].effects` 支持的效果键一览。单一事实来源是
`services/event_effects.py` 的 `EFFECT_SPECS` 注册表——**表中键序 = 注册表序 = 广播概率表
与结算备注的显示顺序**。LLM 设计与手写事件（`/主场事件写`）与内置事件同口径钳制。

设计背景见 `EVENT_INTERACTION_BRAINSTORM.md`；本文档随新键落地持续更新。

## 登记表（v2.11.1 时点）

| 键 | 量纲 | apply 落点 | 钳制（默认 / config 键） | 撤销语义 | describe 文案 | 消费者 |
|---|---|---|---|---|---|---|
| `money` | M，正=收入 | `club_balance` 流水（note=事件名） | ±8.0 / `event_money_clamp` | 删最新同名流水 + 重算余额 | `资金+2.0M` | 财务/报表 |
| `maintenance` | M，0~上限 | 流水（note=事件名（维护）） | 0~5.0 / `event_maintenance_clamp` | 删同名（维护）流水 | `维护−1.0M` | 窗口维护账 |
| `fans_pct` | 小数，±（0.03=3%） | `stadium.fans_diehards` ×(1+v)，钳 `fans_cap` | ±0.05 / `event_fans_clamp` | ÷(1+v) 回退，钳 0/上限记 warning | `死忠+3.0%` | 死忠→上座/收入/行情 |
| `attendance_mod` | 乘数（≠1 才落键） | `next_attendance_mod` ×v | [0.5, 2.0] 固定 | ÷v 反除；已随赛果消耗则跳过 | `上座×1.20` | `record_results` 消费后 reset |
| `satisfaction` | 小数，± | 不落库，仅出备注；窗口结算时并入品牌满意度信号 | ±0.5 / `event_satisfaction_clamp` | 无（未结算删事件即消失；已结算不回滚） | `品牌满意度+0.30` | `brand_service.evolve_sentiment`（event_weight） |
| `brand_heat` | 小数，± | `brand_pool.heat` +v，终钳 `market_heat_config` clamp_low/high | ±0.3 / `event_brand_heat_clamp` | 无（同上） | `品牌热度+0.20` | 品牌 heat → 报价/排名 |

补充语义：

- `satisfaction` / `brand_heat` 的 apply **仅在球队当前有生效冠名时产生效果/备注**
  （无冠名静默跳过）；钳制只限强度，是否生效由 apply 阶段判定。
- redo（强制重算）时状态类键跳过并备注「（重算跳过）」；资金/维护流水由 tx_ids 撤销重算。
- `cancel_team_event` 反演只处理资金类（`named_tx`）流水；品牌键从不像资金键那样回滚
  （历史行为，保持不变）。
- `_worst_option` 未定选项兜底只按 `money − maintenance` 净额比较——**非资金键不参与**，
  这是刻意保留的口径（净额概念只对资金键有意义）。

## 新键接入指南（注册表化后共 4 处）

1. **`services/event_effects.py`**：实现 apply/undo/describe/clamp 四函数，向
   `EFFECT_SPECS` 追加 `EffectSpec`。可选字段：`default`（缺键值，资金类 0.0、
   attendance_mod 类 1.0）、`coerce`（默认 float）、`named_tx`（资金流水类才置 True）。
   插入位置决定概率表显示顺序。clamp 返回 `None` 表示剔除该键（如 attendance_mod=1.0）。
2. **config**：`_conf_schema.json` 增键（description/hint/type/default）+
   `config/defaults.py` 增合法范围；钳幅接线在 `llm_writer.design_events` 与
   `event_engine.add_custom` 两处的 `clamps` dict（键名与 Spec.clamp 读取的一致）。
3. **prompt**：`llm_writer.design_events` 的数值边界行与 effects 字段说明增补
   新键语义与量纲（LLM 键权限全部开放的口径）。
4. **测试**：`tests/s5_events_brands.py` 增钳制边界用例与 apply/undo 行为用例；
   若涉及结算/行情联动，另在 s4/s16 增消费者侧用例。

帮助文本同步：`handlers/admin.py` `add_custom_event` 用法行的效果键清单。
