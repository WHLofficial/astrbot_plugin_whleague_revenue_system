# 效果键登记表（EVENT_KEYS）

随机事件 `effects` / 选择事件 `outcomes[].effects` 支持的效果键一览。单一事实来源是
`services/event_effects.py` 的 `EFFECT_SPECS` 注册表——**表中键序 = 注册表序 = 广播概率表
与结算备注的显示顺序**。LLM 设计与手写事件（`/主场事件写`）与内置事件同口径钳制。

设计背景见 `EVENT_INTERACTION_BRAINSTORM.md`；本文档随新键落地持续更新。

## 登记表（v2.11.4 时点）

| 键 | 量纲 | apply 落点 | 钳制（默认 / config 键） | 撤销语义 | describe 文案 | 消费者 |
|---|---|---|---|---|---|---|
| `money` | M，正=收入 | `club_balance` 流水（note=事件名） | ±8.0 / `event_money_clamp` | 删最新同名流水 + 重算余额 | `资金+2.0M` | 财务/报表 |
| `maintenance` | M，0~上限 | 流水（note=事件名（维护）） | 0~5.0 / `event_maintenance_clamp` | 删同名（维护）流水 | `维护−1.0M` | 窗口维护账 |
| `fans_pct` | 小数，±（0.03=3%） | `stadium.fans_diehards` ×(1+v)，钳 `fans_cap` | ±0.05 / `event_fans_clamp` | ÷(1+v) 回退，钳 0/上限记 warning | `死忠+3.0%` | 死忠→上座/收入/行情 |
| `attendance_mod` | 乘数（≠1 才落键） | `next_attendance_mod` ×v | [0.5, 2.0] 固定 | ÷v 反除；已随赛果消耗则跳过 | `上座×1.20` | `record_results` 消费后 reset |
| `satisfaction` | 小数，± | 不落库，仅出备注；窗口结算时并入品牌满意度信号 | ±0.5 / `event_satisfaction_clamp` | 无（未结算删事件即消失；已结算不回滚） | `品牌满意度+0.30` | `brand_service.evolve_sentiment`（event_weight） |
| `brand_heat` | 小数，± | `brand_pool.heat` +v，终钳 `market_heat_config` clamp_low/high | ±0.3 / `event_brand_heat_clamp` | 无（同上） | `品牌热度+0.20` | 品牌 heat → 报价/排名 |
| `build_credit` | M，± | `club_balance.build_credit` 列直改（沿建设券通道，不带余额流水） | ±5.0 / `event_build_credit_clamp` | 反向加减（可透支为负，`deduct_build_credit` 只扣不抵） | `建设券+3.0M` | 设施建造/扩建消耗 |
| `influence` | 点，±（基准值量纲 0~200） | `stadium.influence` 基准值直改，下钳 0 | ±10.0 / `event_influence_clamp` | 反向加减，触 0 下界记 warning | `影响力+5.0` | 死忠目标/上座等读 live 值自动跟随 |
| `signals` | dict，信号名→数值 | 不落库，仅出备注；窗口结算时按信号名分发给订阅者 | fan_mood ±2.0、upkeep/fee_mod [0.5,2.0] / `event_signal_config` | 无（同 satisfaction） | `粉丝情绪+0.3 / 冠名费×0.8` | 见下方信号总线 |
| `facility` | dict `{"key":设施键,"delta":±1}` | `stadium_facilities.level` 沿建造通道升降，钳 [0, 配置最高档] | delta 收敛 ±1；key 须在 `facility_effects` 域内 | 反向 delta 再钳边界；已被手动调整记 warning | `设施草皮+1级` | 商业/转播收入、维护费公式 |
| `booking_cancel` | 1~2（`event_booking_cancel_clamp`） | 删该队本窗档位最高已订档期；apply 时把取消明细 enrich 进 effects | [0, clamp] / `event_booking_cancel_clamp` | 按 enrich 明细逐条恢复占位（slot/activity/booked_by）；被重占记 warning | `档期取消×1` | 档期收入（本窗结算） |
| `booking_gift` | 活动类型串 | 赠本窗最低空档位（`booked_by=事件`）；满则落空 | type 须在 `activity_config` 域内 | 仅当该档仍为同类型且 `booked_by=事件` 才删；被替换记 warning | `赠档期:球迷开放日` | 档期结算（正常出收益/损毁） |
| `weather_set` | 天气串 | 该队下一场未赛主场 `matches.weather`；后动可再被 forecast 覆盖 | weather 须在 `weather_probabilities` 域内 | 三重校验后恢复原值（可能为 NULL）；已赛果/被覆盖记 warning | `天气→下雨` | 比赛收入/维护的天气系数 |
| `offer_spawn` | 1 | 未签约品牌按 heat 加权上门递价（稳健套餐、行业系数、挂当前招商轮、TTL=`market_offer_ttl_hours`） | truthy→1 | 无报价→已过期清理记 warning；非 pending→已签约不撤销；pending→删除 | `品牌上门` | 市场签约流程 |

补充语义：

- `satisfaction` / `brand_heat` 的 apply **仅在球队当前有生效冠名时产生效果/备注**
  （无冠名静默跳过）；钳制只限强度，是否生效由 apply 阶段判定。
- `influence` 只钳下界 0、不设上界（导入通道允许 1~1000，钳 200 会把存量高位基准
  反向拉低）；事件侧强度由 `event_influence_clamp` 限制。
- `build_credit` 不写余额流水（`apply_balance(0, v)` 直改列），财务流水不含建设券事件；
  取消事件反向加减后已花部分表现为负余额，`deduct_build_credit` 只扣不抵、不会透支建设。
- redo（强制重算）时状态类键跳过并备注「（重算跳过）」；资金/维护流水由 tx_ids 撤销重算。
- `cancel_team_event` 反演只处理资金类（`named_tx`）流水；品牌键从不像资金键那样回滚
  （历史行为，保持不变）。
- **信号总线（`signals`）**：值必须是 dict，已知信号 `fan_mood`（步进型，百分点，
  消费=死忠演化结果×(1+Σ/100)，随 evolve 在 redo 跳过）、`upkeep` / `fee_mod`
  （乘数型，值即乘数，消费=Π(事件值) 终钳后乘本队维护费/冠名费，首结与重算同源
  重应用）。未知名写入侧剔除、消费侧跳过；只读事件日志不落状态（`collect_window_signals`）。
- `_worst_option` 未定选项兜底只按 `money − maintenance` 净额比较——**非资金键不参与**，
  这是刻意保留的口径（净额概念只对资金键有意义）。
- **结构键的发现状态经 `ApplyContext.raw + enrich` 落盘**：apply 前不知道会取消哪条档期、
  改哪场天气、生成哪个报价——apply 时把发现结果（取消明细/原天气/offer_id）原位写回
  effects 源字典，引擎 apply 后才序列化落盘，取消反演由此取回精确状态。redo
  （apply_state=False）不落状态也不增补，重算后持久化的仍是普通值。
- **结构键落库时序**：`booking_gift` 走 `booked_by="事件"` 通道，档期结算照常出收益/
  草皮损毁；`weather_set` 先于 forecast 掷骰生效（同一窗口内后动者优先覆盖）；
  `offer_spawn` 复用市场稳健套餐口径（`naming_fee × industry_factor`、TTL 同
  `market_offer_ttl_hours`），只挂当前开放的招商轮（无轮次落空）。
- 结构键取值域（设施/活动/天气）在 llm_writer 与 add_custom 两处 clamps 里以
  `facility_keys` / `activity_keys` / `weather_keys` 注入，域外值在钳制层剔除。

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
