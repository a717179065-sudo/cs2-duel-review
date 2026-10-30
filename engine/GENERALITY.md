# 通用性审计：哪些地方只对 BiuBiuZ / 已入库的 4 场成立

> 本文写于整理成仓库之前：文中 `pro/data/`、`pro/analysis/` 现在是 `bench/data/`、`bench/`；`pro/cache`、`pro/hltv`、`report/cache` 是本地放 demo 的目录，不随仓库发布。

> **2026-10-29 已处理（integrate-any-demo）**：
> - F1–F9：新增 base 阶段（读取比赛数据，facts/base.py），这一场的回合/交火/sit/位置全部读 work/<key>/base/；"目前只支持已经入库的 demo"删掉。
> - F12–F14：match_no 只在旧多场报告里查得到时才填（facts.match_no），否则页面写 demo id。
> - B1/B2/B3/B6：shooting、economy 对标去掉本场和用户本人；team.json 加 wp_by_demo，运行时减掉本场；demo_id 也扫 pro/cache。
>   B4（prefire 聚类）、B5（静态数）没法按场拆，保留为静态参考。
> - U1–U3：用户其他场 = 静态库（groups.csv 所有组）里 pro/data/duels_<d>.csv 有这个 steamid 的其他场（facts.other_reviews，去掉本场），
>   没有就不写 user_other。2026-10-29 fix：原来扫 work/，shooting.json 会随复盘过哪些场变化，改回下面 U1 表里建议的静态做法。
> - F12：match_no 改成 config.LEGACY_MATCH_NO 静态表（默认用户的 4 场），不再读 report/duels_de_mirage_*.csv。
> - M1–M4/M6/M8：config.half_of/half_end/half_spans + common.half（thresholds/common.json half_rounds/ot_half_rounds）；
>   economy 连败计数/钱重置按半场（含加时每半场）；timing._halves 按赛制；by_side 只算常规回合，加时另记 overtime。M5 改成 HR 推出（值不变）。
> - S1–S3：页面按开局阵营排；计分板名单取所有回合在场的人；用户某回合不在场时阵营按队友推（dc2a00e0 第 21 回合实测）。
> - N1–N5：输出名 key = demo id（默认用户）或 <id>_<steamid>；summary/build 用 killer_is_user/victim_is_user；窗口默认用户从 config 取、
>   用户不在 demo 里时点名单换人重排（/api/rerun），历史按 steamid 过滤。merge 的 death_of 仍是用户名（CONTRACT 定义，同一来源）。
> 下面是审计原文（行号是审计时的）。

范围：engine/（facts、rules、compose、merge、summary、validate、clips、build、common、thresholds、pipeline、config、ctx）和 app/。
审计时只读，没有改引擎代码。行号是 2026-10-29 审计时的。
另外：审计期间 `engine/facts/base.py` 已经在写（另一个任务），它的输出 `work/<id>/base/{rounds,positions,kills,util,duels,sit_econ,sit_hurt,sit,user_duels}.csv` 就是下面"改读 base"的目标文件名。

严重程度：**A** = 新 demo 或换用户直接报错 / 结果错；**B** = 能跑但数字或文字不对（对标混进自己、加时算错）；**C** = 显示、体验、稳健性。

---

## 0. 结论先说

1. 新 demo 卡在 extract 的原因有三个，**都在 facts 层**：`rounds_csv`（pro/data/rounds_<id>.csv）、`user_duels`（report/duels_de_mirage_*.csv 按"场次"匹配）、`sit_rounds.csv`。cache 也读 `rounds_csv`。
2. **11 场 FACEIT 测试 demo 本身就在对标库里**（groups.csv 的 high 组就是 pro/cache 这 11 个 id）。它们有 rounds_/duels_ 底稿，所以 `rounds_csv` 能过，但 `user_duels_source` 会报"比赛库里还没有玩家……"。改完以后它们能跑，但 shooting 的 high 对标、economy 的 high 对标、team.json 的人数胜率表**都含这一场自己**，必须排除。
3. 高分玩家04（90000000000000111）在 11 场 high 全部出现，还在 3 场 pro 里；复盘他时 high 对标基本就是他自己。对标除了排除本场，还要排除用户本人那几行（逐人数据的地方）。
4. 加时：rules（eco.py、post.py）已经按 thresholds 的 half_rounds/ot_half_rounds 处理，但 **facts/economy.py 写死 12/13**、**rules/timing.py 按阵营变化分半场**，bd125285（29 回合，25–27 CT 和下半场同边）都会算错。
5. work/<id> 和 report/<id>.html 只按 demo id 区分，**同一个 demo 换个用户会覆盖**上一个用户的页面和历史记录。

---

## 1. facts 层：必须改读 work/<id>/base/*.csv 的地方

| # | 文件:行 | 现在读 | 改读 | 说明 |
|---|---|---|---|---|
| F1 | facts/__init__.py:29-35 `rounds_csv` | pro/data/rounds_<id>.csv，没有就 FactsError"目前只支持已经入库的 demo" | base/rounds.csv | 改成返回 `work/<id>/base/rounds.csv`；没有就说"底稿阶段没跑"。 |
| F2 | facts/extract.py:145 | `rounds_csv(ctx)` | base/rounds.csv | 用到 round/start_tick/end_tick/dur_s/winner/reason。148-149 那条一致性检查的提示文字里的 `rounds_{DEMO}.csv` 也改。 |
| F3 | facts/economy.py:115 | `rounds_csv(ctx)` | base/rounds.csv | 用到 plant_tick。 |
| F4 | facts/cache.py:24、218（build_grid） | `rounds_csv(ctx)` | base/rounds.csv | grid.parquet 的取样区间。 |
| F5 | facts/__init__.py:38-64 `user_duels_source` / `user_duels` | pro/data/duels_<id>.csv 取用户击杀 tick 集合，再到 report/duels_de_mirage_*.csv 里按"场次"找 tick 集合完全一样的那一组 | base/user_duels.csv（整份就是本场、本用户） | 整个"场次匹配"删掉。返回值不再带场次（见 F12）。用户在 demo 里但一次交火都没有时返回空表，不报错。 |
| F6 | facts/extract.py:28、283-287 | `user_duels(ctx)` → `DU, MATCH_NO` | base/user_duels.csv | 列：回合、tick、开打tick、开局第几秒、结果、敌人、敌人ID、我/敌报点、我/敌 X Y、武器、爆头、局势、对枪方式、交火类型、看见敌人时准星偏差°……、首杀对枪、标准位、凶手位置、原因、责任、责任说明、经济、对方经济（630-661 全部按列名取，缺列会 KeyError，base 必须给齐这些列，pro/data/duels_<id>.csv 就缺 首杀对枪/标准位/凶手位置/责任/责任说明/经济/对方经济）。 |
| F7 | facts/extract.py:447-448 | pro/analysis/sit_rounds.csv 里 `demo == id` 的行 | base/sit.csv | 用到 situation/sub/econ_sit/econ_detail/buy/enemy_buy（进 timeline.econ，页面 build.py:45 的"我方 X vs 对方 Y"）和 user_team/user_k/user_d（771-778 的数据检查）。原表 user_team 只给 groups.csv 里 user 组的 BiuBiuZ 算（sit_rounds.py:32、234），high 组 demo 每回合都会记一条"user_team 标记和 demo 阵营不一致"。 |
| F8 | facts/shooting.py:284-288 | `user_duels(ctx)`（场次） | base/user_duels.csv | 用户本场 user_m3。287 的打印里"场次 {scene}"去掉。 |
| F9 | facts/extract.py:373-374 | `pro/data/positions_*.csv` 全部 | 静态 positions_* **去掉 positions_<本场 id>.csv**，再加 base/positions.csv | 这是道具落点 → 报点名的参考格子。现在同一个 demo 在库里和不在库里落点报点会不一样（库里的会多一份自己的位置），结果依赖"有没有入库"。改成"其他场静态 + 本场 base"后，已入库和新 demo 同口径；base/positions.csv 和 pro/data/positions_<id>.csv 一样时，4 场用户 demo 输出不变。 |

不用改（静态对标，按任务说明保留）：shooting.py:283 groups.csv、:291 pro/data/duels_<对标>.csv、:30 match3/_shots_cache、economy.py:601 sit_rounds.csv 的 high/pro 行、thresholds/team.json wp_table、base.py:1006 prefire_ref.json 的聚类——但这些都要做"排除本场 / 本人"，见第 3 节。

pipeline 要配合：
- P1 pipeline.py:20-35：STAGES / SPEC 在 extract 前加 `('base', '读底稿')`（产出 `base/`，源码 `['facts']`）；extract/cache/economy/shooting 的指纹链自动跟着变。
- P2 app/server.py:43-44 的 STAGES 副本、CONTRACT.md 的阶段表一起加（run.py 的 info 事件会覆盖 server 的列表，但排队时显示的是这份）。
- P3 pipeline.py:173 `USAGE_WORDS` 里的"目前只支持已经入库的 demo"删掉；run.py 文件头"demo 还没入库"那条退出码说明也删。

---

## 2. 场次 / "第 N 场"

| # | 文件:行 | 现在 | 新 demo 会怎样 | 改法 |
|---|---|---|---|---|
| F12 | facts/extract.py:283、875、884 `meta.match_no = MATCH_NO` | 场次 = BiuBiuZ 那份多场报告里的顺序（1–4） | base.py:18、1056 把场次固定成 1 → **每个新 demo 页面都会写"第 1 场"** | `match_no` 默认 None。要保留 4 场旧页面的"第 3 场"，可以只读地按 base/user_duels.csv 的击杀 tick 集合去 report/duels_de_mirage_<用户名>.csv 里找对应场次，找到才填，找不到就 None，不报错。match_no 不进 review.json，不影响确定性。 |
| F13 | facts/economy.py:834 | economy.json meta.match_no 取 timeline | 跟着 F12 | 不用单独改。 |
| F14 | build.py:61-65 `page_title`、:73、:94；page_template.html:258；app/index.html:362 | 有 match_no 就写"<用户> 第 N 场复盘" / "第 N 场 ·" | match_no=1 时全部写成第 1 场 | F12 改成 None 后自动走"<用户> <id> 复盘"。 |

---

## 3. 对标里混进本场 / 用户本人

| # | 文件:行 | 现在 | 问题 | 改法 |
|---|---|---|---|---|
| B1 | facts/shooting.py:290-292 | high/pro 组每场 duels_<demo>.csv 全部玩家都进 high/pro | 复盘 11 场 FACEIT 里的任一场时，这一场 10 个人（含用户本人）都在 high 对标里；summary.py:231、:362-368 用 `by_class.*.high` 做 SUM-01/02 和强项门槛 | 循环里跳过 `g.demo == DEMO`，并且 `du = du[du['steamid'] != USER]`（用户在别的对标场出现也去掉，例如 高分玩家04 在 14 场里）。 |
| B2 | facts/economy.py:601-602 | `S = sit_rounds.csv[group ∈ high, pro]` | 本场在 high 组时，B1–B8 的 high 统计含本场的回合 | `S = S[S.demo != DEMO]`。sit 是按队算的，用户本人在别的对标场出现时可选再去掉"用户那一方"的行（需要 pro/data/duels 或 kills 里查用户阵营），最少要去掉本场。 |
| B3 | thresholds/team.json:18-19 `wp_table`（rules/team.py:344 用） | 高分 11 场 + 职业 12 场 kills_/rounds_ 汇总好的人数状态胜率表 | 本场在 high 组时，表里有本场的回合（TEAM-01 功劳的 ΔWP 用它） | 两种做法：(a) team.py 运行时用 work/_scratch/rules-team/wp_table.py 的同一算法从 pro/data/kills_/rounds_ 现算，跳过 ctx.id；(b) thresholds 里另存每场的计数 `wp_by_demo: {demo: {键: [w, n]}}`，运行时总表减去本场。(b) 快且确定。 |
| B4 | facts/base.py:1004-1006（标准位 / 凶手位置） | prefire_ref.json 的 contexts.*.clusters（职业 + 高分局开枪位聚类） | 本场在 high 组时聚类里有本场；不过 标准位 目前没有规则读（只进 timeline user_fights.standard_pos） | 低优先级：知道就行；以后有规则读它再按场次重聚类。 |
| B5 | thresholds/timing.json:35 `ba_transit_s`（高分 P75）、summary.py:26 `trade_share_high=0.065`（rules_v1 SUM-03 bench） | 静态数 | 同上，含 high 那 11 场 | 1/11 的影响，保留为静态参考；在文档里注明来源含哪些场即可。 |
| B6 | config.py:40-49 `demo_id` | 改过名的 FACEIT demo 只在 report/cache 里按大小 + sha1 找原 id | pro/cache 的对标 demo 改个名再丢进来，会拿到 sha1 id，B1–B3 的"排除本场"按 id 对不上，对标里又有它自己 | 同样的查找再扫一遍 `pro/cache/1-*.dem`（只比较大小相同的，代价很小）。 |

---

## 4. 用户的"其他场"

| # | 文件:行 | 现在 | 问题 | 改法 |
|---|---|---|---|---|
| U1 | facts/shooting.py:293-299 | `others` = groups.csv 里 **user 组**（BiuBiuZ 的 5 场）中 report/cache 有 demo 文件的，再按 steamid==USER 过滤 | 换别的用户时：要么是空行（白解析 3 场 demo），要么漏掉他真正的其他场（高分玩家04 在 high/pro 里有 13 场）；返回语"对标用户其他 3 场"不对 | `others` = groups.csv **所有组**里 `pro/data/duels_<d>.csv` 有这个 steamid 的场（排除本场）。只用静态库，不要扫 work/*/base（那样同一个 demo 的 shooting.json 会随"之前复盘过哪些场"变化，不确定）。user_other 目前没有规则或 summary 读，只进 shooting.json / shooting_out.txt。 |
| U2 | facts/shooting.py:139-142 `user_demo_file`、:161-171 `dem_path` | 用户其他场只在 report/cache 找 `1-<id>*.dem` | 别的用户的 demo 在 pro/cache / pro/hltv | `dem_path` 已经会依次找 pro/cache、pro/hltv；U1 改了以后 `user_demo_file(d)` 这个过滤条件去掉，交给 `dem_path`（找不到会打印跳过）。 |
| U3 | facts/shooting.py:81 GNAME、:360 返回语、:13-14 文件头 | "你(其他 Mirage 场次)"、"对标用户其他 N 场" | 文字跟着 U1 | 跟着改。 |

---

## 5. MR12 / 加时 / 半场

| # | 文件:行 | 写死 | bd125285 / 别的赛制会怎样 | 改法 |
|---|---|---|---|---|
| M1 | facts/economy.py:323、:367 `for half in ((1, 12), (13, NR))` | 只有两个半场 | 加时回合 25–29 被当成下半场接着算连败计数（CS2 加时每半场开局给固定钱、计数重置），25+ 回合的 loss_counter / if_loss_next 都错；连败模型检验的吻合数也被拉低 | 半场切分改成和 rules/eco.py:42-55 `_half` 一样：常规 `half_rounds`（12），加时每 `ot_half_rounds`（3）回合一段；从 thresholds 读（economy 可以读 ctx.th['eco']，或者把 half_rounds/ot_half_rounds 挪到 common.json 大家共用）。 |
| M2 | facts/economy.py:238 `r == 12` | 只有第 12 回合后钱重置 | 24、27、30… 回合后也重置；那几回合的 d_late 会被当成正常奖励统计 | `_last_of_half(r)`（同 M1 的半场函数）。 |
| M3 | facts/economy.py:566-568 `rec['round'] != 12` | next_actual 只在第 12 回合断开 | 24→25、27→28 的"下一回合实际开局"拿到的是加时起始钱 | 同上。 |
| M4 | facts/economy.py:402-404（R13 开局钱）、:497 `r in (1, 13)`（手枪局）、:802、:805、:828（说明文字） | MR12 | 加时没有手枪局，这里本来就对；但写死 13 和 thresholds 的 half_rounds 不同源，改阈值会不一致 | 用 `1` 和 `half_rounds + 1`；说明文字按实测拼，加时场加一句加时开局钱（实测 balance）。 |
| M5 | facts/economy.py:628 `[3, 15]`、:665 `[1, 2, 13, 14]`、`<= 24`、:687、:710 `[2, 14]`、:743 `(13, 25)`、`> 24` | MR12 | 这些只过滤**静态对标表**（对标场都是 MR12），可以不动；为了和 M1 同源可以一起换成 half_rounds | 可选。 |
| M6 | rules/timing.py:160-169 `_halves`（:830、:844、:857 TIME-09 用） | 按用户阵营连续不变分半场 | 加时上半场和常规下半场同一边（bd125285 的 13–27 都是 CT），会被当成同一个半场，TIME-09"同一半场 ≥2 回合中远被 AWP 首杀"会把下半场和加时合起来数 | 改用和 eco.py `_half` 同一个按回合号的函数（最好放进 common.py，eco/post/timing 共用）。 |
| M7 | facts/base.py:356 `econ_class (1, 13)`、:910、:929、:943-972（sit：1/13、2/14、3/15、12/24、>24） | MR12（和原 sit_rounds.py / build_report.py 一样） | 和静态 sit_rounds.csv 同口径，必须保持一致；加时 25+ 记"加时"本来就对 | 不改算法，只把 13/24 换成 half_rounds 推出来的值（值不变）。 |
| M8 | facts/extract.py:866-871 `by_side`；page_template.html:262 | 按 CT/T 两边汇总，页面写"CT 半场 x:y" | 加时回合并进同边，bd125285 的"CT 半场"其实是下半场 + 加时上半场 | 页面标签改"CT 方 / T 方"，或者 by_side 只算常规 24 回合、另给 overtime 一项。 |

已经正确：rules/eco.py:42-79（thresholds half_rounds / ot_half_rounds，第 25 回合不算手枪局）、rules/post.py:255-279（加时 MR3 赛点、加时半场最后一回合）、summary.py:98-108（第一次换边 = 上半场比分，加时不影响）、rules/eco.py:705-712（ECO-09 加时段走不带"上半场"的句子）。

---

## 6. 固定阵营顺序

| # | 文件:行 | 现在 | 问题 | 改法 |
|---|---|---|---|---|
| S1 | page_template.html:262 | `["CT", "T"].filter(...)` 固定先写 CT | 用户先打 T 的场（例如 bd125285）页面也先写 CT 半场，读起来像时间倒过来 | 按 `M.start_side` 排：先开局那一边。build.py:76 `sorted(bs)` 同理（meta.halves 是 dict，顺序无所谓，页面排就行）。 |
| S2 | facts/extract.py:788-791、:839-841 | 计分板名单 = 第 1 回合冻结结束时在场的人，阵营也按第 1 回合 | 用户第 1 回合不在（晚进、掉线重连）：`board[USER]`（:859）、`team_of[1][USER]`（:839）KeyError；中途换人的替补不进计分板 | players 取所有回合 team_of 的并集；start_side 取这个人第一次出现的回合；用户第 1 回合不在时 user_start_side 取他第一次出现时的阵营。 |
| S3 | facts/extract.py:488 `team_of[rnd][USER]`；facts/economy.py:195 `side_of[(r, USER)]` | 每回合都假设用户在场 | FACEIT 掉线/重连的回合直接 KeyError | 用户不在场的回合：timeline 记 `side=None`/跳过这一回合的用户字段，economy 的 `user_side` 用上一回合推（换边按半场函数）；extract:354-355 已经会记 data_issue，但后面先崩了。 |

---

## 7. 用户 = BiuBiuZ / 默认 steamid / 按名字认人

| # | 文件:行 | 现在 | 问题 | 改法 |
|---|---|---|---|---|
| N1 | config.py:24-25 `DEFAULT_USER(_NAME)`；ctx.py:132 `make_ctx(user=DEFAULT_USER)`；run.py:23、:36；clips.py:113-118；base.py:1115 | 不给 --user 就是 BiuBiuZ | 命令行不带 --user 复盘别人的 demo → "这个 demo 里没有用户 76561199042148360"（会列出名单，能用） | 可以保留默认，但更好的是：不给 --user 时如果默认用户不在 demo 里，报错信息里把名单列成可直接复制的 `--user <steamid>`（extract.py:123-125 已经列了名字和 steamid）。 |
| N2 | app/server.py:38、:386-388 `user_of`；app/index.html:181、:192 | 空 steamid → BiuBiuZ；输入框默认填 BiuBiuZ | 别的玩家用窗口时容易忘了改；失败时只能看报错里的名单再手抄 steamid | 失败任务的 error 里带上名单（run.py 已经透传 FactsError 中文），index.html 把"demo 里的玩家：名（steamid）"解析成按钮，点一下用这个 steamid 重排一次任务。 |
| N3 | pipeline.py:52、:93、:237；build.py:112-113；server.py:274-287 `list_reports`（`WORK/<stem>/meta.json`） | work/<id>/、report/<id>.html、meta.json 都只按 demo id | **同一个 demo 换个用户复盘会覆盖**上一个用户的页面和历史条目；指纹里有 user（pipeline.py:106）所以会重跑，但前一个用户的结果就没了；cache 阶段和用户无关却也跟着重跑 | 输出 id 改成 `<demo id>` + 用户后缀（例如默认用户不加、其他用户加 `-<steamid 后 6 位>`），work 里拆成共享的 `work/<id>/{match.dem,cache,base 的对局部分}` 和按用户的 `work/<id>/u_<steamid>/`；report 用带后缀的名字；cache 的指纹去掉 user。 |
| N4 | summary.py:345-353 `_trade_share`、build.py:51 `k["killer"] == me`、compose.py:283-314、merge（info.user = 用户名）、CONTRACT 的 `death_of` = 用户名 | 按**名字**认用户 | 名字和别人一样、或者中途改名（extract 的 name_of 取事件里最后一次的名字，common.py:426-428 取帧里最后一次的名字，两边可能不同）时认错人 | timeline.kills 里已有 `victim_is_user`、extract 里有 `killer_id`（kout 写出前被删了，:696）——保留 `killer_is_user` 布尔，summary/build 用布尔判断；名字只用于文字。 |
| N5 | app/server.py:250-271 `legacy_match3` | 历史列表固定带上 BiuBiuZ 的旧 match3.html | 别的用户的历史里也会出现 | 可选：历史列表按当前输入框的 steamid 过滤（meta.json 有 user_steamid）。 |

规则层（rules/*.py、common.py、merge.py、compose.py、validate.py）**没有**写死用户名、steamid、demo id 或回合号：用户都从 `C.user_sid(ctx)` / timeline meta 来，人名校验用本场名单（validate.py:220-222）。只在注释里出现过 51920faa、队友D、队友C（timing.py:632、post.py:384、team.py:394），不影响运行。

---

## 8. FACEIT 文件名

| # | 文件:行 | 现在 | 问题 | 改法 |
|---|---|---|---|---|
| D1 | config.py:30、:36-38 | `1-<8位hex>-` 取 id，否则 sha1 前 8 位 | 非 FACEIT（Premier/完美/HLTV）能跑，id 是 sha1；没问题 | 不用改。 |
| D2 | config.py:42 | 改名识别只扫 report/cache | 见 B6 | 加扫 pro/cache。 |
| D3 | facts/shooting.py:141、:167 | `1-{demo}*.dem` | 用户其他场 / 对标找 demo 文件只认 FACEIT 名；pro 组走 pro/hltv/<名>.dem | U1 以后用户其他场来自 groups.csv 的静态库，这两个 glob 足够；不在库里的用户场不参与（确定性）。 |
| D4 | base.py:607-616 `group_of` | groups.csv 里有就用它的组，没有记 user | 只影响 base 表里的 group 列；extract/shooting 不按这列分组 | 不用改。 |

---

## 9. 改完以后的检查

1. 4 场用户 demo（8d9b0b8f、51920faa、a32a2106、bd125285，--user 76561199042148360）：
   - base/rounds.csv == pro/data/rounds_<id>.csv；base/user_duels.csv（去掉场次列）== report/duels_de_mirage_BiuBiuZ.csv 对应场次那几行；base/sit.csv 对应列 == sit_rounds.csv 里这场的行（group/scene/user_* 除外）；base/positions.csv == pro/data/positions_<id>.csv。
   - review.json 和改之前**逐字节一样**（match_no 不进 review.json；F9 在 positions 一致时不变）。除了 bd125285 的 M1/M6 修正会改动加时回合的经济数字和 TIME-09——这是预期的修正，单独列出差异。
2. 11 场 pro/cache FACEIT demo，各挑一名玩家 `--user`（至少包括 高分玩家04 90000000000000111，他在全部 11 场里）：9+1 步都过、validate 通过、跑两次 review.json 字节一样；shooting.json 的 high 样本数 = 原来 high 总数 − 本场 − 用户本人的行。
3. 别的用户复盘 BiuBiuZ 的场（队友B 90000000000000155 @ 8d9b0b8f）：能跑；不覆盖 BiuBiuZ 的 report（N3）；user_other 是他自己在库里的场（8d9b0b8f 之外的 bd125285、demo1 里有 demo 文件的那场）。
4. pro/cache 某一场改名（去掉 `1-` 前缀）再丢进来：id 仍然是原 8 位（B6/D2），对标照样排除本场。
5. 用户不在 demo 里：报错列出名单，退出码 2；窗口里能点名字重排（N2）。
