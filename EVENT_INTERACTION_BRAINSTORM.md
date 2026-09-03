# 事件×插件功能交互接口设计（定稿）

- 日期：2026-09-03
- 状态：定稿（四节设计已逐节确认，实施计划已批准）
- 目标版本：v2.11.0 – v2.11.5
- 前置发现：钳制层 `_clamp_effects`/`_clamp_choice_event` 只保留 money/fans_pct/maintenance（attendance_mod 条件保留），`satisfaction`/`brand_heat` 对 LLM 草稿与手写事件被**静默丢弃**——本期无论如何修复。

## 背景与目标

随机事件系统的结果目前通过 6 个效果键与其他功能交互，分两类范式：

- **状态型键**（即时改状态、可撤销）：`money`/`maintenance`（资金流水）、`fans_pct`（死忠）、`attendance_mod`（下一场上座修正）、`brand_heat`（品牌热度直改，钳 market_heat_config 的 clamp_low/high）
- **信号型键**（只落 effect_json/outcome、窗口结算时被其他系统按权重消费）：`satisfaction` → 品牌行情演化 `brand_service.evolve_sentiment`（与上座/战绩信号加权混合，权重 `market_satisfy_config`，负 delta × penalty_mult，跌破 satisfy_floor 自动解约）

反向已有条件门控 5 键（`min_tier`/`max_tier`/`min_capacity`/`requires_naming`/`requires_activity`，`_condition_ok`）。事件碰不到的状态：设施等级、天气、赛果、影响力、市场报价、活动档期、建造信用。无延迟效果、无 buff、无事件链。

## 已确认范围

- ① **扩状态键**：7 个全做（含 weather_set，用户明确要求排除项也纳入）
- ② **信号总线**：satisfaction 先例泛化
- ③ **反向**：条件键扩展 + 动态权重（13 赛后触发钩子不做，留未来）
- ⑥ **基础修复**：钳制放行 + 接入清单文档
- **LLM 键权限：全部开放**——所有新键经钳制后对 LLM 设计/手写事件与内置一视同仁（覆盖「LLM 只管文案」的保守读法，延续现有钳制实践）
- ④ 时间维度（buff/事件链/延迟）、⑤ 跨队/全局事件：**留待未来**，见文末

## §1 提交切片

| 提交 | 内容 | 依赖 |
|---|---|---|
| v2.11.0 | 效果键注册表重构（行为中性，现有测试断言零改动） | — |
| v2.11.1 | 基础修复：钳制放行 satisfaction/brand_heat + prompt 增补 + EVENT_KEYS.md | 0 |
| v2.11.2 | 数值类状态键：build_credit、influence | 0,1 |
| v2.11.3 | 信号总线 signals + 四订阅者 | 0 |
| v2.11.4 | 结构类状态键：facility、booking_cancel/gift、weather_set、offer_spawn | 0,1 |
| v2.11.5 | 反向：条件键扩展 + 动态权重 + schema v12 | 独立 |

## §2 效果键注册表（核心架构）

每个效果键一个 Spec：

```python
@dataclass(frozen=True)
class EffectSpec:
    key: str
    def apply(self, ctx, value): ...   # 落账/落状态 + notes
    def undo(self, ctx, value): ...    # cancel_team_event 反演（幂等）
    def describe(self, value): str     # 概率表/结算备注文案（同口径）
    def clamp(self, value, cfg): ...   # LLM/手写钳制边界
```

`EffectContext`：dao、cfg、team、season、window_seq、label、apply_state、notes 列表、tx_sink。新模块 `services/event_effects.py`（event_engine 与 llm_writer 共同 import，避免循环依赖）。

四处消费方改为遍历注册表：`_apply_effects`、`_outcome_desc` 概率表、`cancel_team_event` 撤销反演、`_clamp_effects`/`_clamp_choice_event` 钳制。老 6 键全部迁入，行为逐位不变。

**不进注册表的保留项**：`_worst_option` 兜底仍只按 money−maintenance 净额比较（非资金键不参与，文档注明）。

## §3 七个新键逐键语义

统一约定：钳幅全部 config 可调；redo（强制重算）时状态类跳过并备注「（重算跳过）」；notes 与概率表同口径。

| 键 | effects 示例 | 落点 | 钳制默认 | 撤销 |
|---|---|---|---|---|
| `build_credit` | `{"build_credit": 3.0}` | club_balance 建设信用（沿 import_facility 现有记账通道） | ±5.0M | 反向加减 |
| `influence` | `{"influence": 5.0}` | stadium.influence 直接改基准值 | ±10.0 | 反向加减 |
| `facility` | `{"facility": {"key": "training", "delta": -1}}` | stadium_facilities.level，clamp [0, 配置最高档] | delta 仅 ±1，key 必须在设施配置 | 反向 delta |
| `booking_cancel` | `{"booking_cancel": 1}` | 取消本窗已订活动中档位最高的一条 | ≤2 | 恢复原档位 |
| `booking_gift` | `{"booking_gift": "fan_meet"}` | 赠一档：填本窗最低空档位，type 必须 activity 配置合法 | 每事件 1 条 | 删档 |
| `weather_set` | `{"weather_set": "rain"}` | 该队下一场未赛主场的 matches.weather | 值 ∈ weather 配置档位 | 恢复原天气（已录赛果跳过） |
| `offer_spawn` | `{"offer_spawn": 1}` | brand_pool 未签约品牌按 heat 加权抽 1，复用市场 _quote 定价生成 market_offers 报价（TTL 72h），挂当前 open 轮 | 每事件 1 条 | 删报价行（已签约跳过） |
| `fee_mod` | 信号形态（§4） | 下一窗冠名费临时乘数 | 连乘后 [0.5, 2.0] | 删事件即失效 |

要点：facility 的 describe 把配置 key 映射中文显示名；booking 拆两键（LLM 钳制与文案更简单）；influence 用绝对值（0–200 裸值口径）；offer_spawn 是「事件摆摊招商」，无轮次则备注跳过。

## §4 信号总线

- effects 新键 `signals: {"fan_mood": 0.3, "fee_mod": 0.8}`（信号名→数值；步进型求和、乘数型连乘，由消费者声明）。存储同 satisfaction（effect_json/outcome，不落状态）。
- 窗口结算末段（现 evolve_sentiment 位置）新增「信号消费」步：从 `get_window_events` + `get_window_choice_outcomes` 汇总，按 config `event_signal_config` 订阅表分发。

| 信号 | 消费者 | 语义 | 钳制 |
|---|---|---|---|
| `satisfaction` | 品牌行情演化（迁移为订阅者，行为不变） | event_weight 权重沿用 | 沿用现有 |
| `fan_mood` | 死忠演化 FansService.evolve | 额外步进项并入非对称演化 | ±2.0%（config） |
| `upkeep` | 窗口维护费 | Σ 连乘后乘到 tier_maintenance | [0.5, 2.0] |
| `fee_mod` | 冠名费 tick | Σ 连乘后乘到当窗冠名费 | [0.5, 2.0] |

- **刻意不接上座模型**：attendance_mod 状态键已覆盖一次性上座效果，信号再走一遍即重复记账。
- redo 跳过（同 evolve_sentiment 现状）；撤销=删事件行（未结算时信号消失；已结算不回滚，与 satisfaction 现状一致）。
- 玩家可见性：概率表显示信号中文名（config 映射），如「粉丝情绪 +0.3 / 冠名费 ×0.8」。

## §5 反向条件与触发

- **条件键扩展**（`_condition_ok` 新增 5 键）：`min_fans`/`max_fans`、`min_balance`/`max_balance`、`facility_min`（值支持 `{"training": 2}` 结构化字典）、`weather_is`（下一场未赛主场天气；无未赛主场不满足）、`last_result`（win/draw/loss，最近一场已完赛主场）。可造「暴雨天/连败/设施老化/穷疯了」事件池。
- **动态权重**：event_pool 新列 `soft_conditions`（schema v12，默认 0 硬门控）。置 1 时条件不满足按 weight × `event_soft_condition_factor`（默认 0.25）衰减而非剔除；`trigger_team` 指定事件不衰减。
- 钳制白名单与 LLM prompt 同步。

## §6 基础修复与文档

- 钳制层放行 `satisfaction`（±0.5）/`brand_heat`（±0.3）及全部新键。
- `llm_writer.design_events` prompt 增新键/信号语义与量纲（全部开放口径）；`/主场事件写` 帮助文本同步。
- 新文档 `EVENT_KEYS.md`（repo 根）：效果键登记表（键名/量纲/钳幅/撤销语义/消费者）+ 新键接入指南（注册表化后 10 处 → 4 处：Spec + config 钳幅 + prompt 语义 + 测试）。

## 未来工作（本期不做）

- 13 赛后触发钩子：record_results 后对主队按概率触发「赛后事件」（绝杀庆典/惨败冲场）——事件体系第一个窗口期外触发源。
- ④ 时间维度：持续 buff（effect 带 span 多窗生效）、事件链（outcome 带 follow_up 下窗强制触发）、延迟效果（delay_windows）。
- ⑤ 跨队/全局事件：联赛级全局广播、德比周跨队联动。

## 决策记录

1. **注册表全迁移而非混合**：老 6 键一并入表，单一代码路径；重构提交以全量测试零断言改动为行为中性证明。
2. **全部开放 LLM**：用户拍板，覆盖保守读法；安全性靠钳制（facility ±1、weather_set 限档位、offer_spawn 限 1 条等）。
3. **influence 直改基准值**：不另立修正列——修正列多一处合成逻辑，YAGNI；消费端读 live 值自动跟随。
4. **booking 拆 cancel/gift 两键**：布尔/枚举比嵌套参数对 LLM 钳制与概率表文案友好得多。
5. **fee_mod 走信号不走状态键**：本质是结算时消费的一次性乘数，信号总线的免费乘客。
6. **上座模型不接信号**：与 attendance_mod 重复记账。
7. **weather_set 后动优先**：事件改写后 forecast 掷骰可再覆盖；撤销时已录赛果跳过（同 attendance_mod 已消耗模式）。
8. **_worst_option 不扩展**：净额概念只对资金键有意义，非资金键不参与未定选项兜底比较。
