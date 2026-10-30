# -*- coding: utf-8 -*-
"""道具类规则：UTIL-01..07（问题 / 责任判定 / 思路）、UTIL-G1..G3（好的操作）。

依据：rules/rules_v1.json 道具类各条的 trigger / blame / template / alt（decisions_v1 没有改道具类）。
原型：pro/analysis/match3/rules_proto/道具.py（DJ-01..10）、好的操作.py（GOOD-08 → UTIL-G3）。

接口：run(ctx) -> list[Hit]（Hit 字段见 engine/CONTRACT.md）。
  - 数据只从 ctx 读：C-* 口径全部调 engine.common；背包（inventory）和道具起爆事件取 ctx.cache('daoju')；
    道具落点报点、起火时间、烧到谁取 ctx.json('timeline') 的 user_util；下包 tick 取 timeline 的 plant。
  - 阈值只从 ctx.th['util']（thresholds/util.json）和 ctx.th['common'] 取。
  - 句子（text / alt / evidence）遵守 P6：中文报点、不写坐标/速度/角度/百分比、不写对方血量、不写报点沟通、
    不引用知识库原文；知识库条目只放在 kb 字段。
  - 确定性：不用随机数和当前时间，所有遍历显式排序。

和原型不同的地方（都按 rules_v1 改）写在 docs/dev/rules/util_REPORT.md。

审核单（手册 v1，2026-10-30）的改动：
  - 第 1 条：UTIL-01 加五道门（_u01_gates）：原地架着被拉出来、正在扔 / 刚扔完道具、队友的闪刚给过、单走一侧还没等到人多那边开打
    （Q19 CT 也适用、Q20 窗口 5 秒）、T 下包后、eco、1vN、人数差 2 个以上、远处对枪身上没烟、转点的狙（Q8）都不写；
    第 5 道门（主句已经说别出去）由 merge 按 facts.contra_rules / fact_only_rules / clause_fact 处理。
  - 第 3 条：match_stats(ctx) 给 summary“闪买了常常没扔”的全场数据和每回合五道门判定。
  - 第 26 条：还可以按道具写（闪抢露头、烟切线、火卡近处），不写“等火烧起来再出”“扔闪拖时间”；拖时间只给烟 / 火且要有报点。
  - 第 27 条：回防已经往包点扔了两颗以上道具，不出 UTIL-01，改出 UTIL-G3（facts.trigger='retake'）好的一面。
  - 第 29 条：UTIL-02 只在被白的队友 3 秒内被打死、或一场两次以上时成时刻，其余 facts.display='summary'。
单独跑（在仓库根目录下，不加 -I，否则找不到 engine 包）：py -m engine.rules.util <demo 路径>
测试脚本：work/_scratch/rules-util/t1.py（打印命中）、t3.py（确定性 + 用词扫描）。
"""
import math
import sys
from collections import Counter

import numpy as np

from engine import common as C

TR = C.TR

# 道具：weapon_fire 的武器键 / 背包显示名 → 中文
NADE_WK = {'smokegrenade': '烟', 'flashbang': '闪', 'hegrenade': '雷', 'molotov': '火', 'incgrenade': '火'}
NADE_INV = {'Smoke Grenade': '烟', 'Flashbang': '闪', 'High Explosive Grenade': '雷', 'Molotov': '火',
            'Incendiary Grenade': '火'}
NADE_ORDER = ['烟', '闪', '火', '雷']                 # 道具清单的写法顺序
ORDER_ATK = ['闪', '火', '烟', '雷']                  # UTIL-01 进攻方 / B 分支：先扔什么
ORDER_DEF = ['烟', '火', '闪', '雷']                  # UTIL-01 防守方 A 分支
UTIL_KIND = {'smoke': '烟', 'flash': '闪', 'he': '雷', 'molotov': '火', 'incendiary': '火'}   # timeline user_util.kind
UTIL_DMG_WK = ('hegrenade', 'inferno', 'molotov', 'incgrenade')                           # player_hurt 里道具伤害
WAIT_WORD = {'闪': '等闪爆了', '火': '等火烧起来', '烟': '等烟起来', '雷': '等雷炸了'}


# ================================================================ 小工具
def _f(t):
    """句子里的秒数"""
    return C.fmt_sec(t)


def _nades_text(ns):
    """['闪', '烟', '闪'] → '烟、2 颗闪'"""
    c = Counter(ns)
    return '、'.join(k if c[k] == 1 else f'{c[k]} 颗{k}' for k in NADE_ORDER if c.get(k))


def _sorted_nades(ns):
    return sorted(ns, key=NADE_ORDER.index)


def _pick(left, order):
    """按 order 从身上的道具里挑第一颗"""
    return next((n for n in order if n in left), left[0] if left else '闪')


def _seg_dist(p, a, b):
    """点 p 到线段 ab 的 3D 距离"""
    p, a, b = (np.asarray(x, float)[:3] for x in (p, a, b))
    ab = b - a
    L = float(ab @ ab)
    if L == 0:
        return float(np.linalg.norm(p - a))
    s = max(0.0, min(1.0, float((p - a) @ ab) / L))
    return float(np.linalg.norm(a + s * ab - p))


def _ev(ctx, name):
    """道具类事件表（flashbang_detonate / smokegrenade_detonate / player_blind ……）：daoju 缓存的 ev，
    没有时退到 sight_s4 / sight；列里的 steamid 统一成字符串。"""
    memo = _memo(ctx)
    key = ('ev', name)
    if key in memo:
        return memo[key]
    df = None
    for cname in ('daoju', 'sight_s4', 'sight'):
        try:
            raw = ctx.cache(cname)
        except (FileNotFoundError, KeyError, OSError):
            continue
        v = raw.get('ev', {}).get(name) if isinstance(raw, dict) else None
        if v is not None and len(v):
            df = v.copy()
            break
    if df is None and name == 'player_blind':            # ticks 缓存顶层也有 blind
        try:
            df = ctx.cache('ticks').get('blind')
            df = df.copy() if df is not None else None
        except (FileNotFoundError, KeyError, OSError):
            df = None
    if df is None:
        import pandas as pd
        df = pd.DataFrame(columns=['tick'])
    for c in ('user_steamid', 'attacker_steamid'):
        if c in df:
            df[c] = df[c].map(C._sid_str)
    if len(df):
        df = df.sort_values(['tick'] + (['entityid'] if 'entityid' in df else []), kind='mergesort').reset_index(drop=True)
    memo[key] = df
    return df


def _memo(ctx):
    """本模块在这个 ctx 上的内存缓存"""
    m = getattr(ctx, '_util_memo', None)
    if m is None:
        m = {}
        try:
            setattr(ctx, '_util_memo', m)
        except AttributeError:
            pass
    return m


# ---------------------------------------------------------------- 背包（daoju 缓存，每 8 tick 一帧）
def has_c4(ctx, sid, tick):
    """tick 前最近一帧背包里有没有 C4（没有背包数据返回 False）"""
    memo = _memo(ctx)
    if 'c4' not in memo:
        out = {}
        try:
            T = ctx.cache('daoju')['ticks'][['tick', 'steamid', 'inventory']]
        except (FileNotFoundError, KeyError, OSError):
            T = None
        if T is not None:
            sids = T['steamid'].map(C._sid_str).to_numpy()
            ticks = T['tick'].to_numpy(np.int64)
            invs = T['inventory'].to_numpy(object)
            for i in np.lexsort((ticks, sids)):
                v = invs[i]
                out.setdefault(sids[i], ([], []))
                out[sids[i]][0].append(int(ticks[i]))
                out[sids[i]][1].append(bool(v is not None and 'C4 Explosive' in list(v)))
        memo['c4'] = {k: (np.array(a, dtype=np.int64), b) for k, (a, b) in out.items()}
    fr = memo['c4'].get(C._sid_str(sid))
    if not fr:
        return False
    i = int(np.searchsorted(fr[0], int(tick), side='right')) - 1
    return bool(i >= 0 and fr[1][i])


def _inv_index(ctx):
    """sid → (帧 tick 数组, 每帧身上的道具中文列表)"""
    memo = _memo(ctx)
    if 'inv' in memo:
        return memo['inv']
    out = {}
    try:
        T = ctx.cache('daoju')['ticks'][['tick', 'steamid', 'inventory']]
    except (FileNotFoundError, KeyError, OSError):          # 没有背包数据：靠背包的规则都不触发
        memo['inv'] = out
        return out
    sids = T['steamid'].map(C._sid_str).to_numpy()
    ticks = T['tick'].to_numpy(np.int64)
    invs = T['inventory'].to_numpy(object)
    order = np.lexsort((ticks, sids))
    cur, tk, iv = None, [], []
    for i in order:
        s = sids[i]
        if s != cur:
            if cur is not None:
                out[cur] = (np.array(tk, dtype=np.int64), iv)
            cur, tk, iv = s, [], []
        tk.append(int(ticks[i]))
        v = invs[i]
        iv.append(tuple(NADE_INV[x] for x in (v if v is not None else []) if x in NADE_INV))
    if cur is not None:
        out[cur] = (np.array(tk, dtype=np.int64), iv)
    memo['inv'] = out
    return out


def _throws(ctx, sid, rnd=None):
    """sid 扔出的道具（weapon_fire）：[{tick, round, n(中文), wk}]，按 tick 排；诱饵不算"""
    memo = _memo(ctx)
    key = ('throws', sid)
    if key not in memo:
        f = C.fires(ctx, sid=sid, guns_only=False)
        f = f[f['wk'].isin(list(NADE_WK))]
        memo[key] = [dict(tick=int(r.tick), round=int(r.round), n=NADE_WK[r.wk], wk=r.wk,
                          xyz=(float(r.user_X), float(r.user_Y), float(r.user_Z))) for r in f.itertuples()]
    th = memo[key]
    return th if rnd is None else [x for x in th if x['round'] == int(rnd)]


def _nades_at(ctx, sid, tick):
    """sid 在 tick 那一刻（之前）身上的道具：tick 前最近一帧背包，减去这一帧之后到 tick 之前扔出去的。
    背包比 weapon_fire 晚更新：帧前 inv_lag_ticks 内扔的，如果这一帧里数量还没减，也扣掉。"""
    idx = _inv_index(ctx).get(C._sid_str(sid))
    if idx is None:
        return []
    tks, invs = idx
    i = int(np.searchsorted(tks, int(tick) - 1, side='right')) - 1
    if i < 0:
        return []
    f_tick = int(tks[i])
    cnt = Counter(invs[i])
    lag = int(ctx.th['util']['inv_lag_ticks'])
    for t in _throws(ctx, sid):
        if not (f_tick - lag < t['tick'] < int(tick)):
            continue
        if t['tick'] > f_tick:
            cnt[t['n']] -= 1
        else:                                            # 帧前刚扔：看扔之前那一帧的数量减了没有
            j = int(np.searchsorted(tks, t['tick'] - 1, side='right')) - 1
            before = Counter(invs[j])[t['n']] if j >= 0 else 0
            if cnt[t['n']] >= before:
                cnt[t['n']] -= 1
    out = []
    for k in NADE_ORDER:
        out += [k] * max(0, cnt.get(k, 0))
    return out


# ---------------------------------------------------------------- 用户道具的落点（timeline user_util）
def _user_util(ctx, rnd):
    """用户这回合扔的每颗道具：weapon_fire 的扔出 tick 对上 timeline user_util（落点报点、起爆秒数、伤害）"""
    memo = _memo(ctx)
    key = ('uutil', int(rnd))
    if key in memo:
        return memo[key]
    tl = C.tl_round(ctx, rnd) or {}
    uu = sorted(tl.get('user_util', []), key=lambda u: (u.get('throw_tick') or 0, u.get('kind', '')))
    used = set()
    out = []
    for t in _throws(ctx, C.user_sid(ctx), rnd):
        best = None
        for j, u in enumerate(uu):
            if j in used or UTIL_KIND.get(u.get('kind')) != t['n'] or u.get('throw_tick') is None:
                continue
            d = int(u['throw_tick']) - t['tick']
            if -8 <= d <= 40 and (best is None or abs(d) < abs(int(uu[best]['throw_tick']) - t['tick'])):
                best = j
        u = uu[best] if best is not None else {}
        if best is not None:
            used.add(best)
        # 秒数用时间线里这颗道具的扔出 tick（故事线写的就是它），不用 weapon_fire 那一刻：
        # 同一颗火页面上写成 3.7 / 3.8 两个秒（8d R6、R14、R15）。key 仍按 weapon_fire 的 tick（和别的命中对得上）
        tl_tick = u.get('throw_tick') if isinstance(u.get('throw_tick'), int) else t['tick']
        lxy = u.get('land_xy')
        land_xyz = (float(lxy[0]), float(lxy[1]), float(u.get('land_z') or 0.0)) if lxy and len(lxy) >= 2 else None
        out.append(dict(t, tick=int(tl_tick), fire_tick=t['tick'], land=u.get('land_place'), det_t=u.get('det_t'),
                        damage=list(u.get('damage') or []), key=f"util:{int(rnd)}:{t['tick']}", land_xyz=land_xyz))
    memo[key] = out
    return out


def _plant_tick(ctx, rnd):
    p = (C.tl_round(ctx, rnd) or {}).get('plant')
    return int(p['tick']) if p and p.get('tick') is not None else None


def _path3(ctx, sid, t0, t1):
    """[t0, t1] 内的路程（每 move_window_ticks 采一点，瞬移段不算）。
    复核修正：只算平面路程（rules_v1 B 分支原来用 3D；从箱子上跳下来的高度差被算成“往外走”，51920 R22 36.2
    原地守 A 点被写成“往外走时”）。名字沿用 _path3，口径见 COMPOSE_NOTES 第八轮复核修正"""
    thc = ctx.th['common']
    w = int(thc['move_window_ticks'])
    pts = [C.pos(ctx, sid, t) for t in range(int(t0), int(t1) + 1, w)]
    tot = 0.0
    for a, b in zip(pts, pts[1:]):
        s = C.dist2(a, b)
        if np.isfinite(s) and s <= thc['move_teleport'] * w / TR:
            tot += s
    return tot


def _spot_ticks(ctx, viewer, targets, t0, t1):
    """[t0, t1] 内 viewer 看见 targets 里任一活人的帧 tick（targets 的 approximate_spotted_by 里有 viewer）。
    common 只有 spots()（返回第一帧），交火链要每一帧，所以这里直接读 common 建好的 spotted 位掩码（只读）。"""
    d = C._data(ctx)
    viewer = C._sid_str(viewer)
    if viewer not in d.col:
        return np.array([], dtype=np.int64)
    i0, i1 = d.frange(t0, t1)
    if i1 <= i0:
        return np.array([], dtype=np.int64)
    tk = d.ticks[i0:i1]
    rnd = C.round_of(ctx, t1)
    ok = np.zeros(len(tk), dtype=bool)
    for s in sorted(targets):
        if s not in d.col:
            continue
        m = (d.spot[i0:i1, d.col[s]] & d.bit[viewer]) != 0
        dt = C.death_tick(ctx, s, rnd) if rnd else None
        if dt is not None:
            m &= tk < dt
        ok |= m
    dv = C.death_tick(ctx, viewer, rnd) if rnd else None
    if dv is not None:
        ok &= tk < dv
    return tk[ok]


def _zone(ctx, place):
    th = ctx.th['util']
    if place in th['a_zone']:
        return 'A'
    if place in th['b_zone']:
        return 'B'
    return None


# ---------------------------------------------------------------- Hit


def _tidy(ctx, s):
    """句子排版：common.tidy（人名整体保护）"""
    return C.tidy(s, C.player_names(ctx))


def _hit(ctx, rule, kind, rnd, tick, subject, blame, facts, text, alt, kb, evidence,
         death_tick=None, death_sid=None, nade_keys=(), scope='moment'):
    U = C.user_sid(ctx)
    text, alt, evidence = _tidy(ctx, text), _tidy(ctx, alt), _tidy(ctx, evidence)
    t = C.t_of(ctx, tick, rnd)
    facts = dict(facts)
    facts.setdefault('t', C.sec(t))
    return dict(
        rule=rule, kind=kind, round=int(rnd), t=C.sec(t), tick=int(tick), scope=scope,
        subject=C.name_of(ctx, subject), is_user=(C._sid_str(subject) == U), blame=blame,
        death_t=C.sec(C.t_of(ctx, death_tick, rnd)) if death_tick is not None else None,
        death_of=C.name_of(ctx, death_sid) if death_sid is not None else None,
        merge_key=None, facts=facts, text=text, alt=alt, kb=list(kb), evidence=evidence,
        _nades=sorted(set(nade_keys)),
    )


# ================================================================ UTIL-01 带着道具进死局
def _combat_ticks(ctx, rnd, t_end):
    """用户自己能感知的交火事件（tick，排好序）：自己开枪（枪械）、被打、打中人、自己看见活着的敌人、
    被闪白 >= u01_blind_s 秒的整段白屏（每 u01_blind_step_ticks 记一点）"""
    th = ctx.th['util']
    U = C.user_sid(ctx)
    ri = C.round_info(ctx, rnd)
    ev = set()
    f = C.fires(ctx, rnd, sid=U)
    ev |= set(int(x) for x in f['tick'] if x <= t_end)
    h = C.hurts(ctx, rnd)
    h = h[h['tick'] <= t_end]
    ev |= set(int(x) for x in h[(h['vic'] == U) & (~h['att'].isin(['nan', U]))]['tick'])
    ev |= set(int(x) for x in h[(h['att'] == U) & (h['vic'] != U)]['tick'])
    ev |= set(int(x) for x in _spot_ticks(ctx, U, C.enemies(ctx, rnd), ri['start'], t_end))
    b = _ev(ctx, 'player_blind')
    if len(b):
        b = b[(b['user_steamid'] == U) & (b['tick'] >= ri['start']) & (b['tick'] <= t_end)]
        step = int(th['u01_blind_step_ticks'])
        for r in b.itertuples():
            dur = float(r.blind_duration)
            if dur < th['u01_blind_s']:
                continue
            end = min(int(t_end), int(r.tick) + int(round(dur * TR)))
            ev |= set(range(int(r.tick), end + 1, step)) | {end}
    return sorted(x for x in ev if ri['start'] <= x <= t_end)


GRENADE_WK = ('flashbang', 'smokegrenade', 'hegrenade', 'molotov', 'incgrenade', 'decoy')
U01_CONTRA = ('POS-05', 'TIME-05', 'TIME-07', 'POST-03', 'POST-01', 'TEAM-07', 'TEAM-04')   # 手册 UTIL-A 第 5 道门（merge 用）
U01_FACT_ONLY = ('TIME-05', 'POS-05')       # 这两条主句下只留“道具都在身上”的事实半句（facts.clause_fact），不给还可以


def _u01_log(ctx, rnd, **kw):
    """UTIL-01 每回合的判定记录（给 match_stats / summary 用）：用户阵亡时身上有道具的回合才记"""
    _memo(ctx).setdefault('u01_log', {})[int(rnd)] = kw


def _duel_row(ctx, dt):
    """base/user_duels.csv 里这次阵亡（结果=输、击杀 tick 对上）那一行：(开打 tick, 局势, 对枪方式)；没有返回 None"""
    memo = _memo(ctx)
    if 'duels' not in memo:
        rows = []
        try:
            import pandas as pd
            d = pd.read_csv(ctx.path('base/user_duels.csv'), encoding='utf-8-sig')
            d = d[d['结果'] == '输']
            for r in d.itertuples():
                eng = getattr(r, '开打tick')
                rows.append((int(r.tick), int(eng) if eng == eng else int(r.tick), str(getattr(r, '局势')),
                             str(getattr(r, '对枪方式'))))
        except (FileNotFoundError, OSError, KeyError, ValueError):
            rows = []
        memo['duels'] = sorted(rows)
    best = None
    for tk, eng, ph, mode in memo['duels']:
        if abs(tk - int(dt)) <= 2 and (best is None or abs(tk - int(dt)) < abs(best[0] - int(dt))):
            best = (tk, eng, ph, mode)
    return None if best is None else best[1:]


def _setup_place(ctx, rnd):
    """CT 本回合开局站的位置（冻结结束后 10–25 秒最常出现的报点；和 base 局势“守点 / 转点”同一口径）"""
    th = ctx.th['util']
    U = C.user_sid(ctx)
    ri = C.round_info(ctx, rnd)
    a, b = th['u01_setup_from_s'], th['u01_setup_to_s']
    c = Counter(p for p in (C.place_at(ctx, U, t) for t in range(ri['start'] + int(a * TR), ri['start'] + int(b * TR), 32)) if p)
    return sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if c else None


def _phase(ctx, rnd, eng, side, planted, row):
    """局势：base 的 user_duels 有这一行就用它的；没有时按同一口径自己算（CT：守点 / 转点/补防 / 回防；T：进点 / 守包 / 其他）"""
    if row is not None and row[1] not in ('nan', 'None', ''):
        return row[1]
    th = ctx.th['util']
    U = C.user_sid(ctx)
    if side == 'CT':
        if planted:
            return '回防'
        sp = _setup_place(ctx, rnd)
        return '守点' if sp in (None, C.place_at(ctx, U, eng)) else '转点/补防'
    if planted:
        return '守包'
    return '进点' if C.place_at(ctx, U, eng) in th['u01_site_places'] else '其他'


def _bomb_xyz(ctx, rnd):
    tl = C.tl_round(ctx, rnd) or {}
    p = tl.get('plant')
    if not p or p.get('tick') is None:
        return None
    sid = C.sid_of(ctx, p.get('planter')) if p.get('planter') else None
    if sid is not None:
        return C.pos(ctx, sid, int(p['tick']))
    xy = p.get('xy') or [None, None]
    return None if xy[0] is None else np.array([float(xy[0]), float(xy[1]), 0.0])


def _retake_util(ctx, rnd, t_end):
    """回防：下包后到 t_end 你往包点（离包 3D ≤ u01_retake_r）扔的道具"""
    th = ctx.th['util']
    if C.user_side(ctx, rnd) != 'CT':
        return []
    plant = _plant_tick(ctx, rnd)
    bomb = _bomb_xyz(ctx, rnd)
    if plant is None or bomb is None:
        return []
    out = []
    for x in _user_util(ctx, rnd):
        if not (plant < x['fire_tick'] <= t_end):
            continue
        lx = x.get('land_xyz')
        if lx is None:
            continue
        d = C.dist3(lx, bomb) if bomb[2] != 0.0 else C.dist2(lx, bomb)
        if d <= th['u01_retake_r']:
            out.append(x)
    return out


def _main_active(ctx, rnd, t0, t1, solo_sid):
    """[t0, t1] 内除你以外的队友（人多那边）有没有交火：开枪、挨敌人打 / 打中敌人、击杀或阵亡"""
    mates = set(C.mates(ctx, rnd)) - {solo_sid}
    if not mates:
        return False
    f = C.fires(ctx, rnd)
    if len(f[(f['tick'] >= t0) & (f['tick'] <= t1) & f['sid'].isin(sorted(mates))]):
        return True
    h = C.hurts(ctx, rnd)
    h = h[(h['tick'] >= t0) & (h['tick'] <= t1)]
    en = set(C.enemies(ctx, rnd))
    if len(h[(h['att'].isin(sorted(mates)) & h['vic'].isin(sorted(en))) | (h['vic'].isin(sorted(mates)) & h['att'].isin(sorted(en)))]):
        return True
    d = C.deaths(ctx, rnd)
    d = d[(d['tick'] >= t0) & (d['tick'] <= t1)]
    return bool(len(d[d['att'].isin(sorted(mates)) | d['vic'].isin(sorted(mates))]))


def _u01_gates(ctx, rnd, dt, fight, killer, left, us, them, side, planted):
    """手册 UTIL-A 的五道门（第 5 道“主句不打架”在 merge 里做，这里只给 facts.contra_rules）：
    1 主动（开打前 2 秒 3D 路程够、接触时你在动，不是原地架着被拉出来）
    2 有目标（凶手或他身边的人之前露过面 / 开过枪 / 刚打死队友 / 扔过道具，或者是进点、回防）
    3 没人给过道具（你没在扔道具、死前几秒没扔过；队友的闪没在你或凶手身边爆、凶手没被我方闪着）
    4 局面（不是 eco、1vN、人数差 2 个以上、T 下包后、单走一侧还没等到人多那边开打、转点的狙、远处对枪身上没烟、回防已经用了道具）
    返回 (gates, info)；gates 每项 True = 过了这道门，info 是写句子 / 定责任用的"""
    th = ctx.th['util']
    U = C.user_sid(ctx)
    ri = C.round_info(ctx, rnd)
    row = _duel_row(ctx, dt)
    eng = None
    if row is not None and fight - 10 * TR <= row[0] <= dt:
        eng = int(row[0])
    if eng is None:
        cands_t = []
        if killer is not None:
            for a, b in ((killer, U), (U, killer)):
                x = C.spots(ctx, a, b, dt - int(th['u01_eng_look_s'] * TR), dt)
                if x is not None:
                    cands_t.append(int(x))
        eng = min(cands_t) if cands_t else max(int(fight), int(dt) - TR)
    eng = max(int(ri['start']), min(int(eng), int(dt)))
    my_team = C.team_of(ctx, U, rnd)
    mates = C.mates(ctx, rnd)
    # ---- 1 主动
    # 接触那一刻的速度不能当“在动”：拉出去的人开枪前会急停（51920 R12、a32 R3 开打那一帧都停住了）。
    # 原地架着 = 开打前一直站着不动 >= u01_hold_s 秒，或者开打前 u01_init_s 秒 3D 路程不到 u01_init_path_2s
    path2 = _path3(ctx, U, eng - int(th['u01_init_s'] * TR), eng)
    still = 0
    for tt in range(eng, eng - int(th['u01_hold_s'] * TR) - 1, -8):
        v = C.move_speed(ctx, U, tt)
        if v is None or v > th['u01_moving_ups']:
            break
        still += 8
    holding = still >= int(th['u01_hold_s'] * TR)
    moving = not holding
    g_init = path2 >= th['u01_init_path_2s'] and not holding
    # ---- 2 有目标
    phase = _phase(ctx, rnd, eng, side, planted, row)
    known_by = []
    cands = []
    if killer is not None:
        kp_dt = C.pos(ctx, killer, dt)
        cands = [killer] + [e for e in C.alive_list(ctx, rnd, dt, enemy=True)
                            if e != killer and C.dist3(C.pos(ctx, e, dt), kp_dt) <= th['u01_known_near']]
        t0 = max(int(ri['start']), int(dt) - int(th['u01_known_look_s'] * TR))
        t1 = eng - int(th['u01_known_lead_s'] * TR)
        if t1 > t0:
            if any(C.on_radar(ctx, e, t0, t1) for e in cands):
                known_by.append('seen')
            if len(C.heard_shots(ctx, U, t0, t1, shooters=cands)):
                known_by.append('heard')
            dd = C.deaths(ctx, rnd)
            if len(dd[(dd['tick'] >= eng - int(th['u01_known_kill_s'] * TR)) & (dd['tick'] <= eng)
                      & dd['att'].isin(cands) & dd['vic'].isin(mates)]):
                known_by.append('mate_kill')
            for name in ('flashbang_detonate', 'smokegrenade_detonate', 'hegrenade_detonate', 'inferno_startburn'):
                e = _ev(ctx, name)
                if not len(e) or 'user_steamid' not in e:
                    continue
                e = e[(e['tick'] >= t0) & (e['tick'] <= t1) & e['user_steamid'].isin(cands)]
                if any(C.dist3(C.pos(ctx, U, int(r.tick)), (float(r.x), float(r.y), float(r.z))) <= th['u01_util_near']
                       for r in e.itertuples()):
                    known_by.append('enemy_util')
                    break
    known_pos = any(k in known_by for k in ('seen', 'heard', 'mate_kill'))   # 对面的道具只说明有人，不算知道凶手在哪
    # 手册 UTIL-A 第 2 道门：进点 / 回防只在凶手站在包点常规位（u01_site_places）时算“位置能预判”。
    # 附录 B：第 3 场 R17 78.9 对手1 蹲在 A 二楼、之前没露过面，过不了这道门（局势虽然是进点）
    if phase in ('进点', '回防') and killer is not None and C.place_at(ctx, killer, dt) in set(th['u01_site_places']):
        known_by.append(phase)
    g_known = bool(known_by)
    # ---- 3 没人给过道具
    inhand = any(C.wkey(C.weapon_at(ctx, U, t) or '') in GRENADE_WK
                 for t in range(eng - int(th['u01_init_s'] * TR), int(dt) + 1, 8))
    recent = any(eng - int(th['u01_inuse_s'] * TR) <= x['tick'] <= int(dt) for x in _throws(ctx, U, rnd))
    mateflash = False
    fl = _ev(ctx, 'flashbang_detonate')
    if len(fl):
        ff = fl[(fl['tick'] >= int(dt) - int(th['u01_mateflash_s'] * TR)) & (fl['tick'] <= int(dt))]
        for r in ff.itertuples():
            if r.user_steamid == U or C.team_of(ctx, r.user_steamid, rnd) != my_team:
                continue
            xyz = (float(r.x), float(r.y), float(r.z))
            if C.dist3(C.pos(ctx, U, int(r.tick)), xyz) <= th['u01_mateflash_r3d'] or (
                    killer is not None and C.dist3(C.pos(ctx, killer, int(r.tick)), xyz) <= th['u01_mateflash_r3d']):
                mateflash = True
                break
    k_blind = False
    if killer is not None:
        bl = _ev(ctx, 'player_blind')
        if len(bl) and 'attacker_steamid' in bl:
            b = bl[(bl['user_steamid'] == killer) & (bl['tick'] <= int(dt))]
            for r in b.itertuples():
                if C.team_of(ctx, r.attacker_steamid, rnd) != my_team or float(r.blind_duration) < th['u01_killer_blind_s']:
                    continue
                if int(r.tick) + int(float(r.blind_duration) * TR) >= eng - int(th['u01_mateflash_s'] * TR):
                    k_blind = True
                    break
    g_free = not (inhand or recent or mateflash or k_blind)
    # ---- 4 局面
    why4 = []
    if C.buy(ctx, rnd, 'us') == 'eco':
        why4.append('eco')
    if us <= 1 and them >= 2:
        why4.append('1vN')
    if them - us >= th['u01_big_gap']:
        why4.append('gap')
    if side == 'T' and planted:
        why4.append('t_planted')
    # 单走一侧（拍板第六轮 3；Q19 CT 也适用、Q20 窗口 5 秒）
    alive_m = [m for m in mates if C.alive(ctx, m, eng, rnd)]
    near_m = min((C.dist3(C.pos(ctx, m, eng), C.pos(ctx, U, eng)) for m in alive_m), default=None)
    # 只有另一边真有“人多的那半边”（除你以外还活着 >= u01_lurk_main_n 人）才算单走一侧（8d R3：2v3 只剩一个队友，不是单走）
    solo = near_m is not None and near_m >= th['u01_lurk_r'] and len(alive_m) >= th['u01_lurk_main_n']
    if solo and not _main_active(ctx, rnd, eng - int(th['u01_lurk_wait_s'] * TR), eng, U):
        why4.append('solo_wait')
    # Q8：CT 拿狙转点，路上没扔拖延道具不算这个位置的问题
    if side == 'CT' and not planted and phase == '转点/补防' and C.is_sniper(C.weapon_at(ctx, U, eng) or ''):
        why4.append('awp_rotate')
    kd3 = C.dist3(C.pos(ctx, killer, eng), C.pos(ctx, U, eng)) if killer is not None else None
    far = kd3 is not None and kd3 >= th['u01_far_3d']
    if far and '烟' not in left:
        why4.append('far')
    # 复核修正：隔着烟被打死（击杀栏 thrusmoke，或他打中你的每一下都隔着烟）——闪帮不上，他也看不见你（461d R9 25.8）
    if killer is not None:
        kr = C.deaths(ctx, rnd)
        kr = kr[(kr['vic'] == U) & (kr['tick'] == int(dt))]
        hk = C.hurts(ctx, rnd)
        hk = hk[(hk['vic'] == U) & (hk['att'] == killer) & (hk['tick'] <= int(dt)) & (hk['tick'] >= eng - int(th['u01_init_s'] * TR))]
        if (len(kr) and 'thrusmoke' in kr.columns and bool(kr['thrusmoke'].iloc[0])) or (
                len(hk) and all(C.smoked(ctx, U, killer, int(tk)) for tk in hk['tick'])):
            why4.append('smoke')
        # 复核修正（审核单第 7 条 / Q15）：进点时凶手 u01_traded_s 秒内被队友补掉 = 进点被换了，不再写“到死都没扔”（bd R29 74.9）；
        # 只管进点（bd R12 B 二楼往外走被换掉，审核单第 3 条例子照样算）
        kd = C.deaths(ctx, rnd)
        kd = kd[(kd['vic'] == killer) & (kd['tick'] > int(dt)) & (kd['tick'] <= int(dt) + int(th['u01_traded_s'] * TR))]
        if phase == '进点' and any(a in mates for a in kd['att']):
            why4.append('traded')
    retake = _retake_util(ctx, rnd, int(dt)) if side == 'CT' and planted else []
    if len(retake) >= th['u01_retake_util_n']:
        why4.append('retake_used')
    g_sit = not why4
    # 进点第一个进（P13：封顶小问题）
    first_in = False
    if phase == '进点':
        site = set(th['u01_site_places'])
        first_in = not any(C.place_at(ctx, m, eng) in site for m in alive_m)
    us_eng, them_eng = C.alive_counts(ctx, eng)
    gates = dict(init=g_init, known=g_known, free=g_free, situation=g_sit)
    info = dict(eng=eng, phase=phase, known_by=known_by, known_pos=known_pos, path2=round(path2), moving=moving,
                inhand=inhand, recent_throw=recent, mateflash=mateflash, killer_blind=k_blind, why4=why4,
                solo=solo, far=far, killer_d3=kd3, first_in=first_in, retake=retake, us_eng=us_eng, them_eng=them_eng)
    return gates, info


def _u01(ctx, rnd, u06):
    """UTIL-01：阵亡时身上还有道具，死前那波交火前有空当，并且人数落后（A）/ 主动走出去（B）/ 对面道具刚从你架的口过来（C），
    再过手册 UTIL-A 的五道门（_u01_gates）才写“到死都没扔”。返回 hit 或 None；每回合的判定记在 _memo['u01_log']"""
    th = ctx.th['util']
    U = C.user_sid(ctx)
    ri = C.round_info(ctx, rnd)
    dt = C.death_tick(ctx, U, rnd)
    if dt is None or dt > ri['end']:                       # 没死 / 回合结束后才死
        return None
    left = _nades_at(ctx, U, dt)
    if not left:
        return None
    log = dict(death_t=C.sec(C.t_of(ctx, dt, rnd)), nades=list(left), has_flash='闪' in left, passed=False)
    # 同一颗烟已由 UTIL-06 报过：这条不出，事实并进 UTIL-06
    if u06 is not None and '烟' in left and not any(
            x['n'] == '烟' and u06['tick'] <= x['tick'] < dt for x in _throws(ctx, U, rnd)):
        u06['facts']['util01_merged'] = dict(death_t=C.sec(C.t_of(ctx, dt, rnd)), nades=left)
        _u01_log(ctx, rnd, **log, pre='u06')
        return None
    # ---- 死前那波交火
    ev = _combat_ticks(ctx, rnd, dt)
    gap = th['u01_gap_s'] * TR
    cur = dt
    for x in reversed(ev):
        if x >= cur - gap:
            cur = min(cur, x)
        else:
            break
    dth = C.deaths(ctx, rnd)
    kills = sorted(int(k) for k in dth[(dth['att'] == U) & (dth['vic'] != U)]['tick'] if cur <= k < dt)
    if kills:                                              # 这一波里自己杀过人：从击杀之后重算
        nxt = [x for x in ev if x > kills[-1]]
        cur = nxt[0] if nxt else dt
    fight = int(cur)
    prev = [x for x in ev if x < fight]
    quiet_from = prev[-1] if prev else ri['start']
    quiet = (fight - quiet_from) / TR
    ft = C.t_of(ctx, fight, rnd)
    side = C.user_side(ctx, rnd)
    plant = _plant_tick(ctx, rnd)
    planted = plant is not None and plant < fight
    us, them = C.alive_counts(ctx, fight)
    killer = None
    drow = dth[(dth['vic'] == U) & (dth['tick'] == dt)]
    if len(drow) and drow.iloc[0]['att'] not in ('nan', U) and C.is_enemy(ctx, U, drow.iloc[0]['att'], rnd):
        killer = drow.iloc[0]['att']
    # 五道门先算（回合判定记录里要有，即使前面的条件没过）
    gates, gi = _u01_gates(ctx, rnd, dt, fight, killer, left, us, them, side, planted)
    log.update(gates=gates, why4=gi['why4'], known_by=gi['known_by'], phase=gi['phase'],
               retake_n=len(gi['retake']))
    if quiet < th['u01_quiet_min_s']:
        _u01_log(ctx, rnd, **log, pre='no_gap')
        return None
    A = us < them
    path = _path3(ctx, U, fight - int(th['u01_move_window_s'] * TR), fight)
    moved = path >= th['u01_move_units']
    B = moved and ft >= th['u01_move_min_t']
    # C：死前 10 秒内对面一颗闪 / 烟在你 1500 内爆开，爆开后 5 秒内你的准星水平方向对着它
    cu = None
    my_team = C.team_of(ctx, U, rnd)
    for name, nn in (('flashbang_detonate', '闪'), ('smokegrenade_detonate', '烟')):
        e = _ev(ctx, name)
        if not len(e):
            continue
        e = e[(e['tick'] >= dt - int(th['u01_util_look_s'] * TR)) & (e['tick'] <= dt)]
        for r in e.itertuples():
            if C.team_of(ctx, r.user_steamid, rnd) in (my_team, 0):
                continue
            xyz = (float(r.x), float(r.y), float(r.z))
            if C.dist3(C.pos(ctx, U, int(r.tick)), xyz) >= th['u01_util_near']:
                continue
            t1 = min(dt, int(r.tick) + int(th['u01_util_face_s'] * TR))
            if any(C.facing_deg(ctx, U, tt, xyz) <= th['u01_util_face_deg'] for tt in range(int(r.tick), t1 + 1, 8)):
                if cu is None or int(r.tick) > cu['tick']:
                    cu = dict(tick=int(r.tick), n=nn, place=C.nearest_place(ctx, xyz, rnd),
                              src=C.place_at(ctx, str(r.user_steamid), int(r.tick)))
    Cb = cu is not None
    if not (A or B or Cb):
        _u01_log(ctx, rnd, **log, pre='no_branch')
        return None
    if not all(gates.values()):
        _u01_log(ctx, rnd, **log, pre='gates')
        return None
    if Cb and not (A or B) and _path3(ctx, U, fight - int(th['u01_move_window_s'] * TR), fight) < th['u01_move_units']:
        _u01_log(ctx, rnd, **log, pre='c_hold')          # C 分支一直在原地架着：P02 原地架着不判
        return None
    log['passed'] = True
    attacker = (side == 'T') != planted                    # 进攻方：T 下包前、CT 下包后回防
    branch = 'defA' if (A and not attacker) else ('atk' if (A or B) else 'C')
    # ---- 场景
    place_now = C.place_at(ctx, U, fight)
    place_before = C.place_at(ctx, U, fight - int(th['u01_move_window_s'] * TR))
    pcn_now = C.place_cn(place_now)
    # 对面道具是从哪个方向来的：落点就在你脚下（闪落在你站的地方）时看扔的人在哪；还是你自己的位置就不写（e6b R9：
    # 闪落在你站的中远，原来写“往中远回一颗火”）
    util_place = None
    if cu:
        for pl_ in (cu['place'], cu.get('src')):
            if pl_ and pl_ != place_now and C.place_cn(pl_, default='') and C.place_cn(pl_) != pcn_now:
                util_place = C.place_cn(pl_)
                break
    alive_e = C.alive_list(ctx, rnd, fight, enemy=True)
    # “最近半分钟一直没露过面”：全队雷达 + 击杀栏（P1）；正在和你对枪的人（互相开枪/打中、凶手）不算
    look0 = fight - int(th['u01_unseen_look_s'] * TR)
    hh = C.hurts(ctx, rnd)
    hh = hh[(hh['tick'] >= fight - TR) & (hh['tick'] <= dt)]
    dueling = set(hh[hh['att'] == U]['vic']) | set(hh[hh['vic'] == U]['att']) | ({killer} if killer else set())
    dueling |= {x['target'] for x in C.shots(ctx, U) if x['round'] == rnd and fight - TR <= x['tick'] <= dt and x['target']}
    unseen = []
    for e in alive_e:
        if e in dueling:
            continue
        lr = C.last_revealed(ctx, e, fight + 1)
        if lr is None or lr < look0:
            unseen.append(e)
    pu_dt, pu_f = C.pos(ctx, U, dt), C.pos(ctx, U, fight)
    tradable, coming = [], []
    for m in C.mates(ctx, rnd):
        if killer is not None and C.alive(ctx, m, dt, rnd):
            if C.dist3(C.pos(ctx, m, dt), pu_dt) <= th['u01_trade_3d'] and C.seen_window(
                    ctx, m, killer, dt - int(th['u01_trade_see_back_s'] * TR), dt + int(th['u01_trade_see_fwd_s'] * TR)):
                tradable.append(m)
        if C.alive(ctx, m, fight, rnd):
            d_now = C.dist3(C.pos(ctx, m, fight), pu_f)
            d_before = C.dist3(C.pos(ctx, m, fight - int(th['u01_coming_s'] * TR)), pu_f)
            if d_before - d_now > th['u01_coming_units']:
                coming.append((d_now, m))
    coming = [m for _, m in sorted(coming)]
    mate = coming[0] if coming else None
    thrown = []                                            # 第 ft 秒之前 10 秒内自己扔过的道具（“扔完火后”）
    # 只算第 ft 秒之前扔的：句子说的是“第 ft 秒时扔完了”，之后才扔的不能写进去（8d R19：32.8 秒扔火，句子写 32.1 秒）
    for x in _throws(ctx, U, rnd):
        if fight - int(th['u01_thrown_look_s'] * TR) <= x['tick'] <= fight and x['n'] not in thrown:
            thrown.append(x['n'])
    # 句子写的是第 ft 秒那一刻身上有什么、到死都没扔：取那一刻身上有、死时也还在的（128 R10：烟是之后才捡的）
    at_f = Counter(_nades_at(ctx, U, fight))
    both = at_f & Counter(left)
    if both:
        left = [k for k in NADE_ORDER for _ in range(both.get(k, 0))]
    # 扔掉后又捡了同一种：“扔完雷后身上还有雷”读着像写错了（128 R15），扔过的这一种不写进“扔完…后”
    thrown = [n for n in thrown if n not in left]
    # ---- 句子
    if A:
        sit = C.alive_str(us, them)
    elif B:
        if pcn_now == '某处':
            sit = '往外走'
        elif place_now != place_before:
            sit = f'往{pcn_now}走'
        else:
            sit = f'在{pcn_now}往外走'               # 一直在同一个地方，不写“往那里走”（128 R22）
    else:
        sit = f'对面的道具刚从{util_place}过来' if util_place else '对面的道具刚扔到你这边'
    q = int(math.floor(quiet + 1e-9))
    # 空当只按你自己的交火算；这段时间击杀栏里有人倒下（别人在打）时，不能写成“没交火”，写“你自己没和人交过火”（635 R28）
    feed = [int(x) for x in dth['tick'] if quiet_from < int(x) < fight]
    me_only = '你自己' if feed else ''
    # 之前只是被雷炸到、看到过人、被闪过，没有一次枪战（自己开枪 / 被枪打 / 用枪打中人）：也算“一直没交过火”（8d R19：12.5 秒吃了一颗雷）
    fu = C.fires(ctx, rnd, sid=U)
    hgun = C.hurts(ctx, rnd)
    hgun = hgun[(hgun['tick'] < fight) & ((hgun['vic'] == U) | (hgun['att'] == U)) & (hgun['att'] != hgun['vic'])]
    gun_prev = bool((fu['tick'] < fight).any()) or any(C.is_gun(w) for w in hgun['wk'])
    if not gun_prev:
        prev = []
    if not prev:
        # 只有开局到现在你一次都没交过火才写“一直”（128 R20：7–14 秒打过一波，后面空了 26 秒）
        gap_txt = f'{me_only}前面一直没和人交过火' if feed else '前面一直没交火'
    else:
        # 不写空当几秒（用户写作规则：不写延迟秒数），门槛仍按 quiet 秒数判
        gap_txt = (f'{me_only}有一段时间没和人交过火' if feed else '前面有一段时间没交火')
    # 过了门 1（不是原地架着）、门 3（没在扔道具）才会走到这里：不再有“一直在原地架着 / 在原地接枪”
    if branch == 'C':
        act = '还在往前走'
    elif tradable or us <= 1:
        act = '出去接枪'                              # 你已经是最后一个人时不写“一个人”（8d R10、128 R10）
    else:
        act = '一个人出去接枪'
    text = f'第 {_f(ft)} 秒{sit}时，你'
    if thrown:
        text += f'扔完{"和".join(thrown)}后'
    text += f'身上还有{_nades_text(left)}，{gap_txt}，到死都没扔，{act}'
    # “还有 N 个对手最近半分钟一直没露过面”不写：挂在句尾不说为什么要紧，读着像雷达信息（bd 8 个回合）；只留在 facts
    if mate:
        text += f'；{C.name_of(ctx, mate)} 正在往你这边来'
    # ---- 还可以：按道具写（手册 UTIL-B / 审核单第 26 条）：闪抢露头、烟切线、火卡近处、雷打血；
    # 火和雷不写“扔了再出”，闪不写“拖时间”；拖时间只给烟、火，并且要有具体报点，没有报点就不写
    kb = []
    radar_place = None
    eng = gi['eng']
    kplace = C.place_at(ctx, killer, eng) if killer is not None else None
    kcn = C.place_cn(kplace) if kplace else '某处'
    kcn = None if kcn in ('某处', pcn_now) else kcn
    k_still = killer is not None and (C.move_speed(ctx, killer, eng) or 0.0) <= th['u01_moving_ups']
    near_fire = gi['killer_d3'] is not None and gi['killer_d3'] < th['u01_fire_near_3d'] and k_still
    alt_kind = None
    if branch == 'defA':
        x = next((n for n in ('烟', '火') if n in left), None)
        cands = []
        for e, lt, pl in C.radar_enemies(ctx, fight, look_s=th['u01_radar_look_s']):
            if e in alive_e and pl and pl != place_now and C.place_cn(pl) != '某处':
                cands.append((-lt, e, pl))
        if cands:
            radar_place = C.place_cn(sorted(cands)[0][2])
        if x and radar_place:
            alt = f'还可以先往{radar_place}扔{x}，自己不露头拖时间'
            if mate:
                alt += f'，等{C.name_of(ctx, mate)}到了一起打'
            alt_kind = 'delay'
        else:
            alt = ''
        if side == 'CT':
            z = _zone(ctx, place_now)
            kb = []
        throw_n = x
    elif branch == 'atk':
        if pcn_now == '某处':
            where = '出去之前'
        elif place_now != place_before:
            where = f'进{pcn_now}前'
        else:
            where = f'在{pcn_now}往外走之前'
        alt, throw_n = '', None
        if '闪' in left and not gi['far']:
            throw_n, alt_kind = '闪', 'flash'
            alt = f'还可以{where}先扔一颗闪，等闪爆了' + (f'和 {C.name_of(ctx, mate)} 一起出' if mate else '再出')
        elif '烟' in left and kcn:
            throw_n, alt_kind = '烟', 'smoke_line'
            alt = f'还可以先用烟挡住{kcn}那条线再过'
        elif '火' in left and kcn and near_fire and not gi['far']:
            throw_n, alt_kind = '火', 'fire_near'
            alt = f'还可以先往{kcn}扔火逼他挪位置'
        if side == 'CT' and planted:
            kb = []
        elif side == 'CT' and place_now == 'Connector':
            kb = []
        elif side == 'T' and place_now in ('Palace', 'PalaceInterior'):
            kb = []
        elif side == 'T' and place_now in ('Middle', 'TopofMid'):
            kb = []
        elif side == 'T' and place_now in ('BombsiteA', 'BombsiteB'):
            kb = []
    else:
        # C：对面道具刚从 util_place 过来。还在往前走（再出）只给闪 / 烟；原地架着（再架）可以回雷、火打血卡口
        alt, throw_n = '', None
        if util_place:
            if moved:
                if '闪' in left:
                    throw_n, alt = '闪', f'还可以先往{util_place}回一颗闪再出'
                elif '烟' in left:
                    throw_n, alt = '烟', f'还可以先用烟挡住{util_place}那条线再过'
            else:
                throw_n = next((n for n in ('雷', '火') if n in left), None)
                alt = f'还可以先往{util_place}回一颗{throw_n}再架' if throw_n else ''
        kb = []
    # ---- 责任（过了门以后：身上有闪且对面位置已知 → 你的责任，其余小问题；进点第一个进的封顶小问题）
    death_cost = False
    if branch == 'C':
        kind, blame = '思路', '—'
    else:
        kind = '问题'
        # 远处对枪闪帮不上（P12）：只有闪能用上、对面位置也知道时才是你的责任
        blame = '你的责任' if ('闪' in left and gi['known_pos'] and not gi['far']) else '小问题'
        if gi['phase'] == '进点' and gi['first_in']:
            blame = '小问题'                            # P13：进点第一个进的
        if gi['us_eng'] - gi['them_eng'] >= th['u01_ahead_cap']:
            blame = '小问题'                            # P01：人多时问题在出去本身（POS-05），道具只记小问题（51920 R22 5v3）
    quiet_from_t = C.t_of(ctx, quiet_from, rnd)
    if not prev:
        ev_txt = f'死前身上还有{_nades_text(left)}，开局到第 {_f(ft)} 秒一直没交火'
    else:
        ev_txt = (f'死前身上还有{_nades_text(left)}，第 {_f(quiet_from_t)} 秒到第 {_f(ft)} 秒之间'
                  + ('你自己没和人交过火' if feed else '没有交火'))
    facts = dict(t=C.sec(ft), death_t=C.sec(C.t_of(ctx, dt, rnd)), us=us, them=them, nades=left,
                 nades_text=_nades_text(left), quiet=C.sec(quiet), quiet_int=q, quiet_from_t=C.sec(quiet_from_t),
                 A=A, B=B, C=Cb, branch=branch, attacker=attacker, planted=planted, eco=False, death_cost=death_cost,
                 place=pcn_now, place_raw=place_now, util_place=util_place, util_t=C.sec(C.t_of(ctx, cu['tick'], rnd)) if cu else None,
                 radar_place=radar_place, throw=throw_n, thrown=thrown, unseen=len(unseen),
                 coming=C.name_of(ctx, mate) if mate else None, tradable=[C.name_of(ctx, m) for m in tradable],
                 killer=C.name_of(ctx, killer) if killer else None, killer_place=kcn, user=C.name_of(ctx, U),
                 # 五道门（第 1–4 道在这里判完才会出这条；第 5 道主句冲突由 merge 按 contra_rules 处理）
                 gates_passed=True, gates=gates, phase=gi['phase'], known_by=gi['known_by'], known_pos=gi['known_pos'],
                 first_in=gi['first_in'], far=gi['far'], alt_kind=alt_kind,
                 contra_rules=list(U01_CONTRA), fact_only_rules=list(U01_FACT_ONLY),
                 clause_fact=f'身上{_nades_text(left)}都还在')
    log.update(round=int(rnd), blame=blame, kind=kind)
    _u01_log(ctx, rnd, **log, pre=None)
    return _hit(ctx, 'UTIL-01', kind, rnd, fight, U, blame, facts, text, alt, kb, ev_txt, death_tick=dt, death_sid=U)


def _g_retake(ctx, rnd, used_keys):
    """审核单第 27 条：回防时已经往包点扔了 u01_retake_util_n 颗以上道具 → 写好的一面（UTIL-G3，facts.trigger='retake'），
    UTIL-01 不出（_u01_gates 第 4 道门 retake_used）。那几颗道具已经被 UTIL-G2 / G3 夸过时不重复"""
    th = ctx.th['util']
    U = C.user_sid(ctx)
    if C.user_side(ctx, rnd) != 'CT' or _plant_tick(ctx, rnd) is None:
        return []
    ri = C.round_info(ctx, rnd)
    dt = C.death_tick(ctx, U, rnd)
    stop = min(ri['end'], dt) if dt is not None else ri['end']
    xs = _retake_util(ctx, rnd, stop)
    if len(xs) < th['u01_retake_util_n'] or any(x['key'] in used_keys for x in xs):
        return []
    first, last = xs[0], xs[-1]
    if not C.alive(ctx, U, first['fire_tick'], rnd):
        return []
    # 扔完以后你真的去回防了：之后阵亡、或者又开了枪
    fired = C.fires(ctx, rnd, sid=U)
    after = (dt is not None and dt > last['fire_tick']) or bool(((fired['tick'] > last['fire_tick']) & (fired['tick'] <= stop)).any())
    if not after:
        return []
    tl = C.tl_round(ctx, rnd) or {}
    site = (tl.get('plant') or {}).get('site')
    site_cn = f'{site} 点' if site in ('A', 'B') else '包点'
    what = '和'.join(dict.fromkeys(x['n'] for x in xs))
    us, them = C.alive_counts(ctx, first['fire_tick'])
    t = C.t_of(ctx, first['tick'], rnd)
    text = f'第 {_f(t)} 秒 {C.alive_str(us, them)} 回防时先把{what}扔进{site_cn}清点，再去接枪'
    facts = dict(trigger='retake', us=us, them=them, util=[x['n'] for x in xs], land=site_cn, throw_t=C.sec(t),
                 throws_t=[C.sec(C.t_of(ctx, x['tick'], rnd)) for x in xs], site=site,
                 why_good='回防时对面守包的位置能预判，先用道具把包点清一遍再进，比直接冲进去稳',
                 death_t=C.sec(C.t_of(ctx, dt, rnd)) if dt is not None else None)
    ev_txt = f'下包后你往{site_cn}扔了{what}，之后才去接枪'
    return [_hit(ctx, 'UTIL-G3', '好', rnd, first['tick'], U, '—', facts, text, '', [], ev_txt,
                 nade_keys=[x['key'] for x in xs])]


# ================================================================ 闪光：每颗闪白了谁
def _flashes(ctx, rnd):
    """这回合每颗闪：{tick, entityid, thrower, xyz, blinds:[(sid, 白屏秒数, blind tick)]}，按爆闪排"""
    fl = _ev(ctx, 'flashbang_detonate')
    bl = _ev(ctx, 'player_blind')
    ri = C.round_info(ctx, rnd)
    out = []
    if not len(fl):
        return out
    fl = fl[(fl['tick'] >= ri['freeze_start']) & (fl['tick'] <= ri['end'] + 5 * TR)]
    for r in fl.itertuples():
        if C.round_of(ctx, int(r.tick)) != int(rnd):
            continue
        b = bl[(bl['entityid'] == r.entityid) & ((bl['tick'] - int(r.tick)).abs() <= 2)] if len(bl) else bl
        # 闪爆时已经阵亡的人（观战也会收到 player_blind）不算被闪到（128 R11）
        blinds = sorted(((x.user_steamid, float(x.blind_duration), int(x.tick)) for x in b.itertuples()
                         if C.alive(ctx, x.user_steamid, int(r.tick), rnd)),
                        key=lambda z: (z[2], z[0]))
        out.append(dict(tick=int(r.tick), entityid=int(r.entityid), thrower=r.user_steamid,
                        xyz=(float(r.x), float(r.y), float(r.z)), blinds=blinds))
    return sorted(out, key=lambda f: (f['tick'], f['entityid']))


def _flash_throw(ctx, rnd, det_tick):
    """用户这颗闪的扔出（det_tick 之前 u02_throw_look_s 秒内最后一次扔闪）"""
    th = ctx.th['util']
    c = [x for x in _user_util(ctx, rnd)
         if x['n'] == '闪' and det_tick - int(th['u02_throw_look_s'] * TR) <= x['tick'] <= det_tick]
    return c[-1] if c else None


# ================================================================ UTIL-02 闪白队友没闪到人
def _u02(ctx, rnd):
    th = ctx.th['util']
    U = C.user_sid(ctx)
    hits = []
    for f in _flashes(ctx, rnd):
        if f['thrower'] != U:
            continue
        mates_b = [(s, d) for s, d, _ in f['blinds'] if s != U and not C.is_enemy(ctx, U, s, rnd)
                   and C.team_of(ctx, s, rnd) and d >= th['u02_mate_blind_s']]
        foes = [(s, d) for s, d, _ in f['blinds'] if C.is_enemy(ctx, U, s, rnd) and d >= th['u02_enemy_blind_s']]
        if not mates_b or foes:
            continue
        thr = _flash_throw(ctx, rnd, f['tick'])
        tk = thr['tick'] if thr else f['tick']
        near = min(C.dist3(f['xyz'], C.pos(ctx, s, f['tick'])) for s, _ in mates_b)
        where = '落在队友身边' if near <= th['u02_near_mate'] else '爆在队友看得到的位置'
        src = thr['xyz'] if thr else C.pos(ctx, U, tk)
        # “在队友身后扔的”要真的在他背后：扔的位置 → 队友 → 闪爆点大致是一条线（同向），而且扔的位置在队友视线背面
        # （e6b R17：你在中路、队友在 CT 出生点看着包点，闪是从侧面拱门那边飞进来的，只按距离差会判成“身后”）
        nm = min(mates_b, key=lambda x: (C.dist3(f['xyz'], C.pos(ctx, x[0], f['tick'])), str(x[0])))[0]
        mp = C.pos(ctx, nm, f['tick'])
        a_ = np.array([mp[0] - src[0], mp[1] - src[1]], dtype=float)
        b_ = np.array([f['xyz'][0] - mp[0], f['xyz'][1] - mp[1]], dtype=float)
        na, nb = float(np.linalg.norm(a_)), float(np.linalg.norm(b_))
        cos_line = float(np.dot(a_, b_) / (na * nb)) if na > 0 and nb > 0 else -1.0
        back = C.facing_deg(ctx, nm, f['tick'], src) > th['u02_behind_face_deg']
        behind = (C.dist3(src, f['xyz']) - near >= th['u02_behind_units'] and cos_line >= th['u02_behind_cos'] and back)
        names = [C.name_of(ctx, s) for s, _ in mates_b]
        text = f'第 {_f(C.t_of(ctx, tk, rnd))} 秒扔的闪{where}'
        if behind:
            text += '，你是在队友身后远处扔的'
        text += f'，白了{"、".join(names)}，一个敌人都没闪到'
        alt = ('还可以走近一点、贴着墙角再丢，让闪在拐角另一侧、队友看不到的地方爆，队友也能跟着闪出去' if behind else
               '还可以让闪在队友视线外、拐角另一侧爆，队友也能跟着闪出去')
        # 审核单第 29 条：被白的队友 u02_mate_die_s 秒内被敌人打死 = 造成了后果（_u02_display 用）
        dth = C.deaths(ctx, rnd)
        died = []
        for s, _ in mates_b:
            dd = dth[(dth['vic'] == s) & (dth['tick'] >= f['tick']) & (dth['tick'] <= f['tick'] + int(th['u02_mate_die_s'] * TR))]
            if len(dd) and C.is_enemy(ctx, U, dd.iloc[0]['att'], rnd):
                died.append(C.name_of(ctx, s))
        facts = dict(mates=names, near_mate=near <= th['u02_near_mate'], behind=behind, entityid=f['entityid'],
                     det_t=C.sec(C.t_of(ctx, f['tick'], rnd)), mate_died=died)
        ev_txt = f'这颗闪白了{"、".join(names)}，对面没有一个人被白到'
        hits.append(_hit(ctx, 'UTIL-02', '问题', rnd, tk, U, '你的责任', facts, text, alt, [],
                         ev_txt, nade_keys=[thr['key']] if thr else []))
    return hits


# ================================================================ UTIL-03 被队友闪白后阵亡 / 挨枪
def _u03(ctx, rnd):
    th = ctx.th['util']
    U = C.user_sid(ctx)
    dt = C.death_tick(ctx, U, rnd)
    h = C.hurts(ctx, rnd)
    h = h[h['vic'] == U]
    mine = []                                              # 用户被队友的闪白到：(闪, 白屏秒数, blind tick)
    for f in _flashes(ctx, rnd):
        if f['thrower'] in (U, 'nan') or C.is_enemy(ctx, U, f['thrower'], rnd) or not C.team_of(ctx, f['thrower'], rnd):
            continue
        for s, d, btk in f['blinds']:
            if s == U:
                mine.append((f, d, btk))
    hits = []
    for f, dur, btk in mine:
        if dur < th['u03_blind_s']:
            continue
        w0, w1 = btk, btk + int(round((dur + th['u03_after_s']) * TR))
        died = dt is not None and w0 <= dt <= w1
        hurt_e = h[(h['tick'] >= w0) & (h['tick'] <= w1) & h['att'].isin(C.enemies(ctx, rnd))]
        # “挨了枪”只算枪打的（635 R10：白屏时只吃了 6 血的雷）
        hurt_e = hurt_e[[C.is_gun(w) for w in hurt_e['wk']]] if len(hurt_e) else hurt_e
        if not (died or len(hurt_e)):
            continue
        thr = f['thrower']
        earlier = [g for g, dd, bt in mine if g['thrower'] == thr and bt < btk and dd >= th['u03_prev_blind_s']
                   and btk - bt <= th['u03_prev_s'] * TR]
        ahead = C.dist3(C.pos(ctx, U, f['tick']), f['xyz']) < C.dist3(C.pos(ctx, thr, f['tick']), f['xyz']) - th['u03_ahead_units']
        also_me = bool(earlier) and ahead
        mname = C.name_of(ctx, thr)
        text = f'第 {_f(C.t_of(ctx, btk, rnd))} 秒被队友 {mname} 的闪白了，还白着就{"被打死了" if died else "挨了枪"}'
        if earlier:
            text += '；之前已经被他闪过一次'
        if ahead:
            text += '，你站在他前面、离闪更近'
        if also_me:
            alt = '还可以被队友闪过一次后落到他身后，等他的闪炸完再跟着进'
            kb = []
        else:
            alt = '还可以被闪白后先往回退两步、等白屏过去再打'      # 和 TIME-06 同一句（8d R20：一处退两步、一处退半步）
            kb = []
        facts = dict(mate=mname, died=died, earlier=bool(earlier), ahead=ahead, also_me=also_me, entityid=f['entityid'],
                     earlier_t=C.sec(C.t_of(ctx, earlier[-1]['tick'], rnd)) if earlier else None)
        ev_txt = f'白屏还没过去就{"阵亡" if died else "被对面打中"}，闪是 {mname} 扔的'
        hits.append(_hit(ctx, 'UTIL-03', '判定', rnd, btk, U if also_me else thr,
                         '你的责任 + 队友或团队' if also_me else '队友或团队', facts, text, alt, kb, ev_txt,
                         death_tick=dt if died else None, death_sid=U if died else None,
                         nade_keys=[f"util:{int(rnd)}:e{f['entityid']}"]))
    return hits


# ================================================================ UTIL-04 被队友雷/火伤到
def _dmg_uncapped(ctx):
    """本场 player_hurt 的 dmg_health 是否不封顶（致死一枪的伤害超过挨打前的血量）"""
    memo = _memo(ctx)
    if 'uncapped' in memo:
        return memo['uncapped']
    h = C.hurts(ctx)
    res = False
    last = {}
    for r in h.itertuples():
        key = (int(r.round), r.vic)
        pre = last.get(key, 100.0)
        if float(r.health) <= 0 and float(r.dmg) > pre + 0.5:
            res = True
            break
        last[key] = float(r.health)
    memo['uncapped'] = res
    return res


def _u04(ctx, rnd):
    th = ctx.th['util']
    U = C.user_sid(ctx)
    h = C.hurts(ctx, rnd)
    mates = set(C.mates(ctx, rnd))
    mine = h[(h['vic'] == U) & h['att'].isin(sorted(mates)) & h['wk'].isin(list(UTIL_DMG_WK))]
    dt = C.death_tick(ctx, U, rnd)
    hits = []
    for att in sorted(mine['att'].unique()):
        g = mine[mine['att'] == att]
        dmg = int(g['dmg'].sum())
        if dmg < th['u04_min_dmg']:
            continue
        tk = int(g['tick'].min())
        changed = None
        if dt is not None and 0 <= dt - tk <= th['u04_death_s'] * TR and _dmg_uncapped(ctx):
            mh = h[h['vic'] == U]
            k = mh[mh['tick'] == dt]
            if len(k):
                raw = float(k['dmg'].max())
                prior = mh[mh['tick'] < dt]
                pre = float(prior.iloc[-1]['health']) if len(prior) else 100.0
                nade_before = float(g[g['tick'] < dt]['dmg'].sum())
                changed = raw < min(100.0, pre + nade_before)   # 没吃这颗道具，最后那一下打不死你
        w = '雷' if 'hegrenade' in set(g['wk']) else '火'
        mname = C.name_of(ctx, att)
        text = f'第 {_f(C.t_of(ctx, tk, rnd))} 秒被队友 {mname} 的{w}伤到'
        if changed is True:
            text += '，这颗道具让你少了扛最后一枪的血'
        elif changed is False:
            text += '，这颗道具没改变结果'
        under = '听到脚下有雷落地' if w == '雷' else '火烧到脚下'
        alt = f'这颗主要是 {mname} 扔前没确认落点附近有没有自己人；你这边{under}时，可以先挪开再架'
        died15 = dt is not None and 0 <= dt - tk <= th['u04_death_s'] * TR
        facts = dict(mate=mname, dmg=dmg, nade=w, changed=changed, uncapped=_dmg_uncapped(ctx))
        ev_txt = f'{mname} 的{w}打掉你' + ('一大截血' if dmg >= 50 else '一点血')     # 不写伤害数字
        hits.append(_hit(ctx, 'UTIL-04', '判定', rnd, tk, att, '队友或团队', facts, text, alt, [], ev_txt,
                         death_tick=dt if died15 else None, death_sid=U if died15 else None))
    return hits


# ================================================================ UTIL-05 唯一一颗闪白扔了
def _u05(ctx, rnd):
    th = ctx.th['util']
    U = C.user_sid(ctx)
    hits = []
    for f in _flashes(ctx, rnd):
        if f['thrower'] != U:
            continue
        if any(s != U and d >= th['u05_any_blind_s'] for s, d, _ in f['blinds']):
            continue
        thr = _flash_throw(ctx, rnd, f['tick'])
        tk = thr['tick'] if thr else f['tick']
        if _nades_at(ctx, U, tk).count('闪') != 1:
            continue
        det = f['tick']
        # 爆闪前 3 秒内雷达上有敌人在爆点 800 内 = 有目标
        target = False
        for e in C.enemies(ctx, rnd):
            for tt in range(det - int(th['u05_radar_s'] * TR), det + 1, 8):
                if C.alive(ctx, e, tt, rnd) and C.on_radar(ctx, e, tt, tt) and \
                        C.dist3(C.pos(ctx, e, tt), f['xyz']) <= th['u05_radar_near']:
                    target = True
                    break
            if target:
                break
        if target:
            continue
        # 有人跟闪：爆闪后 3 秒内我方有人往爆点走近 150 以上，或爆点 1200 内的人开了枪
        follow = False
        t1 = det + int(th['u05_follow_s'] * TR)
        for m in C.mates(ctx, rnd, include_self=True):
            if not C.alive(ctx, m, det, rnd):
                continue
            p0 = C.pos(ctx, m, det)
            p1 = C.pos(ctx, m, min(t1, (C.death_tick(ctx, m, rnd) or t1 + 1) - 1))
            if C.dist3(p0, f['xyz']) - C.dist3(p1, f['xyz']) >= th['u05_follow_move']:
                follow = True
            fm = C.fires(ctx, rnd, sid=m)
            if C.dist3(p0, f['xyz']) <= th['u05_follow_fire_r'] and len(fm[(fm['tick'] >= det) & (fm['tick'] <= t1)]):
                follow = True
        if follow:
            continue
        land = thr['land'] if thr and thr.get('land') else C.nearest_place(ctx, f['xyz'], rnd)
        lcn = C.place_cn(land)
        to = f'扔向{lcn}' if lcn != '某处' else '扔出去'
        text = f'第 {_f(C.t_of(ctx, tk, rnd))} 秒把身上唯一一颗闪{to}，谁都没闪到，那附近也没看到过对手，也没人跟着出去'
        alt = '还可以先留着这颗闪，等有人推进时再给，比没有目标地扔更值'
        kb = []
        facts = dict(land=lcn, entityid=f['entityid'], det_t=C.sec(C.t_of(ctx, det, rnd)))
        ev_txt = '这颗闪谁都没白到，爆闪前后那附近没看到过对手，也没有队友跟着出去'
        hits.append(_hit(ctx, 'UTIL-05', '思路', rnd, tk, U, '—', facts, text, alt, kb, ev_txt,
                         nade_keys=[thr['key']] if thr else []))
    return hits


# ================================================================ UTIL-06 狙击线没补烟
def _u06(ctx, rnd):
    th = ctx.th['util']
    U = C.user_sid(ctx)
    my_team = C.team_of(ctx, U, rnd)
    mates = set(C.mates(ctx, rnd))
    hits = []
    for r in C.deaths(ctx, rnd).itertuples():
        tk = int(r.tick)
        if r.vic not in mates or not C.is_enemy(ctx, U, r.att, rnd) or r.wk not in th['u06_weapons']:
            continue
        if C.t_of(ctx, tk, rnd) > th['u06_max_t'] or bool(r.thrusmoke):
            continue
        pk, pv = C.pos(ctx, r.att, tk), C.pos(ctx, r.vic, tk)
        if C.dist3(pk, pv) < th['u06_kill_dist']:
            continue
        if not C.alive(ctx, U, tk, rnd) or '烟' not in _nades_at(ctx, U, tk):
            continue
        if C.dist3(C.pos(ctx, U, tk), pv) > th['u06_near']:
            continue
        t0 = tk - int(th['u06_stay_s'] * TR)
        if not all(C.dist3(C.pos(ctx, U, tt), C.pos(ctx, r.vic, tt)) <= th['u06_stay_near'] for tt in range(t0, tk + 1, 8)):
            continue
        later = [s for s in C.smokes(ctx, rnd) if tk < s['t0'] <= tk + int(th['u06_late_s'] * TR)
                 and C.team_of(ctx, s['by'], rnd) == my_team and _seg_dist((s['x'], s['y'], s['z']), pk, pv) <= th['u06_line_r']]
        # 你自己已经往这条线扔了烟、只是还没起来（128 R4：开局往 VIP 扔的烟晚了 0.8 秒）：不是“你身上的烟没用”
        if any(s['by'] == U and s['t0'] > tk - TR for s in later + [x for x in C.smokes(ctx, rnd) if x['by'] == U
               and tk - TR < x['t0'] <= tk + int(th['u06_late_s'] * TR)
               and _seg_dist((x['x'], x['y'], x['z']), pk, pv) <= th['u06_line_r']]):
            continue
        if any(x['n'] == '烟' and x['tick'] < tk and (x.get('det_t') is None or x['det_t'] >= C.t_of(ctx, tk, rnd) - 1.0)
               and (x.get('det_t') is None or x['det_t'] <= C.t_of(ctx, tk, rnd) + th['u06_late_s'])
               and C.place_cn(x.get('land')) == C.place_cn(C.place_at(ctx, r.att, tk)) for x in _user_util(ctx, rnd)):
            continue
        kp = C.place_at(ctx, r.att, tk)
        if C.user_side(ctx, rnd) == 'T' and kp in th['u06_t_skip_places']:
            continue            # 审核单第 31 条 / Q17：T 开局拱门方向的烟用不上，不建议补（8d R20 10.8）
        kcn = C.place_cn(kp)
        vname = C.name_of(ctx, r.vic)
        text = f'第 {_f(C.t_of(ctx, tk, rnd))} 秒 {vname} 在你旁边被{kcn}方向的狙打死，当时这条线没烟，你身上的烟没用'
        late_n = None
        if later:
            late_n = int(round((later[0]['t0'] - tk) / TR))
            text += '，过了一会儿才有人补上'             # 不写延迟秒数
        alt = f'还可以由你先补{kcn}方向的烟，挡住这条线再站位'
        mid = th['mid_places']
        kb = []
        facts = dict(mate=vname, killer=C.name_of(ctx, r.att), killer_place=kcn, weapon=C.weapon_cn(r.wk),
                     late_s=late_n, death_t=C.sec(C.t_of(ctx, tk, rnd)))
        ev_txt = f'{vname} 被{kcn}方向的{C.weapon_cn(r.wk)}打死时你就在他附近，身上带着烟'
        hits.append(_hit(ctx, 'UTIL-06', '思路', rnd, tk, U, '—', facts, text, alt, kb, ev_txt,
                         death_tick=tk, death_sid=r.vic))
    return hits


# ================================================================ UTIL-07 中路清空后没帮两边
def _u07(ctx, rnd):
    th = ctx.th['util']
    U = C.user_sid(ctx)
    if C.user_side(ctx, rnd) != 'CT':
        return []
    mates = set(C.mates(ctx, rnd))
    mid = set(th['mid_places'])
    dth = C.deaths(ctx, rnd)
    ks = [r for r in dth.itertuples() if r.att in mates and C.is_enemy(ctx, U, r.vic, rnd)]
    for a, b in zip(ks, ks[1:]):
        if int(b.tick) - int(a.tick) > th['u07_win_s'] * TR:
            continue
        if C.place_at(ctx, a.vic, int(a.tick)) not in mid or C.place_at(ctx, b.vic, int(b.tick)) not in mid:
            continue
        tk = int(b.tick)
        if not C.alive(ctx, U, tk, rnd):
            continue
        up = C.place_at(ctx, U, tk)
        if up not in mid | set(th['u07_user_extra']):
            continue
        # 拖时间只给烟、火（审核单第 26 条：闪拖不了时间），身上只剩闪时不写
        left = [n for n in _nades_at(ctx, U, tk) if n in ('火', '烟')]
        if not left:
            continue
        side_land = set(th['u07_side_land'])
        if any(tk < x['tick'] <= tk + th['u07_idle_s'] * TR and x.get('land') in side_land for x in _user_util(ctx, rnd)):
            continue
        t = C.t_of(ctx, tk, rnd)
        text = (f'第 {_f(t)} 秒队友在中路连杀两人，你在{C.place_cn(up)}，身上还有{_nades_text(left)}，'
                f'之后没往 A 二楼、B 二楼两边扔过')
        alt = f'还可以在中路清空后缩回拱门，往 A 二楼或 B 二楼丢{"、".join(_sorted_nades(set(left)))}，帮两边拖时间，自己不用露头'
        t1 = C.t_of(ctx, int(a.tick), rnd)
        facts = dict(place=C.place_cn(up), nades=left, kill1_t=C.sec(t1), kill2_t=C.sec(t),
                     killers=[C.name_of(ctx, a.att), C.name_of(ctx, b.att)], victims=[C.name_of(ctx, a.vic), C.name_of(ctx, b.vic)])
        ev_txt = (f'第 {_f(t1)} 秒和第 {_f(t)} 秒队友在中路先后打死 {C.name_of(ctx, a.vic)}、{C.name_of(ctx, b.vic)}，'
                  f'之后你的道具没有落到两边')
        return [_hit(ctx, 'UTIL-07', '思路', rnd, tk, U, '—', facts, text, alt, [], ev_txt)]
    return []


# ================================================================ UTIL-G1 开局 A1 火
def _g1(ctx, rnd):
    th = ctx.th['util']
    U = C.user_sid(ctx)
    if C.user_side(ctx, rnd) != 'CT' or C.buy(ctx, rnd, 'us') != '全起' or C.buy(ctx, rnd, 'them') not in ('全起', '强起'):
        return []
    hits = []
    for x in _user_util(ctx, rnd):
        t = C.t_of(ctx, x['tick'], rnd)
        if x['n'] != '火' or t > th['g1_throw_max_t'] or x.get('land') != 'TRamp':
            continue
        burned = sorted({d['name'] for d in x['damage'] if d.get('enemy') and d.get('dmg', 0) > 0})
        det_t = x.get('det_t')
        on_time = det_t is not None and det_t <= th['g1_late_s']
        if burned or on_time:
            text = f'第 {_f(t)} 秒开局往 A1 扔火'
            text += f'，正好卡在 T 冲 A1 的时候，烧到 {len(burned)} 个人' if burned else '，起得及时'
            alt = ''                       # “继续这样扔”是夸奖，理由放 why_good
            kind, blame, kb = '好', '—', []
            ev_txt = f'火在第 {_f(det_t)} 秒起来' + (f'，烧到了{"、".join(burned)}' if burned else '') if det_t is not None else '火烧到了人'
        else:
            text = f'第 {_f(t)} 秒开局往 A1 扔火，起得比大多数人晚，也没烧到人'
            alt = '还可以再早一点出手，走到能扔的位置就扔，火起来时 T 还没上台阶'
            kind, blame, kb = '问题', '小问题', []
            ev_txt = f'火在第 {_f(det_t)} 秒才起来，一个人都没烧到' if det_t is not None else '火一个人都没烧到'
        facts = dict(det_t=C.sec(det_t) if det_t is not None else None, burned=burned, n=len(burned), on_time=on_time)
        if kind == '好':
            facts['why_good'] = ('火正好卡在 T 冲 A1 的时候起来，烧到了人' if burned else
                                 '火起得早，T 上 A1 时就得先等火或者绕路')
        hits.append(_hit(ctx, 'UTIL-G1', kind, rnd, x['tick'], U, blame, facts, text, alt, kb, ev_txt, nade_keys=[x['key']]))
    return hits


# ================================================================ UTIL-G2 道具打掉一截血后拿下击杀
def _g2(ctx, rnd, g1_keys):
    th = ctx.th['util']
    U = C.user_sid(ctx)
    h = C.hurts(ctx, rnd)
    h = h[(h['att'] == U) & h['wk'].isin(list(UTIL_DMG_WK))]
    h = h[h['vic'].isin(C.enemies(ctx, rnd))]
    if not len(h):
        return []
    uu = _user_util(ctx, rnd)
    dth = C.deaths(ctx, rnd)
    ours = set(C.mates(ctx, rnd, include_self=True))
    per_nade = {}                                          # 道具 key → [(敌人, 伤害, 击杀行)]
    for v in sorted(h['vic'].unique()):
        g = h[h['vic'] == v]
        dmg = float(g['dmg'].sum())
        if dmg < th['g2_min_dmg']:
            continue
        k = dth[(dth['vic'] == v) & (dth['tick'] >= int(g['tick'].min()))
                & (dth['tick'] <= int(g['tick'].max()) + int(th['g2_kill_win_s'] * TR)) & dth['att'].isin(sorted(ours))]
        if not len(k):
            continue
        # 伤害最多的那颗道具（每条伤害归到它之前最后一颗同类道具）
        by = Counter()
        for r in g.itertuples():
            n = '雷' if r.wk == 'hegrenade' else '火'
            c = [x for x in uu if x['n'] == n and x['tick'] <= int(r.tick)]
            if c:
                by[c[-1]['key']] += float(r.dmg)
        if not by:
            continue
        key = sorted(by.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        per_nade.setdefault(key, []).append((v, dmg, k.iloc[0]))
    hits = []
    for key in sorted(per_nade, key=lambda s: int(s.split(':')[-1])):
        if key in g1_keys:                                 # 开局 A1 火已由 UTIL-G1 说过
            continue
        x = next(u for u in uu if u['key'] == key)
        v, dmg, krow = sorted(per_nade[key], key=lambda z: (-z[1], z[0]))[0]
        # 掉了血的他在死之前又打死了你或队友：“后面对枪时更好打”不成立（e6b R6：雷炸了 高分玩家80，他随后打死你和 高分玩家81）
        last_dmg = int(h[(h['vic'] == v)]['tick'].max())
        if len(dth[(dth['att'] == v) & (dth['tick'] > last_dmg) & (dth['tick'] < int(krow['tick']))
                   & dth['vic'].isin(sorted(ours))]):
            continue
        t0, t1 = x['tick'] - int(th['g2_seen_s'] * TR), x['tick']
        big = '一大截' if dmg >= th['g2_big_dmg'] else '一截'
        killer = '你' if krow['att'] == U else C.name_of(ctx, krow['att'])
        vname = C.name_of(ctx, v)
        t = C.t_of(ctx, x['tick'], rnd)
        if C.spots(ctx, U, v, t0, t1) is not None:
            how = ''
            lead = f'第 {_f(t)} 秒扔的{x["n"]}打掉 {vname} {big}血'
        elif C.on_radar(ctx, v, t0, t1):
            how = '按队友看到的位置扔'
            lead = f'第 {_f(t)} 秒按队友看到的位置扔了一颗{x["n"]}，打掉 {vname} {big}血'
        else:
            how = '往有动静的地方扔'
            # 只说没看到他这个人：这回合之前可能已经和别人交过火、打死过人（8d R7、R15），“都还没看到人”不对
            lead = f'第 {_f(t)} 秒你和队友都还没看到 {vname}，你往有动静的地方扔了一颗{x["n"]}，打掉他{big}血'
        text = lead + f'，随后{killer}把他打死'
        # 完整性复核：最后那一下就是你这颗道具（雷炸死 / 火烧死），不是“打掉血后再拿下”（bd R25 46.8：雷直接炸死了 对手5（第4场））
        nade_kill = krow['att'] == U and str(krow['wk']) in UTIL_DMG_WK
        if nade_kill:
            verb = '炸死' if x['n'] == '雷' else '烧死'
            lead = lead.replace(f'打掉 {vname} {big}血', f'把 {vname} {verb}了').replace(f'打掉他{big}血', f'把他{verb}了')
            text = lead
        alt = ''                           # “继续这样用”是夸奖，理由放 why_good
        planted = _plant_tick(ctx, rnd) is not None and _plant_tick(ctx, rnd) < x['tick']
        kb = []
        kill_t = C.t_of(ctx, int(krow['tick']), rnd)
        facts = dict(nade=x['n'], victim=vname, big=dmg >= th['g2_big_dmg'], killer=killer, kill_t=C.sec(kill_t),
                     how=how or None, land=C.place_cn(x.get('land')) if x.get('land') else None, text_short=lead,
                     why_good=f'先用{x["n"]}把 {vname} 打掉了血，后面对枪时他已经掉了血，更好打',
                     manner=f'{vname} 之前已经被你的{x["n"]}打掉了{big}血')
        ev_txt = f'这颗{x["n"]}打掉 {vname} {big}血后，第 {_f(kill_t)} 秒{killer}把他打死'
        if nade_kill:
            facts['why_good'] = f'{x["n"]}扔在他身上，直接把 {vname} {verb}了'
            facts['manner'] = f'{vname} 是被你的{x["n"]}{verb}的'
            ev_txt = f'第 {_f(kill_t)} 秒 {vname} 被这颗{x["n"]}{verb}'
        hits.append(_hit(ctx, 'UTIL-G2', '好', rnd, x['tick'], U, '—', facts, text, alt, kb, ev_txt, nade_keys=[key]))
    return hits


# ================================================================ UTIL-G3 占优或掉血后用道具拖时间
def _g3(ctx, rnd):
    th = ctx.th['util']
    U = C.user_sid(ctx)
    side = C.user_side(ctx, rnd)
    approach = set(th['t_approach'] if side == 'CT' else th['ct_approach'])
    trig = []
    dth = C.deaths(ctx, rnd)
    for r in dth[(dth['att'] == U)].itertuples():
        if not C.is_enemy(ctx, U, r.vic, rnd):
            continue
        us, them = C.alive_counts(ctx, int(r.tick) + 1, lag_s=0)
        if us > them:
            trig.append(('kill', int(r.tick), r.vic, C.place_at(ctx, r.vic, int(r.tick)), us, them))
    h = C.hurts(ctx, rnd)
    h = h[(h['vic'] == U)]
    nxt = -1
    for tk in sorted(set(int(x) for x in h['tick'])):
        if tk < nxt:
            continue
        w = h[(h['tick'] >= tk) & (h['tick'] <= tk + int(th['g3_hurt_win_s'] * TR))]
        if w['dmg'].sum() >= th['g3_hurt_min'] and C.alive(ctx, U, tk + int(th['g3_alive_after_s'] * TR), rnd):
            # 这一截血全是被雷 / 火炸烧掉的：没有枪战，句子不写“硬架 / 退出视线”（e6b R18：高分玩家80 的雷 56 血）
            nade = bool(len(w)) and bool(w['wk'].isin(list(UTIL_DMG_WK)).all())
            nw = ('被雷炸掉' if set(w['wk']) <= {'hegrenade'} else '被火烧掉' if 'hegrenade' not in set(w['wk']) else '被雷和火打掉')
            trig.append(('hurt_nade' if nade else 'hurt', tk, None, nw if nade else None, None, None))
            nxt = tk + int(th['g3_hurt_win_s'] * TR) + 1
    # 审核单第 26 条：拖时间只给烟、火（雷是打血的，不算拖住对方）：口径在 util.json g3_delay_nades
    uu = [x for x in _user_util(ctx, rnd) if x['n'] in th['g3_delay_nades']]
    gun = C.fires(ctx, rnd, sid=U)
    used = set()
    hits = []
    for kind, tk, vic, vplace, us, them in sorted(trig, key=lambda z: (z[1], z[0])):
        for x in uu:
            if x['key'] in used or not (0 < x['tick'] - tk <= th['g3_after_s'] * TR):
                continue
            if x.get('land') not in approach and not (kind == 'kill' and x.get('land') == vplace):
                continue
            q0 = tk + int(th['g3_quiet_from_s'] * TR)
            seen = any(C.spots(ctx, e, U, q0, x['tick'] - 1) is not None for e in C.enemies(ctx, rnd))
            fired = len(gun[(gun['tick'] >= q0) & (gun['tick'] < x['tick'])]) > 0
            if seen or fired:
                continue
            grp = [x] + [y for y in uu if y is not x and y['key'] not in used and 0 <= y['tick'] - x['tick'] <= th['g3_chain_s'] * TR]
            for y in grp:
                used.add(y['key'])
            what = '和'.join(dict.fromkeys(y['n'] for y in grp))
            land = C.place_cn(x.get('land'))
            trig_t = C.t_of(ctx, tk, rnd)
            # 扔完之后多久才又和人接触（你开枪、被看到、挨打）：太快的话“没急着找第二枪 / 没再回去硬架”不成立（128 R8：扔完 1.3 秒又打死一个）
            last_throw = max(y['tick'] for y in grp)
            r_end = C.round_info(ctx, rnd)['end']
            udt = C.death_tick(ctx, U, rnd)
            stop = min(r_end, udt) if udt is not None else r_end
            nc = [int(t) for t in gun['tick'] if last_throw < int(t) <= stop]
            nc += [t for t in (C.spots(ctx, e, U, last_throw + 1, stop) for e in C.enemies(ctx, rnd)) if t is not None]
            nc += [int(t) for t in h['tick'] if last_throw < int(t) <= stop]
            nc = min(nc) if nc else None
            soon = nc is not None and nc - last_throw <= th['g3_after_quiet_s'] * TR
            if kind in ('hurt', 'hurt_nade') and soon:
                continue
            if kind == 'kill' and soon:
                text = (f'第 {_f(trig_t)} 秒拿到人（{C.alive_str(us, them)}）后先把{what}扔到{land}拖住对方，再去打下一个')
            elif kind == 'kill':
                text = f'第 {_f(trig_t)} 秒拿到人（{C.alive_str(us, them)}）后没急着再露头找第二枪，先把{what}扔到{land}拖住对方'
            elif kind == 'hurt_nade':
                text = f'第 {_f(trig_t)} 秒{vplace}一大截血后没再出去，把{what}扔到{land}拖住对方'
            else:
                text = f'第 {_f(trig_t)} 秒被打掉一大截血后没再回去硬架，退出对方视线，把{what}扔到{land}拖住对方'
            planted = _plant_tick(ctx, rnd) is not None and _plant_tick(ctx, rnd) < x['tick']
            if side == 'CT':
                alt, kb = '还可以道具给完后换个位置再看，打掉一个人就借着烟换位', []
            elif planted:
                # 原来写“把身上的道具往回防路线上全用掉”：没有具体报点、还包括闪（审核单第 26 条：拖时间只给烟、火并写报点），不写
                alt, kb = '', []
            else:
                alt, kb = '', []
            throw_t = C.t_of(ctx, x['tick'], rnd)
            facts = dict(trigger='hurt' if kind == 'hurt_nade' else kind, hurt_by_nade=kind == 'hurt_nade', trigger_t=C.sec(trig_t), throw_t=C.sec(throw_t), util=[y['n'] for y in grp],
                         land=land, us=us, them=them, victim=C.name_of(ctx, vic) if vic else None,
                         why_good=('拿到人后先用道具把对面拖住，再去打下一个' if kind == 'kill' and soon else
                                   '占优以后没急着露头找第二枪，先用道具把对面拖住' if kind == 'kill' else
                                   '掉了一大截血以后没急着再出去，先用道具把对面拖住' if kind == 'hurt_nade' else
                                   '掉了一大截血以后没回去硬架，先用道具把对面拖住'),
                         next_contact_t=C.sec(C.t_of(ctx, nc, rnd)) if nc is not None else None)
            if soon:
                ev_txt = f'第 {_f(trig_t)} 秒到第 {_f(throw_t)} 秒之间你没再被对面看到，也没再开枪'
            elif nc is not None:
                ev_txt = f'第 {_f(trig_t)} 秒到第 {_f(C.t_of(ctx, nc, rnd))} 秒之间你没再被对面看到，也没再开枪'
            else:
                ev_txt = f'第 {_f(trig_t)} 秒以后你没再被对面看到，也没再开枪'
            hits.append(_hit(ctx, 'UTIL-G3', '好', rnd, x['tick'], U, '—', facts, text, alt, kb, ev_txt,
                             nade_keys=[y['key'] for y in grp]))
            break
    return hits


# ================================================================ 汇总
def _merge_keys(hits):
    """M5：同一回合同一颗道具的命中给同一个 merge_key（几条命中共用道具时并成一组，取组里最早那颗）"""
    parent = {}

    def find(a):
        while parent.setdefault(a, a) != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for h in hits:
        ks = h['_nades']
        for k in ks[1:]:
            ra, rb = find(ks[0]), find(k)
            if ra != rb:
                parent[max(ra, rb, key=_key_order)] = min(ra, rb, key=_key_order)
    groups = {}
    for h in hits:
        if h['_nades']:
            groups.setdefault(find(h['_nades'][0]), []).append(h)
    for root, hs in groups.items():
        allk = sorted({k for h in hs for k in h['_nades']}, key=_key_order)
        for h in hs:
            h['merge_key'] = allk[0]
    for h in hits:
        del h['_nades']


def _key_order(k):
    tail = k.split(':')[-1]
    return (k.split(':')[1], 0 if tail.isdigit() else 1, int(tail) if tail.isdigit() else 0, tail)


def run(ctx):
    """跑全部道具类规则，返回 Hit 列表（按回合、tick、规则排）"""
    hits = []
    _memo(ctx)['u01_log'] = {}
    for r in C.rounds(ctx):
        rnd = r['n']
        if C.team_of(ctx, C.user_sid(ctx), rnd) not in (2, 3):
            continue
        u06 = _u06(ctx, rnd)
        u01 = _u01(ctx, rnd, u06[0] if u06 else None)
        g1 = _g1(ctx, rnd)
        g1_keys = {k for h in g1 for k in h['_nades']}
        g23 = _g2(ctx, rnd, g1_keys) + _g3(ctx, rnd)
        used = g1_keys | {k for h in g23 for k in h['_nades']}
        hits += ([u01] if u01 else []) + _u02(ctx, rnd) + _u03(ctx, rnd) + _u04(ctx, rnd) + _u05(ctx, rnd) + u06 \
            + _u07(ctx, rnd) + g1 + g23 + _g_retake(ctx, rnd, used)
    _u02_display(ctx, hits)
    _merge_keys(hits)
    hits.sort(key=lambda h: (h['round'], h['tick'], h['rule']))
    _memo(ctx)['ran'] = True
    return hits


def _u02_display(ctx, hits):
    """审核单第 29 条：UTIL-02 只在被白的队友 u02_mate_die_s 秒内被打死、或一场出现 u02_min_per_match 次以上时成时刻；
    其余 facts.display='summary'（不进时刻，只计数）。facts.match_count = 这一场 UTIL-02 的次数"""
    th = ctx.th['util']
    u02 = [h for h in hits if h['rule'] == 'UTIL-02']
    n = len(u02)
    for h in u02:
        f = h['facts']
        f['match_count'] = n
        moment = bool(f.get('mate_died')) or n >= th['u02_min_per_match']
        if not moment:
            f['display'] = 'summary'
            f['count_only'] = True


def _flash_carried(ctx, rnd):
    """这回合你买 / 带着的闪：冻结开始到冻结结束后 u_buy_s 秒之间背包里闪最多的那一帧的颗数"""
    th = ctx.th['util']
    idx = _inv_index(ctx).get(C.user_sid(ctx))
    if idx is None:
        return 0
    ri = C.round_info(ctx, rnd)
    tks, invs = idx
    i0, i1 = np.searchsorted(tks, [int(ri['freeze_start']), int(ri['start'] + th['u_buy_s'] * TR) + 1])
    return max((invs[i].count('闪') for i in range(int(i0), int(i1))), default=0)


def match_stats(ctx):
    """给 summary（审核单第 3 条“闪买了常常没扔”）用的全场数据，只算复盘的这个人：
    {
      rounds: 参加的回合数,
      flash_carried: 全场买 / 带的闪（每回合买枪阶段背包里闪最多的颗数相加）,
      flash_thrown: 全场扔出的闪（weapon_fire）,
      flash_rounds: [带了闪的回合], flash_unthrown_rounds: [带了闪、整回合一颗都没扔的回合],
      long_deaths: [长枪（背包里有步枪 / 狙）阵亡的回合], long_deaths_flash: [其中死时身上还有闪的回合],
      u01: {回合: {passed, pre, gates, why4, known_by, phase, nades, has_flash, death_t, blame?, kind?}}
           —— 用户阵亡时身上还有道具的每个回合；passed=True 表示过了五道门的第 1–4 道、这回合有 UTIL-01 命中
           （第 5 道“主句冲突”在 merge 里，summary 只链接 passed 且页面上写出来的回合）,
      u01_pass_rounds: [passed 的回合],
      bench: thresholds/util.json 里的对标（每回合扔闪、长枪阵亡身上有闪的比例；只给判断用，页面写次数不写比例）,
      per_round: {回合: {carried, thrown}}
    }"""
    memo = _memo(ctx)
    if not memo.get('ran'):
        run(ctx)
    th = ctx.th['util']
    U = C.user_sid(ctx)
    per, fr, fu, ld, ldf = {}, [], [], [], []
    tot_c = tot_t = n = 0
    inv_full = {}
    try:
        T = ctx.cache('daoju')['ticks'][['tick', 'steamid', 'inventory']]
        T = T[T['steamid'].map(C._sid_str) == U].sort_values('tick', kind='mergesort')
        inv_full = (T['tick'].to_numpy(np.int64), T['inventory'].to_numpy(object))
    except (FileNotFoundError, KeyError, OSError):
        inv_full = None
    for r in C.rounds(ctx):
        rnd = r['n']
        if C.team_of(ctx, U, rnd) not in (2, 3):
            continue
        n += 1
        c = _flash_carried(ctx, rnd)
        t = sum(1 for x in _throws(ctx, U, rnd) if x['n'] == '闪')
        per[rnd] = dict(carried=c, thrown=t)
        tot_c += c
        tot_t += t
        if c:
            fr.append(rnd)
            if not t:
                fu.append(rnd)
        dt = C.death_tick(ctx, U, rnd)
        if dt is not None and inv_full is not None:
            i = int(np.searchsorted(inv_full[0], int(dt) - 1, side='right')) - 1
            inv = list(inv_full[1][i]) if i >= 0 and inv_full[1][i] is not None else []
            if any(C.gun_tier(w) >= 2 for w in inv):
                ld.append(rnd)
                if '闪' in _nades_at(ctx, U, dt):
                    ldf.append(rnd)
    log = {k: memo.get('u01_log', {})[k] for k in sorted(memo.get('u01_log', {}))}
    return dict(rounds=n, flash_carried=tot_c, flash_thrown=tot_t, flash_rounds=fr, flash_unthrown_rounds=fu,
                long_deaths=ld, long_deaths_flash=ldf, u01=log,
                u01_pass_rounds=[k for k, v in log.items() if v.get('passed')],
                bench=dict(th.get('bench') or {}), per_round=per)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    from engine.ctx import make_ctx
    _ctx = make_ctx(sys.argv[1])
    for _h in run(_ctx):
        print(f"R{_h['round']:>2}-{_h['t']:<5} {_h['rule']:<8} {_h['kind']} [{_h['blame']}] {_h['text']}\n"
              f"           还可以：{_h['alt']}  kb={_h['kb']}  merge={_h['merge_key']}\n           证据：{_h['evidence']}")
