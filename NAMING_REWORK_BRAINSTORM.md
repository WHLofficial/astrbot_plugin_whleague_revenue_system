# 冠名权市场重构 · 定稿记录（v2.10.0）

> 状态：✅ **已实施并发布（v2.10.0）**。计划 v7 获批（「批准，注意语义化bump版本，依旧小步多commit」），
> 分五批落地：批次① b793e56（schema v10+20 配置键+迁移）→ ② 0495089（合同套餐层+续约+退约修复）→
> ③ 8642714（市场引擎四模式/档位/热度/TTL+玩家管理命令+推进挂载）→ ④ b1cd247（行情演化三信号+冠军加成+
> 冠名联动事件+LLM 品牌扩展）→ ⑤ 发布（CHANGELOG/metadata/本文定稿）。全量回归 232 例 0 失败。
> 本文档为最终设计记录；实现期仅两处口径细化：事件 satisfaction 效果不在触发时落合同（只出备注，由演化步
> 经 event_weight 并入，防与上座/战绩信号双重计账）；add_brand 档位缺省口碑（与「存量品牌置口碑」一致）。

## 1. 定位与核心模型（模型甲：品牌竞价、球队签约）

- 把「静态冠名合同」升级为「冠名权市场」：**品牌方（AI）主动向球队发报价，球队签约**。资金方向=品牌→球队（球队收冠名费），与冠名本质一致。
- **报价按队定制**：金额 = naming_fee(球场容量, 死忠数) × 品牌热度（复用现有 `formula.naming_fee`）。同一品牌给大球场多死忠队报得高、小队报得低——队况差异直接反映在报价上。
- 品牌按估值挑目标：头部品牌盯估值最高 1-2 队、新兴盯中游、口碑广撒；**弱队兜底**：口碑品牌优先向本轮零报价球队发报价。
- 标的物不变：球场冠名权，一队一冠名。不做球衣广告等泛赞助；事件引擎只加 2 条市场联动事件。
- **谈判时间线**：签约即成交即广播，合同当窗生效当窗收钱；报价 TTL 超时或下次推进（谁先到）即作废。

## 2. 四模式（market_bid_mode，默认 明标）

| 模式 | 语义 |
|---|---|
| **明标** | 定向报价 + 全员报价板可见（能攀比/观望） |
| **暗标** | 定向报价仅收价队可见；成交即广播揭晓；未成交静默作废（无开标仪式） |
| **先到先得** | 品牌按**平均队况**估值定公开一口价（稳健套餐结构），任意队先签先得、名额满即关——强队看不上（等定向报价），客群=中游队快签 |
| **常态** | 不走市场，/主场冠名 公式直签（=现状） |

## 3. 档位体系

- **头部**（每品牌独家 1 队）/ **新兴**（每品牌限量 market_emerging_slots=2 队）/ **口碑**（不限）。
- 头部品牌数配额 = ceil(队数 ÷ market_top_seat_ratio 默认 8)。
- 排名制自动校准：每次招商期开启，未锁档品牌按热度排名划档（前 K 头部 / 热度≥0.9 新兴 / 其余口碑）；`/主场品牌定档` 写 tier_locked 手动锁档覆盖。
- 迁移不猜配额：存量品牌 tier 一律置「口碑」，首次招商期自动校准到位。
- 三档性格 `market_tier_profiles`（满意度地板/上座好线/上座差线/解约赔率乘数/上座buff/死忠buff）：
  - 头部 {floor 0.7, good 0.95, bad 0.6, penalty_mult 1.5, attend_buff 0.02, fans_buff 0}「端着的大牌」
  - 新兴 {0.6, 0.9, 0.6, 1.0, 0, 0}「务实」
  - 口碑 {0.5, 0.85, 0.5, 0.75, 0, 0.005}「重感情本地伙伴」
- 上座 buff 挂窗口收入计算（随流水重算）；死忠 buff 挂 fans evolve（状态类 redo 跳过）。

## 4. 套餐

- ①稳健 windows×底价×0.85 ②进取 2窗×1.25 ③对赌 4窗×0.7保底 + 奖金=保底×0.7（窗口上座≥0.8 或 死忠增长≥0.03 发放）。
- 签约按 package_json **快照履行**（套餐/档位/性格参数定格，改档不影响在途合同）；legacy `'{}'` 走 `formula.naming_fee` 老公式。
- 行业模板 market_industry_templates（行业名→系数，默认全 1.0）乘金额；naming_bet_expected_prob 0.5 期望价值仅展示。
- 品牌 packages_json：LLM 套餐变体起草，管理员采纳后覆盖标准结构。

## 5. 报价生命周期与 TTL

- 签约即成交即广播（当窗生效当窗收钱）。
- **TTL** = market_offer_ttl_hours 默认 72（0=不限时，活到下次推进）：expire_at=created_at+TTL；**懒检查**——招商/签约/报价板任何读取路径都校验超时，超时拒绝签约显示已过期；下次推进硬收口。
- **签约事务化**（原子认领模式，防先到先得/独家名额并发双签）。
- **热度双向反馈**：报价被签约→品牌热度+0.02；品牌本轮报价全冷落→−0.03（**静默不广播**；冷落判定挂轮次收口）。

## 6. 行情：满意度与热度

- satisfaction 合同级，起点 1.0 钳 [0,2]；三信号：上座 / 战绩（按档位 good-bad 线 ±0.05 缓冲）/ 事件 effects。
- satisfaction < 档位 satisfy_floor → 品牌解约（无赔偿+广播）；**删 maybe_brand_terminate 死忠掷骰**；老合同同入体系（性格按实时档位，费用条款按快照）。
- heat 品牌级钳 [0.5,1.5]：夺冠+0.10 / 连胜+0.03 / 低迷−0.02 / 成交+0.02 / 报价全冷落−0.03（静默不广播）；赛季末冠军队品牌+满意度+热度（联赛积分算，缺 score 跳过）。
- 状态类 redo 跳过（与死忠演化同纪律），奖金/流水类重算；排名制档位随热度自然流动（锁档除外）。

## 7. 事件与 LLM

- event_engine：conditions_json 加 `has_naming`；effects_json 加 `satisfaction`/`brand_heat`；新增 2 条选择型内置事件「品牌联合营销」「竞品挖角」（复用冠名联动类目+event_choices）。
- llm_writer.design_brands 提示词升级（档位+行业+热度+pitch）；套餐变体→管理员采纳存 brand_pool.packages_json。
- 播报=程序模板+品牌 pitch 点缀，零每窗口 LLM 调用；「流拍=未达成合作」措辞废弃，**无落空播报（全部静默）**。

## 8. 数据模型（schema v10）

- brand_pool 加 5 列：`tier` TEXT DEFAULT '口碑'、`industry` TEXT DEFAULT '通用'、`pitch` TEXT DEFAULT ''、`packages_json` TEXT DEFAULT ''、`tier_locked` INTEGER DEFAULT 0。
  - builtin 行业映射：麒麟生物=医疗、阿迪达斯=运动、亚马逊=科技、可口可乐=饮食、海底捞=饮食、星海通讯=科技、CVS Health=医疗，其余'通用'。
- naming_rights 加 2 列：`package_json` TEXT DEFAULT '{}'（legacy）、`satisfaction` REAL DEFAULT 1.0。
- 新表 `market_rounds`(id, season_number, window_seq, bid_mode 快照, status open→settled, opened_by, opened_at, settled_at, UNIQUE(season_number, window_seq))。
- 新表 `market_offers`(id, round_id→market_rounds, brand_id→brand_pool, team_name, package_no, amount 每窗冠名费, windows, package_json 快照, status pending→accepted/expired/withdrawn, created_at, expire_at, UNIQUE(round_id, brand_id, team_name)；**先到先得挂价 team_name 存 ''**)+索引 (round_id, status)。

## 9. 配置键（defaults.py + _conf_schema.json）

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| market_bid_mode | string | 明标 | 明标/暗标/先到先得/常态（白名单校验） |
| market_top_seat_ratio | int | 8 | 头部品牌配额除数 |
| market_offers_per_brand | int | 3 | 每品牌单轮报价上限 |
| market_emerging_slots | int | 2 | 新兴档每品牌名额 |
| market_offer_ttl_hours | int | 72 | 报价时效（0=不限时） |
| naming_pkg_stable_windows / _factor | int/float | 6 / 0.85 | 稳健套餐 |
| naming_pkg_short_windows / _factor | int/float | 2 / 1.25 | 进取套餐 |
| naming_pkg_bet_windows / _factor | int/float | 4 / 0.7 | 对赌套餐保底 |
| naming_pkg_bet_bonus_rate | float | 0.7 | 对赌奖金=保底×率 |
| naming_pkg_bet_attend | float | 0.8 | 对赌上座线 |
| naming_pkg_bet_fans | float | 0.03 | 对赌死忠增长线 |
| naming_bet_expected_prob | float | 0.5 | 期望价值展示 |
| market_satisfy_floor | float | 0.6 | 全局满意度地板（档位 profile 缺省回退） |
| market_satisfy_config | JSON | 见下 | 三信号权重与缓冲 |
| market_heat_config | JSON | 见下 | 热度事件增量与钳制 |
| market_industry_templates | JSON | {} | 行业系数（缺省 1.0） |
| market_tier_profiles | JSON | 见下 | 三档性格 |

- market_satisfy_config 默认：`{"attend_weight":0.5,"result_weight":0.3,"event_weight":0.2,"line_buffer":0.05}`。
- market_heat_config 默认：`{"champion":0.10,"win_streak":0.03,"slump":-0.02,"deal":0.02,"ignored":-0.03,"clamp_low":0.5,"clamp_high":1.5}`。
- market_tier_profiles 默认：`{"头部":{"satisfy_floor":0.7,"good_attend":0.95,"bad_attend":0.6,"penalty_mult":1.5,"attend_buff":0.02,"fans_buff":0.0},"新兴":{"satisfy_floor":0.6,"good_attend":0.9,"bad_attend":0.6,"penalty_mult":1.0,"attend_buff":0.0,"fans_buff":0.0},"口碑":{"satisfy_floor":0.5,"good_attend":0.85,"bad_attend":0.5,"penalty_mult":0.75,"attend_buff":0.0,"fans_buff":0.005}}`。

## 10. 命令

- 玩家：`/主场招商`（本队报价+明标报价板/先到先得挂价）、`/主场签约<报价号>`（alias 签约）、`/主场续约[套餐号]`（alias 续约；剩 1 窗且 satisfaction≥1.0 → 当期新底价+所选套餐无缝直签）、`/主场退冠名` 不变。
- `/主场冠名`：bid_mode=常态 时现状直签（可选套餐号默认稳健）；其他模式拒绝并引导 /主场招商。
- 管理：`/主场品牌定档<品牌ID><头部/新兴/口碑>[行业]`（锁档）；品牌生成/列表/采纳/丢弃不变。
- `/主场推进`：自动 ①上轮收尾（未签约报价作废，TTL 到点即死+硬收口，静默）②档位校准 ③招商引擎发报价 ④播报。

## 11. 流程图

```
════════ 窗口循环（每轮 /主场推进）════════

管理员 /主场推进
   ├─① 上轮收尾：未签约报价作废（TTL 到点即死，推进时硬收口；静默）
   ├─② 自动校准档位：按热度排名划头部/新兴/口碑（锁档品牌跳过）
   ├─③ 招商引擎发报价：头部→估值最高1-2队 / 新兴→中游
   │   / 口碑→广撒，优先兜底本轮零报价球队；先到先得品牌→公开一口价挂出
   └─④ 播报：明标=报价送达通报（公开明细）；暗标=只报「招商期已开启」
       + 收价队私有提醒；先到先得=公开一口价挂价公告
   ▼
窗口期内玩家（任意时刻）
   ├─ /主场招商          本队收到的报价（品牌/档位/套餐/每窗金额/期限/期望价值/pitch）
   │                     明标另见全联赛报价板；先到先得见公开一口价
   ├─ /主场签约<报价号>  签约即成交 → 广播立刻发、快照生效、当窗结算收钱
   └─ /主场续约[套餐号]  合同剩1窗且品牌满意度≥1.0 → 新底价无缝直签
   ▼
窗口结算（随 /主场推进 自动触发）
   ├─ 收冠名费；对赌套餐判定奖金（上座≥0.8 或 死忠增长≥0.03）
   ├─ 满意度三信号演化（上座/战绩/事件）→ 跌破档位地板 → 品牌解约+广播
   ├─ 热度演化（成交+0.02 / 全冷落−0.03静默 / 夺冠·连胜·低迷·事件）
   └─ 合同剩1窗 → 提醒续约；窗口归零 → 自然到期

════════ 管理员侧 ════════

日常：只管 /主场推进，其余全自动（招商/结算/校准/播报）
可选干预：
  /主场品牌定档<品牌ID><档位>[行业]  手动锁档（退出自动排名）
  /主场品牌生成 → 采纳/丢弃          扩品牌池（提示词含档位/行业/热度/pitch）
  set_config                         切 bid_mode、调套餐系数/地板/配额比例/TTL 等

════════ 四模式差异 ════════
明标（默认）=定向报价+全员报价板   暗标=定向报价仅收价队可见，成交即公开
先到先得=公开一口价先签先得        常态=不走市场，/主场冠名 公式直签（=现状）
```

## 12. 实施批次（v2.10.0，批次 commit vX.Y.Z-n 风格，随批测试绿）

1. **批次①** schema v10（brand_pool 5 列 / naming_rights 2 列 / market_rounds / market_offers）+ 全部配置键 + 迁移测试。
2. **批次②** 合同套餐层（三套餐/签约快照/续约/确定性解约/性格 buff）+ s10 测试。
3. **批次③** 市场引擎 services/market_service.py（四模式/目标队选择与弱队兜底/TTL/签约事务化/热度反馈）+ s9 测试 + 命令注册。
4. **批次④** 行情演化 + 事件扩展（has_naming / satisfaction / brand_heat / 2 新内置事件）+ LLM 提示词 + s11 测试。
5. **批次⑤** 全量回归 + CHANGELOG + 本稿定稿收尾 + 发布 commit。

## 13. 决策记录（被否方案与关键拍板）

- **否**：球队竞价买冠名（与「冠名费=球队收入」的资金方向矛盾）。
- **否**：一品牌签多队不受限（改为头部稀缺度=方案三：按联赛规模自动配额 ceil(队数÷8)，排名制+锁档）。
- **否**：管理侧 /主场放盘/撤盘/截标（改全自动品牌发报价，管理日常只管推进）。
- **否**：报价按溢价率排序（期望价值仅展示列）。
- **否**：落空/流拍任何播报——用户拍板「不加，全部静默」，只走热度 −0.03 内部演化。
- **暗标成交即揭晓**（否「推进时统一开标」）；**先到先得=公开一口价**（否按队况计价——用户纠正：强队看不上一口价、等定向报价，客群=中游队）。
- **报价作废=TTL 72h**（否「下次推进才作废」——用户原话：「主场推进他妈都到下一个窗口了，等于联赛打了半程才谈完这合理吗」，对机制性等待/拖延高度敏感）。
- **命令命名**：/主场应价→/主场签约、/主场续冠→/主场续约；三档命名=头部/新兴/口碑（否钻石黄金/旗舰主流/顶流当红等全家桶）。
- **兼容红线**：active 老合同按老公式履行至期满；市场自下一窗口启用；bid_mode=常态 与现状一致；迁移自动执行可部署（服务器步骤随发布说明）。
