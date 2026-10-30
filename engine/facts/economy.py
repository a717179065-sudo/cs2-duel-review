# -*- coding: utf-8 -*-
"""经济层（由 pro/analysis/match3/econ_match3.py 参数化而来，算法不变）。

run(ctx) 输出（work/<id>/）：
  economy.json        {"meta", "rules": {...实测经济规则...}, "bench": {...高分/职业对标...}, "rounds": [每回合一项]}
  economy_out.txt     规则实测过程 + 逐回合经济表（人看）

数据来源：
  demo 直接读每人 balance / cash_spent_this_round / current_equip_value / round_start_equip_value / inventory，
  取样时刻：冻结开始（round_start）、冻结结束、购买时间结束（buytime_ended，= 买完）、回合结束前 1 tick、
  回合结束后 1 tick、下一回合冻结开始前 1 tick；击杀奖励在每次击杀前后 1 tick 取 balance 差。
  回合编号 / 冻结结束 / 回合结束 tick 沿用 work/<key>/base/rounds.csv（和 extract 一致）。
  对标：bench/sit_rounds.csv（每回合每方的买法），groups.csv 的 high（11 场 FACEIT 高分局）和 pro（12 场职业），
  本场如果也在对标库里，先把本场的行去掉。

字段约定：
  start      回合开局的钱 = 买完时 balance + 本回合花的钱（cash_spent_this_round，含给队友买枪）
  spent      本回合花的钱；left = start - spent（买完剩多少）
  equip      买完时装备价值（current_equip_value，含上回合保下来的枪、队友丢的枪）
  kept       上回合带过来的装备价值（round_start_equip_value）
  loss_counter  回合开始时该方的连败计数 k（实测规则见 rules），输了这回合拿 1400+500*min(k,4)... 见 rules.loss_bonus

和原脚本的区别：
  · demo / id / 用户 / 输出目录从 ctx 来；meta 的场次、比分、用户名、地图取 timeline.json；
  · 原脚本里人工写的第 3 场判断（每回合 economy 的 should/verdict/why/effect/enemy、buy 后面的人工备注、
    summary、第 16 回合强起反事实）只对第 3 场成立，不再生成——这些由规则层（ECO-*）按模板产出；
  · rules 里几句带第 3 场例子的说明（after_win、t_survive_timeout、kill_reward_note、loss_counter_field）
    改成按实测值拼的通用句子；
  · 没有第 13 回合（比赛提前结束）时跳过"手枪局开局钱"那一行；
  · 半场按 config.half_spans 切（常规 MR12、加时每半场 MR3，值在 thresholds/common.json），
    连败计数每个半场（含加时的每个半场）重新开始，半场最后一回合之后钱会重置。
"""
import json
import math
from collections import Counter, defaultdict

import numpy as np
import pandas as pd
from demoparser2 import DemoParser

from .. import config
from . import rounds_csv

OUTPUTS = ['economy.json']

TR = 64

RIFLE = {'AK-47', 'M4A1-S', 'M4A4', 'Galil AR', 'FAMAS', 'AUG', 'SG 553'}
AWP = {'AWP'}
AUTO = {'SCAR-20', 'G3SG1'}
SCOUT = {'SSG 08'}
SMG = {'MAC-10', 'MP9', 'MP7', 'MP5-SD', 'UMP-45', 'P90', 'PP-Bizon'}
HEAVY = {'Nova', 'XM1014', 'MAG-7', 'Sawed-Off', 'M249', 'Negev'}
DEFAULT_PISTOL = {'Glock-18', 'USP-S', 'P2000'}
UPG_PISTOL = {'Desert Eagle', 'P250', 'Five-SeveN', 'Tec-9', 'CZ75-Auto', 'Dual Berettas', 'R8 Revolver'}
NADES = {'Smoke Grenade': '烟', 'Flashbang': '闪', 'High Explosive Grenade': '雷', 'Molotov': '火',
         'Incendiary Grenade': '火', 'Decoy Grenade': '诱饵'}
WCN = {'Desert Eagle': '沙鹰', 'MAC-10': '吹风机'}   # 用户允许 MAC-10 叫吹风机
# 和 sit_rounds.py 一致的"步枪"口径（买法分类要和对标数据同口径）
RIFLE_SIT = RIFLE | AWP | AUTO

def nn(v):
    if v is None:
        return None
    if isinstance(v, (float, np.floating)) and math.isnan(v):
        return None
    if isinstance(v, np.bool_):
        return bool(v)
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.floating):
        return float(v)
    return v


def clean(o):
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    return nn(o)


def mode(vals):
    vals = [int(v) for v in vals]
    return Counter(vals).most_common(1)[0][0] if vals else None


def rate(k, n):
    return f'{k}/{n}（{k / n * 100:.0f}%）' if n else '0/0'


def money(x):
    return f'${int(round(x / 50.0) * 50)}'



def run(ctx):
    """解析 ctx.dem 的经济，写 economy.json / economy_out.txt。依赖 timeline.json（meta）。"""
    pd.set_option('display.width', 250); pd.set_option('display.max_columns', 40); pd.set_option('display.max_rows', 500)
    DEM = ctx.dem
    DEMO = ctx.id
    USER = ctx.user
    OUT = ctx.work
    ANA = config.ANA
    TLM = ctx.json('timeline.json')['meta']
    HR, OTR = config.halves_th(ctx.th)
    out_lines = []

    def P(*a):
        s = ' '.join(str(x) for x in a)
        print(s, flush=True)
        out_lines.append(s)

    # ================================================================ 读 demo
    P('解析 demo ...')
    p = DemoParser(str(DEM))
    match_start = int(p.parse_event('begin_new_match')['tick'].max())
    R = pd.read_csv(rounds_csv(ctx)).sort_values('round').reset_index(drop=True)
    round_start_ev = sorted(int(t) for t in p.parse_event('round_start')['tick'])
    buy_end_ev = sorted(int(t) for t in p.parse_event('buytime_ended')['tick'] if t > match_start)

    rounds = []
    for i, r in R.iterrows():
        st = int(r.start_tick)
        fs = max([t for t in round_start_ev if t <= st], default=st - 20 * TR)
        be = min([t for t in buy_end_ev if t > st], default=st + 20 * TR)
        be = min(be, int(r.end_tick) - 1)
        rounds.append(dict(round=int(r['round']), fs=fs, fe=st, be=be, end=int(r.end_tick), winner=r.winner,
                           reason=r.reason, plant=not pd.isna(r.plant_tick),
                           plant_tick=None if pd.isna(r.plant_tick) else int(r.plant_tick)))
    for i, rd in enumerate(rounds):
        rd['next_fs'] = rounds[i + 1]['fs'] if i + 1 < len(rounds) else None
    RN = {rd['round']: rd for rd in rounds}
    NR = len(rounds)
    HALVES = config.half_spans(max(RN), HR, OTR)      # [(第一回合, 最后一回合)]，加时每个半场单独一段

    def last_of_half(r):
        return r == config.half_end(r, HR, OTR)

    PISTOL_R = (1, HR + 1)                            # 常规半场第一回合 = 手枪局（加时没有手枪局）


    def round_of(tick):
        for i, rd in enumerate(rounds):
            hi = rd['next_fs'] if rd['next_fs'] else rd['end'] + 15 * TR
            if rd['fs'] <= tick < hi:
                return rd['round']
        return None


    deaths = p.parse_event('player_death', player=['team_num'], other=['total_rounds_played'])
    deaths = deaths[deaths.tick > match_start].copy()
    for c in ['attacker_steamid', 'user_steamid', 'assister_steamid']:
        if c in deaths:
            deaths[c] = deaths[c].astype(str)
    deaths['round'] = [round_of(int(t)) for t in deaths.tick]
    deaths = deaths[deaths['round'].notna()].copy()
    deaths['round'] = deaths['round'].astype(int)
    def evdf(name):
        """事件表；这场一次都没发生（demoparser 返回空 list）时给带 tick / steamid 列的空表"""
        d = p.parse_event(name)
        if isinstance(d, pd.DataFrame) and len(d):
            return d
        return pd.DataFrame({c: pd.Series(dtype='int64' if c == 'tick' else object)
                             for c in ('tick', 'user_steamid', 'steamid', 'item_name', 'was_sold', 'user_name')})

    planted = evdf('bomb_planted'); planted = planted[planted.tick > match_start].copy()
    planted['user_steamid'] = planted['user_steamid'].astype(str)
    defused = evdf('bomb_defused'); defused = defused[defused.tick > match_start].copy()
    defused['user_steamid'] = defused['user_steamid'].astype(str)
    purch = evdf('item_purchase'); purch = purch[purch.tick > match_start].copy()
    purch['steamid'] = purch['steamid'].astype(str)
    purch['round'] = [round_of(int(t)) for t in purch.tick]

    PROPS = ['balance', 'cash_spent_this_round', 'current_equip_value', 'round_start_equip_value', 'team_num',
             'is_alive', 'armor_value', 'has_helmet', 'has_defuser', 'inventory']
    sample = set()
    for rd in rounds:
        sample |= {rd['fs'] + 2, rd['fe'], rd['be'], rd['end'] - 1, rd['end'] + 1}
        if rd['next_fs']:
            sample.add(rd['next_fs'] - 1)
        else:
            sample.add(rd['end'] + 5 * TR)
    for t in deaths.tick:
        sample |= {int(t) - 1, int(t) + 1}
    for t in list(planted.tick) + list(defused.tick):
        sample |= {int(t) - 1, int(t) + 1}
    TK = p.parse_ticks(PROPS, ticks=sorted(sample))
    TK['steamid'] = TK['steamid'].astype(str)
    NAME = dict(zip(TK.steamid, TK.name))
    TKI = TK.set_index(['tick', 'steamid'])
    P(f'采样 tick {len(sample)} 个，行数 {len(TK)}')


    def g(tick, sid, col):
        try:
            return TKI.at[(tick, sid), col]
        except KeyError:
            return None


    # 每回合双方阵容（以冻结结束时 team_num 为准）
    side_of = {}   # (round, sid) -> 'T'/'CT'
    roster = {}    # (round, side) -> [sid]
    for rd in rounds:
        x = TK[(TK.tick == rd['fe']) & TK.team_num.isin([2, 3])]
        for sid, tn in zip(x.steamid, x.team_num):
            side_of[(rd['round'], sid)] = 'T' if tn == 2 else 'CT'
        for s in ('T', 'CT'):
            roster[(rd['round'], s)] = sorted([sid for sid, tn in zip(x.steamid, x.team_num) if (tn == 2) == (s == 'T')],
                                              key=lambda z: NAME[z])
    OPP = {'T': 'CT', 'CT': 'T'}
    # 用户每回合的阵营；某回合不在场（掉线/晚进）时按最近一个在场回合的队友这回合站哪边推
    present = [rd['round'] for rd in rounds if (rd['round'], USER) in side_of]
    user_side = {}
    for rd in rounds:
        r = rd['round']
        if (r, USER) in side_of:
            user_side[r] = side_of[(r, USER)]
            continue
        r0 = min(present, key=lambda x: (abs(x - r), x))
        mates0 = [sid for (rr, sid), sd in sorted(side_of.items()) if rr == r0 and sd == side_of[(r0, USER)] and sid != USER]
        votes = Counter(side_of[(r, sid)] for sid in mates0 if (r, sid) in side_of)
        user_side[r] = sorted(votes, key=lambda k: (-votes[k], k))[0] if votes else side_of[(r0, USER)]

    # ================================================================ 规则实测
    P('\n================ 规则实测 ================')
    # ---- 1) 击杀奖励：击杀前后 1 tick 的 balance 差（同一 tick 多杀按人数均分；排除回合结束那一 tick 的击杀）
    kr_rows = []
    dd = deaths[(deaths.attacker_steamid != deaths.user_steamid) & deaths.attacker_steamid.isin(NAME.keys())].copy()
    dd['tk'] = [side_of.get((r, a)) == side_of.get((r, u)) for r, a, u in zip(dd['round'], dd.attacker_steamid, dd.user_steamid)]
    end_ticks = {rd['end'] for rd in rounds}
    for (tick, att), grp in dd.groupby(['tick', 'attacker_steamid']):
        tick = int(tick)
        b0, b1 = g(tick - 1, att, 'balance'), g(tick + 1, att, 'balance')
        if b0 is None or b1 is None or tick in end_ticks or tick + 1 in end_ticks:
            continue
        if b1 >= 16000:
            continue
        n = len(grp)
        w = grp.weapon.iloc[0] if grp.weapon.nunique() == 1 else 'mixed'
        kr_rows.append(dict(tick=tick, attacker=NAME[att], weapon=w, n=n, tk=bool(grp.tk.iloc[0]), delta=int(b1 - b0) / n))
    KR = pd.DataFrame(kr_rows)
    kill_reward = {}
    P('\n[击杀奖励] 每把武器：击杀前后 balance 差的众数（括号是样本数和出现过的值）')
    for w, x in KR[~KR.tk].groupby('weapon'):
        vals = Counter(int(v) for v in x.delta)
        kill_reward[w] = dict(reward=mode(x.delta), n=len(x), values=dict(vals))
        P(f'  {w:16s} ${mode(x.delta)}  (n={len(x)}, {dict(vals)})')
    tkx = KR[KR.tk]
    if len(tkx):
        P(f'  误杀队友：{dict(Counter(int(v) for v in tkx.delta))}')

    # ---- 2) 下包 / 拆包个人奖励
    plant_personal = []
    for t, s in zip(planted.tick, planted.user_steamid):
        b0, b1 = g(int(t) - 1, s, 'balance'), g(int(t) + 1, s, 'balance')
        if b0 is not None and b1 is not None:
            plant_personal.append(int(b1 - b0))
    P(f'\n[下包个人奖励] 下包者在下包那一刻 balance 变化：{dict(Counter(plant_personal))}')

    # ---- 3) 回合结束奖励：end-1 → end+1（胜负奖励），end+1 → 下一回合冻结开始前（之后的团队奖励）
    end_rows = []
    for rd in rounds:
        r = rd['round']
        if rd['next_fs'] is None or last_of_half(r):   # 半场最后一回合后钱会重置；最后一回合没有下一回合
            late_tick = None
        else:
            late_tick = rd['next_fs'] - 1
        for s in ('T', 'CT'):
            for sid in roster[(r, s)]:
                b0, b1 = g(rd['end'] - 1, sid, 'balance'), g(rd['end'] + 1, sid, 'balance')
                b2 = g(late_tick, sid, 'balance') if late_tick else None
                alive = g(rd['end'] - 1, sid, 'is_alive')
                end_rows.append(dict(round=r, side=s, sid=sid, name=NAME[sid], won=rd['winner'] == s, reason=rd['reason'],
                                     plant=rd['plant'], alive=bool(alive), b0=b0, d_end=None if b1 is None else int(b1 - b0),
                                     d_late=None if (b2 is None or b1 is None) else int(b2 - b1), capped=bool(b1 is not None and b1 >= 16000)))
    ER = pd.DataFrame(end_rows)
    # 回合结束后（end+1 → 下一回合冻结开始）CT 方的团队奖励：和本回合 CT 杀了几个 T 对比
    P('\n[回合结束后的团队奖励] end+1 → 下一回合冻结开始前 1 tick，每方众数 vs 本回合击杀数')
    late_rows = []
    for (r, s), x in ER[ER.d_late.notna() & ~ER.capped].groupby(['round', 'side']):
        k = dd[(dd['round'] == r) & ~dd.tk & dd.attacker_steamid.map(lambda a: side_of.get((r, a)) == s)]
        k_in = k[k.tick <= RN[r]['end']]
        late_rows.append(dict(round=r, side=s, late=mode(x.d_late), kills=len(k), kills_in_round=len(k_in)))
    LT = pd.DataFrame(late_rows)
    for s in ('CT', 'T'):
        y = LT[LT.side == s]
        P(f'  {s}: ' + '，'.join(f'R{a}:${b}/{c}杀' for a, b, c in zip(y['round'], y.late, y.kills)))
    ct = LT[LT.side == 'CT']
    ct_ok = (ct.late == 50 * ct.kills).mean() if len(ct) else 0
    ct_ok_in = (ct.late == 50 * ct.kills_in_round).mean() if len(ct) else 0
    t_late = Counter(LT[LT.side == 'T'].late)
    P(f'  CT：回合后奖励 = $50 × 本回合 CT 击杀数（含回合结束后的击杀）吻合 {ct_ok * 100:.0f}%；只算回合内击杀吻合 {ct_ok_in * 100:.0f}%')
    P(f'  T：回合后奖励取值 {dict(t_late)}')

    # 胜方奖励
    P('\n[胜方奖励] end-1 → end+1 的 balance 差（每回合每方众数）')
    win_rows = []
    for (r, s), x in ER[ER.won & ER.d_end.notna() & ~ER.capped].groupby(['round', 'side']):
        win_rows.append(dict(round=r, side=s, reason=x.reason.iloc[0], amt=mode(x.d_end), vals=dict(Counter(x.d_end))))
    WR = pd.DataFrame(win_rows)
    win_reward = {}
    for rs, x in WR.groupby('reason'):
        win_reward[rs] = dict(amount=mode(x.amt), n_rounds=len(x), values=dict(Counter(int(a) for a in x.amt)))
        P(f'  {rs:14s} ${mode(x.amt)}  回合 {list(x["round"])}  {dict(Counter(int(a) for a in x.amt))}')
    # 拆包者个人：胜方奖励之外多出来的
    defuse_personal = []
    for t, s in zip(defused.tick, defused.user_steamid):
        r = round_of(int(t))
        row = ER[(ER['round'] == r) & (ER.sid == s)]
        if len(row) and row.d_end.notna().iloc[0]:
            defuse_personal.append(int(row.d_end.iloc[0]) - int(WR[(WR['round'] == r)].amt.iloc[0]))
    P(f'  拆包者比队友多拿：{dict(Counter(defuse_personal))}')

    # 败方奖励
    P('\n[败方奖励] end-1 → end+1（每回合每方众数；T 方回合结束时还活着、且没下包输掉的会拿 0，单列）')
    loss_rows = []
    for (r, s), x in ER[~ER.won & ER.d_end.notna() & ~ER.capped].groupby(['round', 'side']):
        zero_alive = x[(x.d_end == 0)]
        base = x[x.d_end > 0]
        loss_rows.append(dict(round=r, side=s, plant=bool(x.plant.iloc[0]) and s == 'T', reason=x.reason.iloc[0],
                              amt=mode(base.d_end) if len(base) else 0, vals=dict(Counter(int(v) for v in x.d_end)),
                              zero=list(zero_alive.name)))
    LR = pd.DataFrame(loss_rows).sort_values(['side', 'round']).reset_index(drop=True)

    def loss_bonus(k_after):
        return BASE[min(max(k_after, 1), 5) - 1]


    # 推连败计数：先猜下包加成 = 两种候选（600/800），选能让所有 T 下包输的回合落到 1400+500k 网格上的那个
    BASE = [1400, 1900, 2400, 2900, 3400]
    best_plant = None
    for pb in (800, 600, 700, 500, 900):
        ok = all((a - (pb if pl else 0)) in BASE for a, pl in zip(LR.amt, LR.plant) if a > 0)
        if ok:
            best_plant = pb
            break
    P(f'  T 下包后输掉的团队加成：${best_plant}（这个值让所有败方奖励都落在 1400/1900/2400/2900/3400 上）')
    LR['base'] = [a - (best_plant if pl else 0) for a, pl in zip(LR.amt, LR.plant)]
    LR['k_obs'] = [BASE.index(b) + 1 if b in BASE else None for b in LR.base]   # 输完之后的计数 1..5
    for row in LR.itertuples():
        P(f'  R{row.round:2d} {row.side:2s} {"下包" if row.plant else "    "} 败方奖励 ${row.amt}（基础 ${row.base}，'
          f'对应输完后计数 {row.k_obs}） 拿 0 的：{row.zero}')


    # ---- 4) 连败计数模型：赢了清零 vs 赢了减 1；半场开始计数是多少。用实测败方奖励逐回合检验
    def simulate(win_rule, start_k):
        """win_rule: 'reset' 赢了清零 / 1 赢了减 1 / 2 赢了减 2。返回 {(round, 'us'|'them'): (k_before, k_after)}"""
        res = {}
        for half in HALVES:
            k = {'us': start_k, 'them': start_k}
            for r in range(half[0], half[1] + 1):
                if r not in RN:
                    continue
                us = user_side[r]
                for key, s in (('us', us), ('them', OPP[us])):
                    kb = k[key]
                    if RN[r]['winner'] == s:
                        ka = 0 if win_rule == 'reset' else max(kb - win_rule, 0)
                    else:
                        ka = min(kb + 1, 5)
                    res[(r, key)] = (kb, ka)
                    k[key] = ka
        return res


    def check(model):
        hits, miss = 0, []
        for row in LR.itertuples():
            key = 'us' if row.side == user_side[row.round] else 'them'
            kb, ka = model[(row.round, key)]
            pred = BASE[min(ka, 5) - 1]
            if pred == row.base:
                hits += 1
            else:
                miss.append(f'R{row.round}{key}:预测{pred}/实测{row.base}')
        return hits, miss


    models = {}
    P('\n[连败计数模型检验]（败方奖励 = 1400 + 500 ×（输完后的计数 − 1），计数最多 5，即最多 $3400）')
    WR_CN = {'reset': '清零', 1: '减 1', 2: '减 2'}
    for wr in ('reset', 1, 2):
        for sk in (0, 1):
            m = simulate(wr, sk)
            h, miss = check(m)
            models[(wr, sk)] = (m, h, miss)
            P(f'  赢了{WR_CN[wr]}，半场开始计数 {sk}：吻合 {h}/{len(LR)}  {miss[:6]}')
    # 吻合数打平时优先“赢了减 2”（手册 ECON-A：demo 实测是 −2；635357b4 清零和 −2 都是 27/28 时原来选了清零），
    # 其次减 1、清零；半场开始计数 0 优先
    WR_PREF = {2: 2, 1: 1, 'reset': 0}
    best = max(models.items(), key=lambda kv: (kv[1][1], WR_PREF[kv[0][0]], -kv[0][1]))
    (WIN_RULE, START_K), (KM, KH, KMISS) = best
    P(f'  => 采用：赢了{WR_CN[WIN_RULE]}，半场开始计数 {START_K}（吻合 {KH}/{len(LR)}）')

    # 赢了以后连败计数怎么变：直接用"赢之后下一次输"的金额证据列出来
    evid = []
    for key in ('us', 'them'):
        for half in HALVES:
            seq = []
            for r in range(half[0], half[1] + 1):
                if r not in RN:
                    continue
                s = user_side[r] if key == 'us' else OPP[user_side[r]]
                seq.append((r, RN[r]['winner'] == s))
            for i in range(1, len(seq)):
                r, w = seq[i]
                if w or not seq[i - 1][1]:
                    continue
                # 本回合输、上回合赢：找本回合实测
                row = LR[(LR['round'] == r) & (LR.side == (user_side[r] if key == 'us' else OPP[user_side[r]]))]
                if not len(row):
                    continue
                # 赢之前的连败串
                j = i - 1
                while j >= 0 and seq[j][1]:
                    j -= 1
                nwin = i - 1 - j
                nloss = 0
                while j >= 0 and not seq[j][1]:
                    nloss += 1; j -= 1
                evid.append(dict(team='我方' if key == 'us' else '对方', round=r, before=f'连输 {nloss} 回合后连赢 {nwin} 回合',
                                 loss_bonus=int(row.base.iloc[0]),
                                 k_before_win=KM[(seq[i - nwin][0], key)][0]))
    P('\n[赢了以后再输：实测败方奖励（去掉下包加成）]')
    for e in evid:
        kw = e['k_before_win']
        m1 = models[(1, START_K)][0][(e['round'], 'us' if e['team'] == '我方' else 'them')][1]
        m2 = models[(2, START_K)][0][(e['round'], 'us' if e['team'] == '我方' else 'them')][1]
        m0 = models[('reset', START_K)][0][(e['round'], 'us' if e['team'] == '我方' else 'them')][1]
        e.update(pred_reset=loss_bonus(m0), pred_minus1=loss_bonus(m1), pred_minus2=loss_bonus(m2))
        P(f'  {e["team"]} R{e["round"]}：{e["before"]}（按采用的模型，赢之前计数 {kw}），这回合输了拿 ${e["loss_bonus"]}'
          f'（清零应为 ${e["pred_reset"]}，减 1 应为 ${e["pred_minus1"]}，减 2 应为 ${e["pred_minus2"]}）')

    # ---- 5) 半场开局的钱
    if HR + 1 in RN:
        h2 = [int(g(RN[HR + 1]['fs'] + 2, sid, 'balance')) for sid in roster[(HR + 1, 'T')] + roster[(HR + 1, 'CT')]]
        P(f'\n[手枪局开局钱] R1/R{HR + 1} 每人 {dict(Counter(h2))}')
    ot_start = {}
    for lo, hi in HALVES[2:]:
        v = [g(RN[lo]['fs'] + 2, sid, 'balance') for sid in roster[(lo, 'T')] + roster[(lo, 'CT')]] if lo in RN else []
        v = [int(x) for x in v if x is not None]
        if v:
            ot_start[lo] = mode(v)
            P(f'[加时开局钱] R{lo} 每人 {dict(Counter(v))}')

    # ---- 6) T 方存活到时间结束拿不拿钱
    t_zero = LR[LR.zero.map(len) > 0]
    P(f'[T 时间到存活不拿败方奖励] 出现 {len(t_zero)} 次：' + '；'.join(f'R{a} {b}' for a, b in zip(t_zero['round'], t_zero.zero)))


    WIN_ELIM = win_reward.get('ct_killed', win_reward.get('t_killed', {})).get('amount', 3250)
    WIN_BOMB = win_reward.get('target_bombed', {}).get('amount', 3500)
    WIN_DEFUSE = win_reward.get('bomb_defused', {}).get('amount', 3500)
    PLANT_TEAM = best_plant
    PLANT_PERSONAL = mode(plant_personal) if plant_personal else 300

    # ================================================================ 每人每回合明细
    FULL_T = 3900     # AK 2700 + 头盔甲 1000 + 一两颗道具
    FULL_CT = 4400    # M4 2900/3100 + 头盔甲 1000 + 钳子/道具
    AWP_NEED = 5750   # AWP 4750 + 头盔甲 1000


    def weapon_buckets(inv):
        inv = list(inv) if isinstance(inv, (list, np.ndarray)) else []
        prim = [w for w in inv if w in RIFLE | AWP | AUTO | SCOUT | SMG | HEAVY]
        pist = [w for w in inv if w in UPG_PISTOL | DEFAULT_PISTOL]
        nades = [w for w in inv if w in NADES]
        return prim, pist, nades


    player_rows = []
    for rd in rounds:
        r = rd['round']
        # 枪看“买完、还没有人倒下”那一刻：购买结束前已经有人死了的话，之后背包里可能是捡来的枪
        # （e6b 高分玩家84 R6：买的是 Galil，第一次死人之后捡了 高分玩家04 的 AK，购买结束时背包里是 AK）
        fd = deaths[(deaths['round'] == r) & (deaths.tick > rd['fe'])].tick
        inv_tick = int(fd.min()) - 1 if len(fd) and int(fd.min()) - 1 < rd['be'] and int(fd.min()) - 1 >= rd['fe'] else rd['be']
        for s in ('T', 'CT'):
            for sid in roster[(r, s)]:
                bal_be = g(rd['be'], sid, 'balance')
                spent = g(rd['be'], sid, 'cash_spent_this_round')
                start = int(bal_be + spent)
                # 买完到购买结束之间可能已经有击杀奖励：用冻结开始的钱交叉验证
                bal_fs = g(rd['fs'] + 2, sid, 'balance')
                spent_fs = g(rd['fs'] + 2, sid, 'cash_spent_this_round') or 0
                start_fs = None if bal_fs is None else int(bal_fs + spent_fs)
                kills_before_be = dd[(dd.attacker_steamid == sid) & (dd.tick <= rd['be']) & (dd.tick >= rd['fe']) & ~dd.tk]
                if len(kills_before_be) and start_fs is not None:
                    start = start_fs
                eq_fe = g(rd['fe'], sid, 'current_equip_value') or 0
                eq_be = g(rd['be'], sid, 'current_equip_value') or 0
                alive_be = bool(g(rd['be'], sid, 'is_alive'))
                inv_be = g(rd['be'], sid, 'inventory') if alive_be else g(rd['fe'], sid, 'inventory')
                inv_fe = g(rd['fe'], sid, 'inventory')
                prim, pist, nades = weapon_buckets(inv_be)
                if inv_tick != rd['be'] and bool(g(inv_tick, sid, 'is_alive')):
                    prim_e, pist_e, _ = weapon_buckets(g(inv_tick, sid, 'inventory'))
                    if prim_e:
                        prim = prim_e
                    if pist_e:
                        pist = pist_e
                prim_fe, _, nades_fe = weapon_buckets(inv_fe)
                if not prim and prim_fe:
                    prim = prim_fe
                if len(nades_fe) > len(nades):
                    nades = nades_fe
                armor = g(rd['be'], sid, 'armor_value') if alive_be else g(rd['fe'], sid, 'armor_value')
                helmet = g(rd['be'], sid, 'has_helmet') if alive_be else g(rd['fe'], sid, 'has_helmet')
                kit = g(rd['be'], sid, 'has_defuser') if alive_be else g(rd['fe'], sid, 'has_defuser')
                b_end0 = g(rd['end'] - 1, sid, 'balance')
                b_end1 = g(rd['end'] + 1, sid, 'balance')
                k_in = dd[(dd.attacker_steamid == sid) & (dd['round'] == r) & ~dd.tk]
                pur = purch[(purch['round'] == r) & (purch.steamid == sid)]
                bought = [f'{w}{"(退)" if sold else ""}' for w, sold in zip(pur.item_name, pur.was_sold)]
                player_rows.append(dict(
                    round=r, side=s, sid=sid, name=NAME[sid], us=s == user_side[r], user=sid == USER,
                    start=start, spent=int(spent), left=int(start - spent), kept=int(g(rd['fe'], sid, 'round_start_equip_value') or 0),
                    equip=int(max(eq_be, eq_fe)), primary=prim[0] if prim else None, pistol=pist[0] if pist else None,
                    nades=[NADES[n] for n in nades], armor=int(armor or 0) > 0, helmet=bool(helmet), kit=bool(kit),
                    kills=len(k_in), end_bal=None if b_end0 is None else int(b_end0),
                    end_reward=None if (b_end0 is None or b_end1 is None) else int(b_end1 - b_end0),
                    purchases=bought))
    PR = pd.DataFrame(player_rows).astype({'primary': object, 'pistol': object})
    PR['primary'] = [w if isinstance(w, str) else None for w in PR.primary]
    PR['pistol'] = [w if isinstance(w, str) else None for w in PR.pistol]


    def wname(w):
        return WCN.get(w, w)


    def team_summary(x, r, s):
        n = len(x)
        prim = [w for w in x.primary if isinstance(w, str)]
        n_rifle = sum(w in RIFLE for w in prim)
        n_awp = sum(w in AWP for w in prim)
        n_auto = sum(w in AUTO for w in prim)
        n_scout = sum(w in SCOUT for w in prim)
        n_smg = sum(w in SMG for w in prim)
        n_heavy = sum(w in HEAVY for w in prim)
        n_upg = sum(1 for pr_, pi in zip(x.primary, x.pistol) if not isinstance(pr_, str) and pi in UPG_PISTOL)
        n_rifle_sit = n_rifle + n_awp + n_auto
        nade_cnt = Counter(n for lst in x.nades for n in lst)
        avg_equip = x.equip.mean()
        pre_sum, spent_sum = x.start.sum(), x.spent.sum()
        ratio = spent_sum / max(pre_sum, 1)
        if r in PISTOL_R:
            buy = '手枪局'
        elif avg_equip < 1600 and n_rifle_sit <= 1:
            buy = 'eco'
        elif n_rifle_sit >= 4 or (n_rifle_sit == 3 and avg_equip >= 3500):
            buy = '全起'
        else:
            buy = '强起' if ratio >= 0.75 else '半起'
        wc = Counter(wname(w) for w in prim)
        parts = [f'{wname(k)}×{v}' if v > 1 else wname(k) for k, v in wc.most_common()]
        pc = Counter(wname(pi) for pr_, pi in zip(x.primary, x.pistol) if not isinstance(pr_, str) and pi in UPG_PISTOL)
        if pc:
            parts.append('只拿手枪的：' + '、'.join(f'{k}×{v}' if v > 1 else k for k, v in pc.most_common()))
        n_pistol_only = sum(1 for pr_ in x.primary if not isinstance(pr_, str))
        desc = (f'{n_rifle} 把步枪、{n_awp} 把 AWP' + (f'、{n_scout} 把鸟狙' if n_scout else '') +
                f'、{n_smg} 把冲锋枪' + (f'、{n_heavy} 把霰弹/机枪' if n_heavy else '') + f'、{n_pistol_only} 人只拿手枪'
                f'；{x.helmet.sum()} 人头盔甲、{(x.armor & ~x.helmet).sum()} 人只有甲'
                + (f'、{x.kit.sum()} 把钳子' if s == 'CT' else '') +
                f'；道具 {sum(nade_cnt.values())} 颗（' + '、'.join(f'{k}{v}' for k, v in nade_cnt.most_common()) + '）')
        return dict(
            n=n, avg_start=round(x.start.mean()), min_start=int(x.start.min()), max_start=int(x.start.max()),
            sum_start=int(pre_sum), avg_spent=round(x.spent.mean()), sum_spent=int(spent_sum), spent_ratio=round(ratio, 2),
            avg_left=round(x.left.mean()), sum_left=int(x.left.sum()), avg_equip=round(avg_equip), sum_equip=int(x.equip.sum()),
            min_equip=int(x.equip.min()), max_equip=int(x.equip.max()), avg_kept=round(x.kept.mean()),
            buy=buy, n_rifle=n_rifle, n_awp=n_awp, n_auto=n_auto, n_scout=n_scout, n_smg=n_smg, n_heavy=n_heavy,
            n_upg_pistol_only=n_upg, n_pistol_only=n_pistol_only, n_helmet=int(x.helmet.sum()), n_armor=int(x.armor.sum()),
            n_kit=int(x.kit.sum()), n_nades=int(sum(nade_cnt.values())), nades=dict(nade_cnt),
            guns=' + '.join(parts) if parts else '全员默认手枪', desc=desc, n_rifle_sit=n_rifle_sit)


    # ================================================================ 每回合汇总 + 连败计数 + 预测
    def loss_amount(side, k_after, planted_flag):
        return loss_bonus(k_after) + (PLANT_TEAM if side == 'T' and planted_flag else 0)


    ROWS = []
    for rd in rounds:
        r = rd['round']
        us = user_side[r]
        rec = dict(round=r, side=us, winner_side=rd['winner'], won=rd['winner'] == us, reason=rd['reason'], plant=rd['plant'])
        for key, s in (('us', us), ('them', OPP[us])):
            x = PR[(PR['round'] == r) & (PR.side == s)]
            summ = team_summary(x, r, s)
            kb, ka = KM[(r, key)]
            summ['loss_counter'] = kb
            # 预测：按回合结束前一刻的钱（含本回合实际击杀奖励），分别加上赢/输的奖励
            b0 = x.end_bal.fillna(x.left)
            k_if_loss = min(kb + 1, 5)
            k_if_win = 0 if WIN_RULE == 'reset' else max(kb - 1, 0)
            win_amt = WIN_ELIM   # 按全灭 $3250 算（T 炸包是 $3500）
            ct_bonus = 0
            lose_amt = loss_amount(s, k_if_loss, rd['plant'])
            summ['if_win_next'] = dict(avg=round(float(np.minimum(b0 + win_amt, 16000).mean())), min=int(np.minimum(b0 + win_amt, 16000).min()),
                                       reward=win_amt, k_next=k_if_win)
            summ['if_loss_next'] = dict(avg=round(float(np.minimum(b0 + lose_amt, 16000).mean())), min=int(np.minimum(b0 + lose_amt, 16000).min()),
                                        reward=lose_amt, k_next=k_if_loss)
            # 买完那一刻的预判（不含本回合击杀）
            summ['from_buy_if_win'] = round(float((x.left + win_amt).mean()))
            summ['from_buy_if_loss'] = round(float((x.left + loss_amount(s, k_if_loss, False)).mean()))
            summ['k_after'] = ka
            summ['players'] = [dict(name=p_.name, user=bool(p_.user), start=p_.start, spent=p_.spent, left=p_.left, kept=p_.kept,
                                    equip=p_.equip, primary=p_.primary if isinstance(p_.primary, str) else None,
                                    pistol=p_.pistol if isinstance(p_.pistol, str) else None, nades=p_.nades, helmet=p_.helmet,
                                    armor=p_.armor, kit=p_.kit if s == 'CT' else None, kills=p_.kills,
                                    end_balance=p_.end_bal, end_reward=p_.end_reward, purchases=p_.purchases)
                               for p_ in x.itertuples()]
            rec[key] = summ
        ROWS.append(rec)
    # 下一回合实际开局（同一批人；半场最后一回合之后钱重置，不接）
    for i, rec in enumerate(ROWS):
        for key in ('us', 'them'):
            if i + 1 < len(ROWS) and not last_of_half(rec['round']):
                nx = ROWS[i + 1][key]
                rec[key]['next_actual'] = dict(avg=nx['avg_start'], min=nx['min_start'], max=nx['max_start'],
                                               avg_with_kept=round(nx['avg_start'] + nx['avg_kept']))
            else:
                rec[key]['next_actual'] = None

    # 资源（下一回合开局钱 + 保下来的装备）够全起 / 够起狙的人数
    for i, rec in enumerate(ROWS):
        for key in ('us', 'them'):
            s = rec['side'] if key == 'us' else OPP[rec['side']]
            need = FULL_T if s == 'T' else FULL_CT
            pl = rec[key]['players']
            res = [p_['start'] + p_['kept'] for p_ in pl]
            rec[key]['n_can_full'] = sum(v >= need for v in res)
            rec[key]['n_can_awp'] = sum(p_['start'] >= AWP_NEED or (p_['kept'] >= 4750) for p_ in pl)
            rec[key]['full_need'] = need

    P('\n================ 逐回合经济表 ================')
    for rec in ROWS:
        u, t = rec['us'], rec['them']
        P(f"R{rec['round']:2d} 我方{rec['side']:2s} {'赢' if rec['won'] else '输'} {rec['reason']:13s} | 我方 k={u['loss_counter']}→{u['k_after']} "
          f"{u['buy']} 人均开局${u['avg_start']}（{u['min_start']}–{u['max_start']}）花${u['avg_spent']} 剩${u['avg_left']} 装备${u['avg_equip']} "
          f"[{u['guns']}] 道具{u['n_nades']} 能全起{u['n_can_full']}人 || 对方 k={t['loss_counter']}→{t['k_after']} {t['buy']} 人均${t['avg_start']}"
          f"（{t['min_start']}–{t['max_start']}）装备${t['avg_equip']} [{t['guns']}] 道具{t['n_nades']} 能全起{t['n_can_full']}人 能起狙{t['n_can_awp']}人")
        P(f"      赢→下回合我方人均≈${u['if_win_next']['avg']}  输→≈${u['if_loss_next']['avg']}  实际下回合"
          f" {u['next_actual']}｜对方 赢→≈${t['if_win_next']['avg']} 输→≈${t['if_loss_next']['avg']} 实际 {t['next_actual']}")
        for p_ in u['players']:
            P(f"        {p_['name']:12s} 开局${p_['start']:5d} 花${p_['spent']:5d} 剩${p_['left']:5d} 保留${p_['kept']:5d} 装备${p_['equip']:5d} "
              f"{p_['primary'] or '-':10s} {p_['pistol'] or '-':12s} {''.join(p_['nades'])} 击杀{p_['kills']} 结束前${p_['end_balance']}")

    # ================================================================ 对标（sit_rounds.csv）
    P('\n================ 对标：高分局 / 职业 ================')
    S = pd.read_csv(ANA / 'sit_rounds.csv', dtype={'demo': str})
    S = S[S.group.isin(['high', 'pro']) & (S.demo != DEMO)].copy()      # 本场如果在对标库里，不拿自己对标自己
    SI = S.set_index(['demo', 'round', 'side'])
    bench = {}


    def won_of(d, r, s):
        return SI.won.get((d, r, s))


    # B1 第二回合（手枪局输了）买法和胜率
    b1 = {}
    for grp in ('high', 'pro'):
        for side in ('T', 'CT'):
            x = S[(S.group == grp) & (S.situation == '第二回合') & (S.prev_result == '输') & (S.side == side)]
            d = {}
            for c in ('强起', '半起', 'eco'):
                y = x[x['sub'] == c]
                d[c] = dict(n=len(y), won=int(y.won.sum()))
            b1[f'{grp}_{side}'] = dict(n=len(x), detail=d)
            P(f'[B1 手枪局输→第二回合] {grp} {side} n={len(x)}：' + '；'.join(f'{c} {v["n"]} 次赢 {v["won"]}' for c, v in d.items()))
    bench['second_round_after_pistol_loss'] = b1

    # B2 第三回合：手枪输 + 第二回合强起赢 → 反 eco 的胜率；B3 被翻之后第四回合
    b2, b3 = {}, {}
    for grp in ('high', 'pro'):
        rows3 = []
        for row in S[(S.group == grp) & S['round'].isin([3, HR + 3])].itertuples():
            d, r, s = row.demo, row.round, row.side
            w1, w2 = won_of(d, r - 2, s), won_of(d, r - 1, s)
            if w1 is None or w2 is None or w1 or not w2:
                continue
            r4 = SI.loc[(d, r + 1, s)] if (d, r + 1, s) in SI.index else None
            rows3.append(dict(demo=d, round=r, side=s, won3=bool(row.won), buy3=row.buy, enemy_buy3=row.enemy_buy,
                              buy4=None if r4 is None else r4.buy, pre4=None if r4 is None else r4.pre,
                              spent_ratio4=None if r4 is None else r4.spent_ratio,
                              won4=None if r4 is None else bool(r4.won), enemy_buy4=None if r4 is None else r4.enemy_buy))
        X3 = pd.DataFrame(rows3)
        b2[grp] = dict(n=len(X3), won=int(X3.won3.sum()),
                       by_side={s: dict(n=int((X3.side == s).sum()), won=int(X3[X3.side == s].won3.sum())) for s in ('T', 'CT')})
        L4 = X3[~X3.won3 & X3.buy4.notna()]
        cnt = Counter(L4.buy4)
        b3[grp] = dict(n=len(L4), buys={k: dict(n=v, won=int(L4[L4.buy4 == k].won4.sum())) for k, v in cnt.items()},
                       avg_pre4=round(float(L4.pre4.mean())) if len(L4) else None,
                       rows=[dict(demo=a, round=b + 1, side=c, buy=d_, pre=round(e), won=bool(f_))
                             for a, b, c, d_, e, f_ in zip(L4.demo, L4['round'], L4.side, L4.buy4, L4.pre4, L4.won4)])
        P(f'[B2 手枪输→第二回合赢→第三回合反 eco] {grp}: 赢 {rate(b2[grp]["won"], b2[grp]["n"])}；'
          + '；'.join(f'{s} 赢 {rate(v["won"], v["n"])}' for s, v in b2[grp]['by_side'].items()))
        P(f'[B3 反 eco 被翻 → 下一回合] {grp}: n={len(L4)} 人均开局 ${b3[grp]["avg_pre4"]}，'
          + '；'.join(f'{k} {v["n"]} 次（赢 {v["won"]}）' for k, v in b3[grp]['buys'].items()))
    bench['third_round_reverse_eco_after_force_win'] = b2
    bench['after_reverse_eco_lost'] = b3

    # B4 常规回合：按本方人均开局钱分档、对面全起时的买法和胜率（不含 R1/R2/R13/R14 和加时）
    BINS = [(0, 2000), (2000, 2600), (2600, 3200), (3200, 3900), (3900, 4500), (4500, 99999)]


    def bin_of(v):
        for a, b in BINS:
            if a <= v < b:
                return f'{a}-{b if b < 99999 else "+"}'
        return None


    REG = S[~S['round'].isin([1, 2, HR + 1, HR + 2]) & (S['round'] <= 2 * HR) & (S.enemy_buy == '长枪')].copy()
    REG['bin'] = REG.pre.map(bin_of)
    b4 = {}
    for grp in ('high', 'pro'):
        for side in ('T', 'CT'):
            for bn, x in REG[(REG.group == grp) & (REG.side == side)].groupby('bin'):
                d = {k: dict(n=int((x.buy == k).sum()), won=int(x[x.buy == k].won.sum())) for k in ('eco', '强起', '长枪')}
                b4[f'{grp}_{side}_{bn}'] = dict(n=len(x), **d)
    for side in ('T', 'CT'):
        P(f'[B4 常规回合 对面全起 {side}] 人均开局档 → 买法次数（赢）')
        for a, b in BINS:
            bn = f'{a}-{b if b < 99999 else "+"}'
            line = f'   ${bn:10s}'
            for grp in ('high', 'pro'):
                v = b4.get(f'{grp}_{side}_{bn}')
                if v:
                    line += f' | {grp} n={v["n"]}: eco {v["eco"]["n"]}({v["eco"]["won"]}) 强起 {v["强起"]["n"]}({v["强起"]["won"]}) 全起 {v["长枪"]["n"]}({v["长枪"]["won"]})'
            P(line)
    bench['regular_vs_full_buy_by_money'] = b4

    # B5 CT 起狙：CT 方人均开局钱分档 → 有 AWP 的比例
    b5 = {}
    CTX = S[(S.side == 'CT') & ~S['round'].isin([1, HR + 1]) & (S.buy == '长枪')].copy()
    CTX['bin'] = CTX.pre.map(bin_of)
    for grp in ('high', 'pro'):
        for bn, x in CTX[CTX.group == grp].groupby('bin'):
            b5[f'{grp}_{bn}'] = dict(n=len(x), with_awp=int((x.n_awp >= 1).sum()))
    P('[B5 CT 全起时带 AWP 的比例，按人均开局钱] ' + '；'.join(f'{k}: {rate(v["with_awp"], v["n"])}' for k, v in sorted(b5.items())))
    bench['ct_awp_by_money'] = b5

    # B6 eco / 强起 对全起的胜率（常规回合）
    b6 = {}
    for grp in ('high', 'pro'):
        for side in ('T', 'CT'):
            for k in ('eco', '强起'):
                x = REG[(REG.group == grp) & (REG.side == side) & (REG.buy == k)]
                b6[f'{grp}_{side}_{k}'] = dict(n=len(x), won=int(x.won.sum()))
    P('[B6 常规回合 打对面全起] ' + '；'.join(f'{k}: {rate(v["won"], v["n"])}' for k, v in b6.items()))
    bench['eco_force_vs_full'] = b6

    # B7 反 eco 胜率（我方全起 vs 对面 eco / 强起）
    b7 = {}
    for grp in ('high', 'pro'):
        for side in ('T', 'CT'):
            for eb in ('eco', '强起'):
                x = S[(S.group == grp) & (S.side == side) & (S.buy == '长枪') & (S.enemy_buy == eb) & ~S['round'].isin([2, HR + 2])]
                b7[f'{grp}_{side}_vs_{eb}'] = dict(n=len(x), won=int(x.won.sum()))
    P('[B7 全起打对面 eco/强起] ' + '；'.join(f'{k}: {rate(v["won"], v["n"])}' for k, v in b7.items()))
    bench['full_vs_eco_force'] = b7

    # B8 第二回合赢家（反 eco）胜率
    b8 = {}
    for grp in ('high', 'pro'):
        for side in ('T', 'CT'):
            x = S[(S.group == grp) & (S.situation == '第二回合') & (S.prev_result == '赢') & (S.side == side)]
            b8[f'{grp}_{side}'] = dict(n=len(x), won=int(x.won.sum()))
    P('[B8 手枪局赢→第二回合反 eco 胜率] ' + '；'.join(f'{k}: {rate(v["won"], v["n"])}' for k, v in b8.items()))
    bench['second_round_winner'] = b8

    # B1b 手枪局输了（T 方按有没有下包分）→ 第二回合
    b1b = {}
    for grp in ('high', 'pro'):
        for pl in (True, False):
            x = S[(S.group == grp) & (S.situation == '第二回合') & (S.prev_result == '输') & (S.side == 'T') & (S.prev_plant == pl)]
            b1b[f'{grp}_T_plant{int(pl)}'] = dict(n=len(x), force=int(x['sub'].isin(['强起', '半起']).sum()),
                                                  force_won=int(x[x['sub'].isin(['强起', '半起'])].won.sum()),
                                                  eco=int((x['sub'] == 'eco').sum()), eco_won=int(x[x['sub'] == 'eco'].won.sum()),
                                                  avg_pre=round(float(x.pre.mean())) if len(x) else None)
    P('[B1b T 手枪局输→第二回合，按手枪局有没有下包] ' + '；'.join(
        f'{k}: n={v["n"]} 人均${v["avg_pre"]} 强起/半起 {v["force"]}（赢 {v["force_won"]}） eco {v["eco"]}（赢 {v["eco_won"]}）' for k, v in b1b.items()))
    bench['second_round_T_by_pistol_plant'] = b1b

    # B3b 任意回合反 eco 被翻 → 下一回合（同半场），下一回合人均开局钱不够全起（T < $3900 / CT < $4400）时怎么买
    b3b = {}
    for grp in ('high', 'pro'):
        rows_ = []
        for row in S[(S.group == grp) & (S.econ_sit == '反eco') & (~S.won)].itertuples():
            nr = row.round + 1
            if nr in (HR + 1, 2 * HR + 1) or nr > 2 * HR or (row.demo, nr, row.side) not in SI.index:
                continue
            nx = SI.loc[(row.demo, nr, row.side)]
            need = FULL_T if row.side == 'T' else FULL_CT
            rows_.append(dict(side=row.side, pre=float(nx.pre), buy=nx.buy, won=bool(nx.won), can=nx.pre >= need))
        X = pd.DataFrame(rows_)
        y = X[~X.can]
        b3b[grp] = dict(n_all=len(X), buys_all=dict(Counter(X.buy)), n_cannot=len(y),
                        buys={k: dict(n=int((y.buy == k).sum()), won=int(y[y.buy == k].won.sum())) for k in Counter(y.buy)},
                        avg_pre=round(float(y.pre.mean())) if len(y) else None)
        P(f'[B3b 反 eco 被翻 → 下一回合全起不了] {grp}: n={len(y)}（人均 ${b3b[grp]["avg_pre"]}）：'
          + '；'.join(f'{k} {v["n"]}（赢 {v["won"]}）' for k, v in b3b[grp]['buys'].items()) + f'；所有反 eco 被翻的下一回合 {dict(Counter(X.buy))}')
    bench['after_reverse_eco_lost_cannot_full'] = b3b



    # ================================================================ 每回合输出（人工判断交给规则层）
    U = {rec['round']: rec['us'] for rec in ROWS}

    def d(v):
        return f'${int(v)}'

    def userp(r):
        mine = [p_ for p_ in U[r]['players'] if p_['user']]
        return mine[0] if mine else None

    def buy_text(r):
        u = U[r]
        me = userp(r)
        if me is None:
            my = '（你这回合不在场）'
        else:
            my = WCN.get(me['primary'], me['primary']) if me['primary'] else WCN.get(me['pistol'], me['pistol'])
        arm = f'{u["n_helmet"]} 人头盔甲' + (f'、{u["n_armor"] - u["n_helmet"]} 人只有甲' if u['n_armor'] > u['n_helmet'] else '')
        return f'{u["buy"]}：{u["guns"]}；{arm}；道具 {u["n_nades"]} 颗；你拿 {my}'

    P('\n================ 逐回合买法 ================')
    score = {'us': 0, 'them': 0}
    OUT_ROUNDS = []
    for rec in ROWS:
        r = rec['round']
        econ = dict(ours=f'人均 {d(U[r]["avg_start"])}（{d(U[r]["min_start"])}–{d(U[r]["max_start"])}）', buy=buy_text(r))
        sb = f'{score["us"]}:{score["them"]}'
        if rec['won']:
            score['us'] += 1
        else:
            score['them'] += 1
        us_b = {k: v for k, v in rec['us'].items() if k not in ('loss_counter', 'k_after')}
        th_b = {k: v for k, v in rec['them'].items() if k not in ('loss_counter', 'k_after')}
        OUT_ROUNDS.append(dict(
            round=r, side=rec['side'], score_before=sb, result='赢' if rec['won'] else '输', reason=rec['reason'], plant=rec['plant'],
            us=us_b, them=th_b,
            loss_counter=dict(us=rec['us']['loss_counter'], them=rec['them']['loss_counter']),
            loss_counter_after=dict(us=rec['us']['k_after'], them=rec['them']['k_after']),
            next_if=dict(us=dict(win=rec['us']['if_win_next'], loss=rec['us']['if_loss_next']),
                         them=dict(win=rec['them']['if_win_next'], loss=rec['them']['if_loss_next'])),
            economy=econ))
        P(f"R{r:2d} {rec['side']:2s} {sb} {'赢' if rec['won'] else '输'} | {econ['ours']} | {econ['buy']}")

    # ================================================================ 规则说明（按实测值拼）
    WIN_TXT = '清零' if WIN_RULE == 'reset' else f' −{WIN_RULE}（最低 0）'
    zero_txt = '；'.join(f'第 {a} 回合 {"、".join(b)}' for a, b in zip(t_zero['round'], t_zero.zero))
    RULES_OUT = dict(
        start_money=f'手枪局（第 1、{HR + 1} 回合）每人 $800'
                    + ''.join(f'；加时第 {lo} 回合开局每人 ${v}（实测）' for lo, v in sorted(ot_start.items())),
        loss_bonus=dict(table={str(i + 1): BASE[i] for i in range(5)},
                        text=f'输一回合的奖励 = $1400 + $500 ×（输完后的连败计数 − 1），计数 1–5 对应 $1400/$1900/$2400/$2900/$3400，最多 $3400。'
                             f'每个半场开始计数是 {START_K}，所以手枪局输了直接拿 $1900。'),
        after_win=(f'赢一回合连败计数{WIN_TXT}。用 {len(LR)} 次败方奖励检验：'
                   + '，'.join(f'{WR_CN[w]} 吻合 {models[(w, START_K)][1]}/{len(LR)}' for w in ('reset', 1, 2)) + '。'),
        after_win_evidence=evid,
        win_reward=dict(elimination=WIN_ELIM, bomb_exploded=WIN_BOMB, bomb_defused=WIN_DEFUSE,
                        time_ran_out_ct=win_reward.get('time_ran_out', {}).get('amount'), detail=win_reward,
                        note='本场没有炸包赢的回合，炸包 $3500 是按 CS2 规则写的，未实测' if 'target_bombed' not in win_reward else ''),
        ct_team_kill_bonus='回合结束后 CT 方每人再拿 $50 × 本回合 CT 杀掉的 T 人数（含回合结束后的击杀），输赢都拿；T 方没有这一项。'
                           f'{len(ct)} 个可测回合吻合 {ct_ok * 100:.0f}%。',
        plant=dict(personal=PLANT_PERSONAL, team_if_lost=PLANT_TEAM,
                   text=f'T 下包：下包的人立刻 +${PLANT_PERSONAL}；下了包但输了，T 全队每人在败方奖励之外再 +${PLANT_TEAM}（不是 $800）。'),
        defuse_personal=mode(defuse_personal) if defuse_personal else None,
        t_survive_timeout=('T 方没下包、活到时间结束的人拿不到败方奖励（$0）'
                           + (f'：{zero_txt}。' if zero_txt else '；本场没有出现。')),
        kill_reward={k: v['reward'] for k, v in kill_reward.items()},
        kill_reward_note=('实测（击杀前后 balance 差的众数）：'
                          + '，'.join(f'{k} ${v["reward"]}' for k, v in sorted(kill_reward.items()))
                          + '。本场没出现的击杀方式没有实测值。'),
        kill_reward_detail=kill_reward,
        cap=16000,
        full_buy_need=dict(T=FULL_T, CT=FULL_CT, AWP=AWP_NEED,
                           text=f'本文"够全起"按开局钱 + 保下来的装备：T ≥ ${FULL_T}（AK + 头盔甲 + 一两颗道具），'
                                f'CT ≥ ${FULL_CT}（M4 + 头盔甲 + 钳子/道具），起狙 ≥ ${AWP_NEED}'),
        buy_classes=f'手枪局 = 第 1、{HR + 1} 回合；eco = 全队平均装备 < $1600 且步枪 ≤ 1 把；全起 = 步枪（含 AWP）≥ 4 把，或 3 把且平均装备 ≥ $3500；'
                    '其余按花钱比例：花掉开局钱 ≥ 75% 强起，否则半起（和对标数据 sit_rounds.csv 同口径）。',
        loss_counter_field=f'loss_counter = 回合开始时的连败计数；这回合输了计数 +1（最多 5），赢了{WIN_TXT}。',
    )

    econ_json = dict(
        meta=dict(match_no=TLM.get('match_no'), demo=DEMO, map=TLM.get('map'), final_score=TLM.get('final_score'),
                  user=TLM.get('user'), user_steamid=USER,
                  note='金额单位美元；start=开局钱，spent=本回合花费（含给队友买的），left=买完剩下，equip=买完装备价值，kept=上回合保下来的装备价值；'
                       'next_if = 回合结束前一刻的钱 + 赢/输奖励（赢按 $3250 全灭算，不含 CT 每杀 $50 的团队奖励），'
                       '是"如果这回合赢/输，下一回合大概每人多少钱"；next_actual 是下一回合实际开局。'),
        rules=RULES_OUT, bench=bench, rounds=OUT_ROUNDS)
    with open(OUT / 'economy.json', 'w', encoding='utf-8') as f:
        json.dump(clean(econ_json), f, ensure_ascii=False, indent=1)
    (OUT / 'economy_out.txt').write_text('\n'.join(out_lines), encoding='utf-8')
    P(f'\n写出 {OUT / "economy.json"}、{OUT / "economy_out.txt"}')
    print(f'连败计数模型吻合 {KH}/{len(LR)}', flush=True)
    return f'{len(OUT_ROUNDS)} 回合的经济已算好'
