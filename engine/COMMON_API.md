# common.py 接口（规则模块照这份写）

`from engine import common as C`。所有函数第一个参数是 `ctx`（CONTRACT 的 Ctx）；数据只从 `ctx.cache(name)` / `ctx.json(name)` 读，阈值只从 `ctx.th['common']`（`thresholds/common.json`）取。同一个 ctx 第一次调用时建一次数据（约 1 秒），之后都走内存；`shots()` / `bursts()` 按人缓存。规则代码里不要再写 idx / eye / sees / 速度 / 烟 这些，全部用这里的。

约定：

- `sid` 一律是 steamid 字符串；`tick` 是 demo tick（int）；`rnd` 是回合号（从 1 开始）。
- 秒数 `t` 从冻结结束算（买枪阶段为负数），函数返回的是不取整的 float；**facts 里存 `C.sec(t)`，句子里写 `C.fmt_sec(t)`**，两者同一个取整。
- 可见性、补枪、听声一律 3D（`dist3`、`eye`、spotted）；`dist2` 只给"站位远近、走了多远"用。
- 判断只用玩家当时能知道的（P1）。`hp()`、对方位置只能用来写后果，不能写成"你该知道"。

## 数据来源（给 facts/cache.py）

| 用途 | ctx 名 | 要求 |
|---|---|---|
| 帧 | `cache('ticks')`（首选；= 原型 `ticks_8d9b0b8f.pkl`） | 每 tick 一帧：`tick steamid name X Y Z pitch yaw duck_amount is_alive health team_num active_weapon_name is_scoped last_place_name approximate_spotted_by`；顶层还有 `fire`（weapon_fire，带 `user_X/Y/Z user_pitch user_yaw user_velocity_X/Y user_is_scoped user_last_place_name`）、`hurt`、`death`、`blind` |
| 帧（退化） | `sight_s4` → `sight_s8` → `sight` → `pp_ticks` → `daoju` | 原型 sight/pp/daoju 缓存格式（`ticks` + `ev`）。没有 `ticks` 时才用；weapon_fire 没有速度时停稳改用帧差，第 3 场 248 枪里 sight_s4 有 197 枪三档一致、sight_s8 180 枪 |
| 烟 | 上面任一缓存的 `smokegrenade_detonate` / `smokegrenade_expired`（`entityid x y z`） | `ticks` 缓存里没有，所以 **至少还要有一个 sight 缓存**（或者把这两个事件也放进 ticks.pkl 顶层） |
| 回合 | `json('timeline.json')` 的 `rounds[].key_ticks`（`freeze_start / round_start / round_end`）和 `side` | |
| 经济 | `json('economy.json')` | `rounds[].us/them.buy、avg_start、players[].start`、`side` |
| 知识库 | `engine/knowledge/mirage_kb.json`（可选的本地扩展，本仓库不带；没有时 kb_items() 返回空） | 只读，背景用，不引用原文 |

`ctx.json` 同时接受 `'timeline.json'` 和 `'timeline'`；`ctx.cache` 缺文件时抛 `FileNotFoundError`/`KeyError` 即可，common 会换下一个。

## C-TIME 时刻 / 回合

| 函数 | 说明 |
|---|---|
| `rounds(ctx) -> [ {n, freeze_start, start, end, side} ]` | 按回合号排；`start` = 冻结结束，`side` = 用户阵营 |
| `round_info(ctx, rnd) -> dict` | 同上一条 |
| `round_of(ctx, tick) -> int \| None` | 从这回合冻结开始到下回合冻结开始 |
| `t_of(ctx, tick, rnd=None) -> float` | 冻结结束后的秒数，买枪阶段为负 |
| `tick_of(ctx, rnd, t) -> int` | 反过来 |
| `fmt_sec(t) -> str` | 句子用：一位小数四舍五入，小数是 0 就取整（`44.0→'44'`，`39.25→'39.3'`） |
| `sec(t) -> float` | facts 用，和 `fmt_sec` 同一个取整 |

开枪类时刻取这一串第一枪（`burst['t']`），阵亡类取阵亡 tick（`death_tick`），回合层面的写"回合"。

## C-PLACE 报点

| 函数 | 说明 |
|---|---|
| `PLACE_CN` | 来自 `engine.config.PLACE_CN`（config 不在时用 common 里同样内容的一份） |
| `place_cn(place, default='某处') -> str` | last_place_name → 中文；空值和**没收录的英文名一律返回 default**（config.place_cn 会原样返回英文，这里不会） |
| `place_at(ctx, sid, tick) -> str \| None` | 帧上的原文（判断用） |
| `place_cn_at(ctx, sid, tick, default='某处')` | 句子用 |
| `nearest_place(ctx, xyz, rnd=None) -> str \| None` | 没有报点的 3D 点（道具落点、烟）取最近的帧报点原文 |

## 玩家 / 阵营

| 函数 | 说明 |
|---|---|
| `user_sid(ctx)` | ctx.user 是 steamid 或名字都行 |
| `players(ctx)`、`name_of(ctx, sid)`、`sid_of(ctx, name)` | |
| `team_of(ctx, sid, rnd) -> 2/3/0`、`side_of(ctx, sid, rnd) -> 'T'/'CT'`、`user_side(ctx, rnd)` | 名单取冻结结束后 1 秒那一帧的阵营 |
| `mates(ctx, rnd, sid=None, include_self=False)`、`enemies(ctx, rnd, sid=None)`、`is_enemy(ctx, a, b, rnd)` | sid 默认用户；返回按 steamid 排好序 |
| `side_name(x)`、`other_side(side)`、`team_num(side)` | 2/3/'T'/'CT' 互转 |

## C-ALIVE 存活 / 人数（阈值 `alive_lag_s`=0.25）

| 函数 | 说明 |
|---|---|
| `death_tick(ctx, sid, rnd) -> int \| None` | player_death |
| `alive(ctx, sid, tick, rnd=None) -> bool` | 按击杀栏：这回合在名单里、阵亡 tick 晚于 tick（不用帧上的 is_alive） |
| `alive_counts(ctx, tick, sid=None, lag_s=None) -> (us, them)` | **某一刻的人数**：死亡 tick 严格早于 tick 且至少早 0.25 秒的才算已死（自己的死不算进去）。`us` = sid 那一边。要"这次击杀之后"的人数传 `tick=击杀tick+1, lag_s=0` |
| `alive_at_death(ctx, victim, rnd) -> (死者一方, 对方)` | 阵亡那一刻（C-ALIVE 口径） |
| `alive_list(ctx, rnd, tick, side_sid=None, enemy=False) -> [sid]` | 还活着的一方 |
| `alive_str(us, them) -> '3v2'` | 句子用 |

## 帧查询 / 几何

| 函数 | 说明 |
|---|---|
| `pos(ctx, sid, tick) -> np.array(3)` | 脚下 |
| `eye(ctx, sid, tick)` | 眼睛：Z + `eye_height`(64) − `crouch_drop`(18) × duck |
| `view(ctx, sid, tick) -> (pitch, yaw)`、`weapon_at`（显示名）、`scoped_at`、`hp` | 帧（tick 之前最近一帧） |
| `dist3(a, b)`、`dist2(a, b)` | |
| `view_vec(pitch, yaw)` | pitch 正 = 往下看 |
| `aim_err(src, pitch, yaw, dst) -> (总°, 左右°, 高低°, 距离)` | 左右正 = 准星在他左边；高低正 = 准星高于目标点 |
| `half_body_deg(ctx, dist)` | atan(`half_body_units`=16 / 3D 距离) |
| `body_err(ctx, src, pitch, yaw, sid, tick)` | 准星到身体竖线（脚上 `body_bottom`=10 到眼睛，`body_samples`=6 点）的最小偏差 |
| `aim_limit(ctx, dist, min_deg, n_half)` | max(min_deg, n_half 个半身宽)，规则自定口径时用 |
| `facing_deg(ctx, sid, tick, dst)` | 水平朝向夹角 |
| `path_len(ctx, sid, t0, t1)` | 平面路程（每 `move_window_ticks` 采样，瞬移段去掉） |

## C-STILL 停稳（`still_frac` 0.34、`run_speed` 180、`max_speed`、`max_speed_scoped`、`move_window_ticks` 8、`move_teleport` 400）

| 函数 | 说明 |
|---|---|
| `max_speed(ctx, w, scoped=False)` | 开镜的狙（awp 100、g3sg1/scar20 120、ssg08 230）用开镜速度 |
| `still_limit(ctx, w, scoped=False)` | 最大速度 × 0.34（AK 73、M4 76、沙鹰 78、USP 82、开镜 AWP 34） |
| `move_word(ctx, spd, w, scoped=False) -> '停稳' / '还没停稳' / '跑着'` | > 180 叫跑着 |
| `fire_speed(ctx, fire_row)` | 开枪那一刻：weapon_fire 的 velocity_X/Y；没有时用 `move_speed` |
| `move_speed(ctx, sid, tick) -> float \| None` | 不开枪时：tick 和 tick−8 两帧的平面位移 / 时间；瞬移返回 None |

## 事件

| 函数 | 说明 |
|---|---|
| `deaths(ctx, rnd=None)` | 列 `tick round att vic wk headshot thrusmoke …`，按 tick 排，只含比赛开始后 |
| `hurts(ctx, rnd=None)` | 列 `tick round att vic wk dmg health hitgroup` |
| `fires(ctx, rnd=None, sid=None, guns_only=True)` | 列 `tick round sid wk gun user_X/Y/Z user_pitch/yaw user_velocity_X/Y user_is_scoped` |
| `blind_left(ctx, sid, tick) -> 秒` | 还剩多久白屏；"被闪多久算白屏"由规则自己的阈值定 |

## C-HIT 命中（`hit_ticks` 3）

`shot_hits(ctx, shooter, tick, victim=None, rnd=None) -> DataFrame`：开枪后 0–3 tick 内这名开枪人打**敌人**的 player_hurt。

## C-SEEN 看得见

`seen(ctx, viewer, target, tick, shot=None, f_min_deg=None, f_need_radar=True) -> 'a'..'f' | None`，返回满足的第一条：

| | 条件 | 阈值键 |
|---|---|---|
| a | target 的 approximate_spotted_by 里有 viewer（取 tick−4 到 tick+16；spotted 每 1/8 秒才更新） | `seen_a_back_ticks` 4、`seen_a_fwd_ticks` 16 |
| b | 这一枪打中了他（要 shot） | |
| c | 他 ±1 秒内打中过 viewer | `seen_c_s` |
| d | 他 ±1 秒内朝 viewer 开枪，开枪方向离 viewer 身体 < max(1.5°, 2 半身宽)，并且 viewer 是我方活人里离这条枪线最近的、或者他那一枪前 4 tick 内 spotted 了 viewer | `seen_d_s`、`seen_d_min_deg`、`seen_d_half_bodies`、`seen_d_spot_ticks` |
| e | viewer 这一枪离他身体 ≤ max(5°, 3 半身宽)，并且 ±1 秒内"viewer 在他的 spotted 里"（要 shot） | `seen_e_s`、`seen_e_min_deg`、`seen_e_half_bodies`、`seen_e_mode` |
| f | 全服务器 ±16 tick 没有任何 spotted 记录，这一枪离他身体 < max(2°, 1.5 半身宽)，并且他 ±1.5 秒内上过 viewer 一队的雷达（要 shot） | `seen_f_gap_ticks`、`seen_f_min_deg`、`seen_f_half_bodies`、`seen_f_radar_s`、`seen_f_blackout_ok` |

- `shot` 是 `shots()` 返回的一枪；不给 shot 时只看 a / c / d。
- `seen` 不管烟：隔烟要另外调 `smoked()`（C-SMOKE）。例：R15 45.4 对手5 隔烟打死你，`seen` 因为他打中过你返回 'c'，`smoked` 返回那颗烟。
- `seen_e_mode`：`he_sees`（默认，按 SHOT-01/SHOT-03 说明里的"他看得见你" = 他 spotted 了 viewer）/ `viewer_sees`（字面读成 viewer spotted 了他）/ `either`。第 3 场三种读法的命中一样。
- `f_min_deg`、`f_need_radar`：规则自己的 f 口径，例如 SHOT-04 狙"全服务器没有 spotted 时方向离他身体不到 3°"、不提雷达 → `seen(..., f_min_deg=3, f_need_radar=False)`。
- `seen_window(ctx, viewer, target, t0, t1) -> (tick, 字母) | None`：这段时间里第一次看得见（帧上的 a/c/d 每 4 tick 查一次，再加 viewer 对着他的开枪 b/e/f）。"能不能补枪""队友倒下时你看不看得见凶手"用这个。对方死后 spotted 还会挂几秒（尸体），这里只算两人都活着的帧。
- `spots(ctx, a, b, t0, t1=None) -> tick | None`：a 看见了 b（b 的 spotted_by 里有 a，两人都活着）的第一帧。
- `server_spotted(ctx, t0, t1) -> bool`：这段时间全服务器有没有 spotted 记录。

## C-TARGET / C-FIGHT 每一枪、每一串

`shots(ctx, sid=None) -> [dict]`（默认用户，按 tick 排，只含枪械、活着时开的枪）。每枪：

| 键 | 含义 |
|---|---|
| `i tick round t sid` | `t` 不取整 |
| `wk weapon wcls tier sniper scoped` | 武器键、中文名、大类、C-GUN 档、是不是狙、开没开镜 |
| `spd move still` | C-STILL：开枪速度、'停稳'/'还没停稳'/'跑着' |
| `pitch yaw src place` | 开枪方向、眼睛 3D、报点原文 |
| `hit_vics` | C-HIT 打中的敌人 |
| `target` | C-TARGET：打中了谁就是谁；没打中时取偏差 ≤ max(5°, 3 半身宽) 且 C-SEEN 成立的里偏差最小的；都不成立时，同一回合上一枪（1 秒内，狙 3 秒内）有目标、这一枪离那个人 ≤ 20°（狙 8°）就接上；再不行取偏差在线内、但看不见的那个人 |
| `seen` | `'a'..'f'`、`'chain'`（接上的）、`None`（对着人但看不见，或没有目标） |
| `hit hitgroup dmg` | |
| `err_body err_head dist hb` | 到目标身体的偏差°、到头（眼睛）的 (总°, 左右°, 高低°)、3D 距离、半身宽° |
| `cands` | `{敌人 sid: (身体偏差°, 3D 距离)}`，规则要换口径时自己挑（例如 SHOT-04） |
| `engage_first` | C-FIGHT：这一枪对着看得见的人（a–f，不含 chain），并且离上一次对着看得见的人开枪已过 0.8 秒、或上一个目标已被打死而这一枪对着另一个人 |
| `burst` | 所在串的编号 |

`bursts(ctx, sid=None) -> [dict]`：同一目标、同一回合、两枪间隔 ≤ `burst_gap_s` 1 秒（狙 `burst_gap_sniper_s` 3 秒）为一串（SHOT-03 的口径）。每串：`id round t tick`（都取第一枪）`sid target wk weapon wcls n hits moves shots seen engage_first engage_ticks`。

- `seen`：串里有没有对着看得见的人的枪。
- `engage_first`：串里第一枪"对着看得见的人"的那枪是新交火 = **第一串**。串前面对着同一个人、还没有可见证据的枪也算在串里，所以串的 `t` 可能早于第一条可见证据（例：R3 42.6 第一枪还没证据，42.7 才有 f）。
- `engage_ticks`：串里所有新交火起点（同一串中途停了 0.8–1 秒又开火的也会列出）。

`first_bursts(ctx, sid=None)`：`engage_first` 且对着看得见的人的串（SHOT-01/02 的对象）。

阈值：`target_min_deg` 5、`target_half_bodies` 3、`chain_s` 1、`chain_sniper_s` 3、`chain_deg` 20、`chain_deg_sniper` 8、`fight_gap_s` 0.8、`burst_gap_s` 1、`burst_gap_sniper_s` 3。

## C-RADAR 雷达（`radar_look_s` 15 只是默认回看）

| 函数 | 说明 |
|---|---|
| `on_radar(ctx, enemy, t0, t1, team_sid=None) -> bool` | 这段时间里 team_sid（默认用户）一队任一活人在他的 spotted_by 里；t0 截到本回合冻结结束 |
| `last_on_radar(ctx, enemy, tick, look_s=None, team_sid=None) -> tick \| None` | 最后一次在雷达上 |
| `radar_enemies(ctx, tick, look_s=0.0, team_sid=None) -> [(sid, tick, 报点原文)]` | |

## C-SMOKE 隔烟（`smoke_radius` 144、`smoke_z_low` 0、`smoke_height` 130、`smoke_default_s` 20）

| 函数 | 说明 |
|---|---|
| `smokes(ctx, rnd=None) -> [ {t0, t1, x, y, z, by, entityid, round} ]` | 起爆→消散，按 entityid 配对，配不上按 20 秒 |
| `smoke_between(ctx, a, b, tick) -> dict \| None` | 3D 线段 a→b 穿过生效中的烟柱（圆柱，解析求交） |
| `smoked(ctx, sid_a, sid_b, tick)` | 两人眼睛之间 |

## C-HEAR 听得到（`hear_shot` 2000、`hear_near` 600）

| 函数 | 说明 |
|---|---|
| `can_hear(ctx, listener, src_xyz, tick, near=False) -> bool` | 3D 距离近似，没算墙和消音器 |
| `heard_shots(ctx, listener, t0, t1, shooters=None, near=False) -> DataFrame` | listener 活着时听得到的枪声（fire 行） |
| `hear_limit(ctx, near=False)` | |

## C-ECO 买法（`full_buy` T 3900 / CT 4400、`force_min` T 2050 / CT 2250、`force_avg` 2500、`force_n` 4、`awp_need` 5750）

| 函数 | 说明 |
|---|---|
| `econ_round(ctx, rnd)` | economy.json 这一回合 |
| `buy(ctx, rnd, who='us') -> 'eco'/'强起'/'半起'/'全起'/'手枪局'` | 一律取 economy.json |
| `econ_side(ctx, rnd, who='us')` | |
| `full_buy_line(ctx, side)`、`force_line(ctx, side)`、`awp_line(ctx)` | |
| `can_force(ctx, rnd, who='us') -> bool` | 人均开局钱 ≥ $2500 且 ≥ 4 人开局钱够强起最低线 |

## C-GUN 武器

| 函数 | 说明 |
|---|---|
| `wkey(w)` | `'weapon_ak47' / 'ak47' / 'AK-47'` → `'ak47'`；`*_off`（拆消音器）归到原枪；所有刀（含 Karambit 等皮肤名）→ `'knife'` |
| `is_gun(w)`、`is_sniper(w)` | |
| `gun_tier(w)` | 手枪 0 < 冲锋枪、霰弹、机枪 1 < 步枪 2 < AWP / 鸟狙 / 连狙 3；不是枪 −1 |
| `weapon_class(w)` | '手枪' / '冲锋枪' / '霰弹' / '机枪' / '步枪' / '狙' |
| `weapon_cn(w, default='枪')` | AK、M4A4、M4A1-S、AWP、沙鹰、USP、格洛克、P2000、P250、Tec-9、FN57、CZ75、双枪、吹风机（MAC-10）、MP9、MP7、MP5、UMP、P90、野牛、Galil、法玛斯、AUG、SG553、鸟狙、连狙、霰弹、机枪、刀、雷、闪、烟、火 |

## 句子小工具 / 知识库

`fmt_sec`、`sec`、`alive_str`、`num_cn(n)`（一、两、三…）、`money(v)`（'$2500'）、`place_cn`、`weapon_cn`。

知识库只作背景（M6），句子里不引用原文：`kb_items(path=None) -> {id: item}`、`kb_item(id)`、`kb_find(side=None, area=None)`（side 取 'T'/'CT'，'通用' 条目总会带上；area 按中文区域包含匹配）。

## 和原型不同的地方（都按 rules_v1 common 改）

| 项 | 原型 | common.py |
|---|---|---|
| 报点 | 各文件自己一份；`Shop` 写成"B 商店"、`Scaffolding` 写成"A 二楼出口"、`House` 有的写"T 家 / T 出生点 / A 二楼外" | 只用 config.PLACE_CN：超市、跳台、T 方房区、A 二楼外巷；没收录的英文名不进句子 |
| 人数 | 每帧 is_alive（站位、道具）或 timeline.alive（下包后、好的操作，取"≤ t"或"< t"不统一） | 只数 player_death，严格早于且至少早 0.25 秒 |
| 存活（单人） | 帧上的 is_alive | 击杀栏；死后 spotted 还挂着的尸体不算看见（例 R18 66.8 对手3 死后 1.5 秒还在用户 spotted 里，原型团队配合算"看得见"） |
| 停稳 | 下包后原型用帧差 110 / 40；时机与转点用 ticks8 帧差；开枪与准星、好的操作用 weapon_fire 速度 × 0.34 | 开枪一律 weapon_fire 速度 × 0.34（开镜狙用开镜速度），> 180 跑着；不开枪用帧差（8 tick） |
| 目标归属 | 看得见的 ≤ 20°、看不见的 ≤ 4° | ≤ max(5°, 3 半身宽)；接上：1 秒内 20°，狙 3 秒内 8° |
| C-SEEN d | 他朝你开枪 < 5° 就算 | < max(1.5°, 2 半身宽)，且你离枪线最近或他看得见你 |
| C-SEEN e | 没有 | 新增（"他看得见你" + 你对着他打） |
| C-SEEN f | 整段 ±1.5 秒全服务器都没有 spotted 时，不要雷达条件也算 | 必须带雷达条件（`seen_f_blackout_ok: true` 可退回原型做法） |
| 新交火 | 没有目标、看不见目标的枪也会被标成交火第一枪 | 只有对着看得见的人（a–f）的枪才能开始交火，接上的枪（chain）不另开 |
| 分串 | 同一目标 1 秒（狙 3 秒） | 同样；另外串的 engage_first 取串里第一枪对着看得见的人的那枪 |
| 隔烟 | 开枪与准星：平面距离 135、烟取 timeline（没有 z）；道具：平面 150 | 3D 线段穿圆柱（半径 144、高 0–130），起爆→消散按 entityid 配对 |
| 雷达 | 道具：帧 is_alive 的队友；站位：不截回合 | 按击杀栏的活人，回看截到本回合冻结结束 |
| 枪档 | 好的操作把鸟狙算步枪档、机枪算步枪档 | 鸟狙 3（C-GUN）；机枪归 1（和冲锋枪、霰弹一档，rules_v1 没写，按站位与视线.py） |
| 秒数取整 | 各写各的 round(…, 1)（半数取偶） | `fmt_sec` / `sec` 四舍五入 |
