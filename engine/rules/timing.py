# -*- coding: utf-8 -*-
"""「时机与转点」规则：TIME-G1（转点及时，好的操作）、TIME-01..TIME-09（问题）。

来源：rules_v1.json 的 TIME-*（trigger / blame / template / alt），原型 pro/analysis/match3/rules_proto/时机与转点.py
（及其本地修正版 tm2.py，未发布），decisions_v1 #25（TIME-G1：转点前必须有你能拿到的信号；不再用“后来真的在那边下包 /
死了 2 个 T”事后确认）。原型里的编号比 rules_v1 大一号（原 TIME-01 = 现 TIME-G1，原 TIME-02 = 现 TIME-01……）。

数据只走 ctx 和 engine.common（C-* 口径）：帧（位置/报点/速度/存活/spotted）、击杀栏、player_hurt、weapon_fire、
player_blind；道具取 timeline.json 的 enemy_util / team_util / user_util（land_place、det_t、end_t），起爆点高度取
cache 里的 ev_*_detonate。阈值全部在 thresholds/timing.json（ctx.th['timing']）。

审核单（手册 v1，2026-10-30）：
- 19 TIME-04 看第一个 T 进包点的时间（late_entry_s 还没人进 → 太晚），不再看首杀；
- 24 TIME-01 只数离交火还远、附近也没有 T 的静步，句子不写静步了几秒；
- 31 / Q16 / Q17 TIME-09 不再建议拱门烟（也不建议中路过点烟）；VIP 烟起来前就上中远、开局死在中线：
  TIME-08 思路“还可以等 VIP 烟起来再上”（合理代价），TIME-09 的 alt 也用这一句；
- Q15 T 方进点倒下、凶手 3 秒内被补掉：TIME-05 不判。

用法：from engine.rules import timing; hits = timing.run(ctx)
"""
import math

import numpy as np

from engine import common as C
from engine.rules import util as UT

RULE_IDS = ('TIME-G1', 'TIME-01', 'TIME-02', 'TIME-03', 'TIME-04', 'TIME-05', 'TIME-06', 'TIME-07', 'TIME-08', 'TIME-09')

# 道具：句子里的说法
UTIL_WORD = {'molotov': '起火', 'incendiary': '起火', 'smoke': '起烟', 'flash': '炸闪', 'he': '炸雷'}
UTIL_LANE = {'molotov': '起了火', 'incendiary': '起了火', 'smoke': '起了一颗烟', 'flash': '炸了一颗闪', 'he': '炸了一颗雷'}
UTIL_EV = {'smoke': 'ev_smokegrenade_detonate', 'flash': 'ev_flashbang_detonate', 'he': 'ev_hegrenade_detonate',
           'molotov': 'ev_inferno_startburn', 'incendiary': 'ev_inferno_startburn'}
FIRE_DMG = ('inferno', 'molotov', 'incgrenade')       # 火的伤害（player_hurt 的 weapon）
HE_DMG = ('hegrenade',)


# ================================================================ 小工具
def _th(ctx):
    return ctx.th['timing']


def _r(x):
    """整数四舍五入（不用 round 的半数取偶）"""
    return int(math.floor(float(x) + 0.5))


def _secs(ctx, s):
    """秒 → tick 数"""
    return int(round(float(s) * C.TR))




def _hit(rule, kind, rnd, t, tick, scope, subject, is_user, blame, facts, text, alt, kb, evidence,
         death_t=None, death_of=None, merge_key=None):
    """按 CONTRACT 的 Hit 格式组一条命中"""
    return dict(rule=rule, kind=kind, round=int(rnd), t=C.sec(t), tick=None if tick is None else int(tick),
                scope=scope, subject=subject, is_user=bool(is_user), blame=blame,
                death_t=C.sec(death_t), death_of=death_of, merge_key=merge_key, facts=C.plain(facts, sort_keys=True),
                text=C.tidy(text), alt=C.tidy(alt), kb=list(kb), evidence=C.tidy(evidence))


class _Memo:
    """一次 run 里的帧缓存（每 frame_step tick 一帧）"""

    def __init__(self, ctx):
        self.ctx = ctx
        self.th = _th(ctx)
        self.U = C.user_sid(ctx)
        self.uname = C.name_of(ctx, self.U)
        self.fr = {}
        self.util_z = {}

    def frames(self, sid, rnd):
        """sid 这回合（冻结结束到回合结束）的帧：[{tick, t, x, y, z, place, spd, alive}]"""
        key = (sid, int(rnd))
        if key not in self.fr:
            ctx = self.ctx
            info = C.round_info(ctx, rnd)
            out = []
            for tk in range(info['start'], info['end'] + 1, int(self.th['frame_step'])):
                p = C.pos(ctx, sid, tk)
                spd = C.move_speed(ctx, sid, tk)
                out.append(dict(tick=tk, t=C.t_of(ctx, tk, rnd), x=float(p[0]), y=float(p[1]), z=float(p[2]),
                                place=C.place_at(ctx, sid, tk), spd=0.0 if spd is None else float(spd),
                                alive=C.alive(ctx, sid, tk, rnd)))
            self.fr[key] = out
        return self.fr[key]

    def seg(self, sid, rnd, t0, t1, alive=True):
        """[t0, t1] 秒内的帧（默认只要活着的）"""
        return [f for f in self.frames(sid, rnd) if t0 <= f['t'] <= t1 and (f['alive'] or not alive)]

    def util_xyz(self, rnd, u):
        """道具起爆点 3D：timeline 只有 land_xy，高度从 cache 的起爆事件里按时间、平面位置配；配不上返回 None"""
        key = (int(rnd), u.get('kind'), u.get('det_t'), tuple(u.get('land_xy') or ()))
        if key in self.util_z:
            return self.util_z[key]
        xyz = None
        ev = UTIL_EV.get(u.get('kind'))
        xy = u.get('land_xy')
        if ev and xy and u.get('det_t') is not None:
            if u.get('land_z') is not None:
                xyz = (float(xy[0]), float(xy[1]), float(u['land_z']))
            else:
                try:
                    df = self.ctx.cache(ev)
                except (FileNotFoundError, KeyError, OSError):
                    df = None
                if df is not None and len(df):
                    tk = C.tick_of(self.ctx, rnd, u['det_t'])
                    w = df[(df['tick'] - tk).abs() <= int(self.th['util_match_ticks'])]
                    best = None
                    for r in w.sort_values('tick', kind='mergesort').itertuples():
                        d2 = math.hypot(r.x - xy[0], r.y - xy[1])
                        if d2 <= self.th['util_match_xy'] and (best is None or d2 < best[0]):
                            best = (d2, (float(r.x), float(r.y), float(r.z)))
                    if best:
                        xyz = best[1]
        self.util_z[key] = xyz
        return xyz

    def heard_util(self, rnd, u):
        """C-HEAR：道具起爆时你活着、离起爆点 3D 在 hear_shot 之内（没有高度时按你的高度算）"""
        ctx = self.ctx
        tk = C.tick_of(ctx, rnd, u['det_t'])
        if not C.alive(ctx, self.U, tk, rnd):
            return False
        xyz = self.util_xyz(rnd, u)
        if xyz is None:
            xy = u.get('land_xy')
            if not xy:
                return False
            xyz = (float(xy[0]), float(xy[1]), float(C.pos(ctx, self.U, tk)[2]))
        return C.can_hear(ctx, self.U, xyz, tk)


def _death_place(ctx, sid, tick):
    """阵亡前一刻的报点原文"""
    return C.place_at(ctx, sid, int(tick) - _secs(ctx, _th(ctx)['death_place_before_s']))


def _plant(ctx, rnd):
    """timeline 的下包（site 统一成 'A' / 'B'）。facts 的 site 只在第 3 场是 A/B，别的 demo 是包点实体号，
    这时按下包人所在报点（BombsiteA / BombsiteB）换算；都换不出来 site 记 None"""
    p = (C.tl_round(ctx, rnd) or {}).get('plant')
    if not p:
        return None
    p = dict(p)
    if p.get('site') not in ('A', 'B'):
        p['site'] = {'BombsiteA': 'A', 'BombsiteB': 'B'}.get(p.get('planter_place'))
    return p


def _user_death(ctx, rnd):
    """用户这回合的阵亡：(tick, 凶手 sid, 武器键) 或 None"""
    U = C.user_sid(ctx)
    d = C.deaths(ctx, rnd)
    d = d[d['vic'] == U]
    if not len(d):
        return None
    r = d.iloc[0]
    return int(r['tick']), str(r['att']), r['wk']


def _traded(ctx, rnd, killer, d_tk, within_s):
    """凶手 killer 在 d_tk 之后 within_s 秒内被你的队友打死（击杀栏）"""
    U = C.user_sid(ctx)
    ms = set(C.mates(ctx, rnd))
    dd = C.deaths(ctx, rnd)
    dd = dd[(dd['vic'] == killer) & (dd['tick'] >= int(d_tk)) & (dd['tick'] <= int(d_tk) + _secs(ctx, within_s))]
    return any(a in ms and a != U for a in dd['att'])


def _halves(ctx):
    """{回合: 半场序号（1 上半场、2 下半场、3 起加时每个半场）}；按赛制切（C.half），
    加时上半场和常规下半场同一边时也分开算"""
    return {r['n']: C.half(ctx, r['n']) + 1 for r in C.rounds(ctx)}


def _mate_deaths_in(ctx, rnd, zone):
    """击杀栏里我方队友（不含你）阵亡、阵亡前在 zone：[(t, tick, sid, 报点原文)]"""
    U = C.user_sid(ctx)
    ms = set(C.mates(ctx, rnd))
    out = []
    for r in C.deaths(ctx, rnd).itertuples():
        if r.vic in ms and r.vic != U:
            p = _death_place(ctx, r.vic, r.tick)
            if p in zone:
                out.append((C.t_of(ctx, r.tick, rnd), int(r.tick), r.vic, p))
    return out


# ================================================================ CT 转点段（TIME-G1、TIME-01 共用）
def _side_here(th, f):
    """这一帧在哪一侧守点区：'A' / 'B' / None"""
    p = f['place']
    if p in th['b_hold']:
        return 'B'
    if p in th['a_hold'] or (p in th['a_src_extra'] and f['y'] < th['a_ctspawn_y']):
        return 'A'
    return None


def _rotations(m, rnd):
    """CT 用户从一侧守点区第一次进入另一侧的段：[{src, dst, leave(帧), arr(帧)}]。
    出发 = 到达前 rot_window_s 秒内最后一次原地停住（连续 still_frames 帧速度 < still_speed，停在出发一侧）之后，
    第一次连续 move_frames 帧速度 > move_speed 的起点；一次也没停过取最后一次在出发一侧的那一帧。"""
    th = m.th
    fr = [f for f in m.frames(m.U, rnd) if f['alive']]
    out = []
    side_now, last_side_f = None, None
    for f in fr:
        here = _side_here(th, f)
        if here is None:
            continue
        if side_now and here != side_now:
            src_ok = set(th['b_hold']) if side_now == 'B' else set(th['a_hold']) | set(th['a_src_extra'])
            w = [g for g in fr if f['t'] - th['rot_window_s'] <= g['t'] < f['t']]
            leave = last_side_f
            run = 0
            nmv = int(th['move_frames'])
            for j, g in enumerate(w):
                run = run + 1 if g['spd'] < th['still_speed'] else 0
                if run >= th['still_frames'] and g['place'] in src_ok:
                    leave = g
                    for k in range(j + 1, len(w) - nmv + 1):
                        if all(x['spd'] > th['move_speed'] for x in w[k:k + nmv]):
                            leave = w[k]
                            break
            out.append(dict(src=side_now, dst=here, leave=leave, arr=f))
        side_now, last_side_f = here, f
    return out


def _dst_zones(th, dst):
    """目标一侧的信号区（道具 / 雷达 / 枪声）和阵亡区（击杀栏）"""
    sig = set(th['a_sig'] if dst == 'A' else th['b_sig'])
    return sig, sig | set(th['a_dst_extra'] if dst == 'A' else th['b_dst_extra'])


def _rot_signals(m, rnd, dst, t_cap):
    """目标区“T 的动静”，都是你当时能拿到的（decisions #25）：[(t, tick, 类别, 句子里的说法)]，按时间排。
    ① 敌方道具在目标区起爆、你听得到（C-HEAR）；② 击杀栏里有队友在目标区阵亡；③ 我方雷达上同时有 ≥ radar_min_t 个 T
    在目标区；④ 你听得到目标区里 T 开枪（C-HEAR）。"""
    ctx, th = m.ctx, m.th
    sig_zone, dst_zone = _dst_zones(th, dst)
    out = []
    for u in (C.tl_round(ctx, rnd) or {}).get('enemy_util') or []:
        if u.get('land_place') in sig_zone and u.get('det_t') is not None and u['det_t'] <= t_cap and m.heard_util(rnd, u):
            out.append((float(u['det_t']), C.tick_of(ctx, rnd, u['det_t']), 'util',
                        f"{C.place_cn(u['land_place'])}{UTIL_WORD.get(u['kind'], '有道具')}"))
    for t, tk, sid, p in _mate_deaths_in(ctx, rnd, dst_zone):
        if t <= t_cap:
            out.append((t, tk, 'death', f"队友 {C.name_of(ctx, sid)} 在{C.place_cn(p)}阵亡"))
    # ③ 雷达：每帧数一下同时在目标区、上了我方雷达的活着的 T
    cnt = {}
    for e in C.enemies(ctx, rnd):
        for f in m.frames(e, rnd):
            if f['t'] > t_cap or not f['alive'] or f['place'] not in sig_zone:
                continue
            if C.on_radar(ctx, e, f['tick'] - int(th['frame_step']) + 1, f['tick']):
                cnt.setdefault(f['tick'], set()).add(e)
    # 每一段连续上雷达的第一帧都算一个信号（不只是这回合第一次）：调用的地方按出发前的窗口挑离出发最近的一个
    gap = int(th['sig_gap_s'] * C.TR)
    last = None
    for tk in sorted(cnt):
        if len(cnt[tk]) < th['radar_min_t']:
            continue
        if last is None or tk - last > gap:
            out.append((C.t_of(ctx, tk, rnd), tk, 'radar', f"{dst} 区同时出现{C.num_cn(len(cnt[tk]))}个 T"))
        last = tk
    # ④ 听得到的 T 枪声
    info = C.round_info(ctx, rnd)
    hs = C.heard_shots(ctx, m.U, info['start'], C.tick_of(ctx, rnd, t_cap), shooters=C.enemies(ctx, rnd))
    last = None
    for r in hs.itertuples():
        p = C.place_at(ctx, r.sid, int(r.tick))
        if p in sig_zone:
            if last is None or int(r.tick) - last > gap:
                out.append((C.t_of(ctx, r.tick, rnd), int(r.tick), 'shot', f"{C.place_cn(p)}响了枪"))
            last = int(r.tick)
    return sorted(out, key=lambda s: (s[0], s[2], s[3]))


def _pick_sig(th, sigs, leave_t):
    """出发前 [rot_lag_lo, rot_lag_hi] 秒里离出发最近的那个信号；窗口里没有返回 None。
    另返回窗口里是不是只有队友阵亡这一种信号（只有击杀栏、那边又没人了：动身是常规反应，不算亮眼）"""
    win = [x for x in sigs if th['rot_lag_lo'] <= leave_t - x[0] <= th['rot_lag_hi']]
    if not win:
        return None, False
    return win[-1], all(x[2] == 'death' for x in win)


def _anchors(m, rnd, tick, dst):
    """tick 那一刻守在 dst 一侧守点区的活着的队友（不含你）"""
    ctx, th = m.ctx, m.th
    out = []
    for mt in C.mates(ctx, rnd):
        if mt == m.U or not C.alive(ctx, mt, tick, rnd):
            continue
        p = C.pos(ctx, mt, tick)
        if _side_here(th, dict(place=C.place_at(ctx, mt, tick), y=p[1])) == dst:
            out.append(mt)
    return out


def _intent(m, rnd, leave, dst):
    """第六轮拍板 4：一次转点 / 挪位的意图。leave = 出发那一帧。
    补防 = 目标一侧只有 <= intent_anchor_max 个队友守；有动静 = 出发前 rot_lag_hi 秒内目标一侧有你能拿到的信号（decisions #25）。
    返回 dict(kind='补防' / '有动静' / '补防+有动静' / '无', anchors=[sid], signal=(t, tick, 类别, 说法) | None)"""
    th = m.th
    anchors = _anchors(m, rnd, leave['tick'], dst)
    sig, only_death = _pick_sig(th, _rot_signals(m, rnd, dst, leave['t']), leave['t'])     # 离出发最近的那一个
    weak = len(anchors) <= th['intent_anchor_max']
    kind = '补防+有动静' if weak and sig else '补防' if weak else '有动静' if sig else '无'
    return dict(kind=kind, anchors=anchors, signal=sig, only_death=only_death)


def move_intent(ctx, rnd, t0_tick, t1_tick):
    """给别的规则用（TEAM-06 你走开了）：CT 用户从 t0 走到 t1，往哪个包点靠了（平面离包点近了 >= shift_closer）、意图是什么。
    不是 CT、没往哪个包点靠返回 None"""
    if C.user_side(ctx, rnd) != 'CT':
        return None
    m = getattr(ctx, '_time_memo', None)
    if m is None:
        m = _Memo(ctx)
        try:
            setattr(ctx, '_time_memo', m)
        except AttributeError:
            pass
    th = m.th
    p0, p1 = C.pos(ctx, m.U, t0_tick), C.pos(ctx, m.U, t1_tick)
    best = None
    for site, xy in sorted(th['site_xy'].items()):
        d = math.hypot(p0[0] - xy[0], p0[1] - xy[1]) - math.hypot(p1[0] - xy[0], p1[1] - xy[1])
        if d >= th['shift_closer'] and (best is None or d > best[1]):
            best = (site, d)
    if best is None:
        return None
    leave = dict(tick=int(t0_tick), t=C.t_of(ctx, t0_tick, rnd))
    out = _intent(m, rnd, leave, best[0])
    out['dst'] = best[0]
    return out


def _intent_clause(ctx, it, dst):
    """意图写成半句：“B 只有 队友C 一个人守”/“B 没人守”"""
    if not it or '补防' not in it['kind']:
        return ''
    if not it['anchors']:
        return f'{dst} 没人守'
    return f"{dst} 只有 {'、'.join(C.name_of(ctx, x) for x in it['anchors'])} 一个人守"


def _shifts(m, rnd):
    """CT 用户没进另一侧守点区、但明显往另一侧挪过去的一段（在 CT 出生点里从 A 那头挪到超市那头）：
    两次原地停住之间（<= rot_window_s 秒），平面离目标包点近了 >= shift_closer、离原来那个包点远了 >= shift_away。
    [{src, dst, leave(帧), arr(帧)}]"""
    th = m.th
    fr = [f for f in m.frames(m.U, rnd) if f['alive']]
    stills, run = [], 0
    for i, f in enumerate(fr):
        run = run + 1 if f['spd'] < th['still_speed'] else 0
        if run == th['still_frames']:
            stills.append(i)
    out = []
    xy = th['site_xy']

    def d(f, s_):
        return math.hypot(f['x'] - xy[s_][0], f['y'] - xy[s_][1])
    for a, b in zip(stills, stills[1:]):
        # 出发 = 前一次停住之后第一帧动起来的；到达 = 后一次停住那一帧
        j = next((k for k in range(a, b) if fr[k]['spd'] >= th['move_speed']), None)
        if j is None:
            continue
        leave, arr = fr[j], fr[b]
        if arr['t'] - leave['t'] > th['rot_window_s']:
            continue
        for dst, src in (('A', 'B'), ('B', 'A')):
            if d(leave, dst) - d(arr, dst) >= th['shift_closer'] and d(arr, src) - d(leave, src) >= th['shift_away'] \
                    and d(leave, src) < d(leave, dst):
                out.append(dict(src=src, dst=dst, leave=leave, arr=arr))
    return out


def _fight_points(m, rnd, dst, t0, t1, plant):
    """TIME-01：目标区 [t0, t1] 里的交火点 [(tick, xyz)]：击杀栏里有队友参与、阵亡前在目标区的击杀（双方当时的位置），
    你听得到的、开枪人在目标区信号区的 T 枪声（开枪位置），目标点下包（下包人位置）"""
    ctx, th = m.ctx, m.th
    sig_zone, dst_zone = _dst_zones(th, dst)
    ms = set(C.mates(ctx, rnd))
    tk0, tk1 = C.tick_of(ctx, rnd, t0), C.tick_of(ctx, rnd, t1)
    out = []
    for r in C.deaths(ctx, rnd).itertuples():
        if not (tk0 <= int(r.tick) <= tk1):
            continue
        if any(who in ms and _death_place(ctx, who, r.tick) in dst_zone for who in (r.vic, r.att)):
            for who in (r.vic, r.att):
                if who:
                    out.append((int(r.tick), tuple(float(x) for x in C.pos(ctx, who, int(r.tick)))))
    info = C.round_info(ctx, rnd)
    hs = C.heard_shots(ctx, m.U, max(info['start'], tk0), tk1, shooters=C.enemies(ctx, rnd))
    for r in hs.itertuples():
        if C.place_at(ctx, r.sid, int(r.tick)) in sig_zone:
            out.append((int(r.tick), (float(r.user_X), float(r.user_Y), float(r.user_Z))))
    if plant and plant.get('site') == dst and t0 <= plant['t'] <= t1 and plant.get('planter'):
        ptk = C.tick_of(ctx, rnd, plant['t'])
        sid = C.sid_of(ctx, plant['planter'])          # timeline 里是名字
        if sid:
            out.append((ptk, tuple(float(x) for x in C.pos(ctx, sid, ptk))))
    return sorted(out)


def _walk_near(m, rnd, f, fpts):
    """TIME-01 的静步帧 f 是不是“已经靠近对面”：离到这一帧为止的交火点 3D < walk_far_from_fight，
    或 walk_enemy_near 内有活着的 T（真实位置：只用来豁免，不拿来判错）"""
    ctx, th = m.ctx, m.th
    me = (f['x'], f['y'], f['z'])
    if any(tk <= f['tick'] and C.dist3(me, p) < th['walk_far_from_fight'] for tk, p in fpts):
        return True
    for e in C.enemies(ctx, rnd):
        if C.alive(ctx, e, f['tick'], rnd) and C.dist3(C.pos(ctx, e, f['tick']), me) < th['walk_enemy_near']:
            return True
    return False


def _rule_rotation(m, hits):
    """TIME-G1（转点及时）和 TIME-01（转点路上开打了还静步）"""
    ctx, th, U = m.ctx, m.th, m.U
    for ri in C.rounds(ctx):
        rnd = ri['n']
        if C.user_side(ctx, rnd) != 'CT':
            continue
        plant = _plant(ctx, rnd)
        for rot in _rotations(m, rnd):
            src, dst, leave, arr = rot['src'], rot['dst'], rot['leave'], rot['arr']
            if leave is None:
                continue
            sig_zone, dst_zone = _dst_zones(th, dst)
            mkey = f"rot:{rnd}:{arr['tick']}"
            # ---------------- TIME-G1
            sigs = _rot_signals(m, rnd, dst, arr['t'])
            # 出发前窗口里离出发最近的信号（不是这回合第一个：B 小更早的一次交火会把真正让你动身的那一下挤掉，d5a R6）
            pick, only_death = _pick_sig(th, sigs, leave['t'])
            if sigs:
                s_t, s_tk, s_kind, s_txt = pick if pick is not None else sigs[0]
                lag = leave['t'] - s_t
                # 出发时出发一侧还有活着的队友
                keep = []
                for mt in C.mates(ctx, rnd):
                    if not C.alive(ctx, mt, leave['tick'], rnd):
                        continue
                    p = C.pos(ctx, mt, leave['tick'])
                    if _side_here(th, dict(place=C.place_at(ctx, mt, leave['tick']), y=p[1])) == src:
                        keep.append(mt)
                before_plant = (not plant) or arr['t'] <= plant['t']
                # 从信号到到达，目标区没有（另外的）队友阵亡
                lost = [x for x in _mate_deaths_in(ctx, rnd, dst_zone) if x[1] > s_tk and x[0] <= arr['t']]
                if th['rot_lag_lo'] <= lag <= th['rot_lag_hi'] and len(keep) >= th['other_side_mates'] \
                        and before_plant and not lost:
                    until = '下包' if plant else '对面打进来'
                    keep_n = [C.name_of(ctx, x) for x in keep]
                    # 第六轮拍板 4：转点的意图——目标一侧只剩一个人守时写明是去补防
                    it_r = _intent(m, rnd, leave, dst)
                    ic = _intent_clause(ctx, it_r, dst)
                    if lag >= 0:
                        text = (f"{dst} 区一出现 T 的动静（{s_txt}）" + (f"，{ic}" if ic else '')
                                + f"，你就从 {src} 区往 {dst} 转，另一边还有队友守着，赶在{until}前到了 {dst} 侧")
                    else:
                        text = (f"{dst} 区刚出现 T 的动静（{s_txt}）时，你已经在从 {src} 区往 {dst} 转，"
                                f"赶在{until}前到了 {dst} 侧")
                    alt = ''                    # “继续这样”是夸奖，理由放 why_good
                    kb = []
                    if src == 'B':
                        alt = "还可以从 B 转 A 时往 B 小方向走，先在 B 二楼看不到你的位置停一停，看清 A 区打成什么样再进"
                    facts = dict(src=src, dst=dst, signal=s_txt, signal_kind=s_kind, signal_t=C.sec(s_t),
                                 leave_t=C.sec(leave['t']), arrive_t=C.sec(arr['t']),
                                 plant_t=C.sec(plant['t']) if plant else None, until=until, other_side_mates=keep_n,
                                 anchors=[C.name_of(ctx, x) for x in it_r['anchors']], only_death=only_death,
                                 why_good=f'{dst} 区一有动静就动身，另一边还留着队友，赶在{until}前到了')
                    ev = (f"第 {C.fmt_sec(s_t)} 秒{s_txt}，第 {C.fmt_sec(leave['t'])} 秒你从 {src} 区动身，"
                          f"第 {C.fmt_sec(arr['t'])} 秒到 {dst} 侧" + (f"，第 {C.fmt_sec(plant['t'])} 秒下包" if plant else ''))
                    hits.append(_hit('TIME-G1', '好', rnd, leave['t'], leave['tick'], 'moment', m.uname, True, '—',
                                     facts, text, alt, kb, ev, merge_key=mkey))
            # ---------------- TIME-01
            # 从“出发”到进目标一侧隔了 rot_window_s 以上：中间去过别处，不是一次转点（635 高分玩家45 R15：11.3 秒离开 B、66.3 秒才到 A）
            if arr['t'] - leave['t'] > th['rot_window_s']:
                continue
            ms = set(C.mates(ctx, rnd))
            fights = []
            for r in C.deaths(ctx, rnd).itertuples():
                t = C.t_of(ctx, r.tick, rnd)
                if not (leave['t'] - th['fight_pre_s'] <= t <= arr['t']):
                    continue
                for who in (r.vic, r.att):
                    if who in ms and _death_place(ctx, who, r.tick) in dst_zone:
                        fights.append((t, int(r.tick), 'kill'))
                        break
            if plant and plant.get('site') == dst and leave['t'] - th['fight_pre_s'] <= plant['t'] <= arr['t']:
                ptk = C.tick_of(ctx, rnd, plant['t'])
                if any(C.alive(ctx, x, ptk, rnd) and C.place_at(ctx, x, ptk) in dst_zone for x in sorted(ms)):
                    fights.append((float(plant['t']), ptk, 'plant'))
            if not fights:
                continue
            f_t, f_tk, f_kind = sorted(fights)[0]
            # 到了目标一侧的外围（CT 出生点 / Jungle 这种阵亡区）、在那里停下来守了，才算到了：之后站着守不算“路上静步”
            # （e6b R20）。进了外围还一路往目标点走的不算到（8d R10：39 秒从超市那头进 CT 出生点，一直静步走到 52 秒）
            seg = m.seg(U, rnd, leave['t'], arr['t'])
            arr_f, run = arr, 0
            for f in seg:
                run = run + 1 if (f['place'] in dst_zone and f['spd'] < th['still_speed']) else 0
                if run >= th['still_frames']:
                    arr_f = f
                    break
            # 审核单 24（手册 CT-D）：只数离交火还远、附近也没有 T 的静步帧——走到对面可能听到的范围再静步是对的
            # （8d R10：43.6–50 秒每一帧离最近的 T 都在 1000 左右，不再判）
            fpts = _fight_points(m, rnd, dst, leave['t'] - th['fight_pre_s'], arr_f['t'], plant)
            walk = 0.0
            step_s = th['frame_step'] / C.TR
            for f in m.seg(U, rnd, max(f_t, leave['t']), arr_f['t']):
                if not (th['walk_lo'] <= f['spd'] <= th['walk_hi']):
                    continue
                if _walk_near(m, rnd, f, fpts):
                    continue
                walk += step_s
            if walk < th['walk_s']:
                continue
            transit = None
            if src == 'B':
                shop = [f for f in m.seg(U, rnd, arr_f['t'] - th['rot_window_s'], arr_f['t']) if f['place'] == 'Shop'
                        and f['t'] < arr_f['t']]
                t_from = shop[-1]['t'] if shop else leave['t']
                transit = arr_f['t'] - t_from
                if transit <= th['ba_transit_s']:
                    continue
            us, them = C.alive_counts(ctx, f_tk + 1, lag_s=0)
            dead = [x for x in _mate_deaths_in(ctx, rnd, dst_zone) if f_t <= x[0] <= arr_f['t'] + th['mate_dead_after_s']]
            dead_n = [C.name_of(ctx, x[2]) for x in dead]
            walk_i = _r(walk)
            # 不写静步了几秒（审核单 24）
            text = f"{dst} 区已经开打（打成 {C.alive_str(us, them)}），离 {dst} 区还远的那一段你还在静步"
            if dead_n:
                text += f"；你到 {dst} 侧前后 {'、'.join(dead_n)}{'先后' if len(dead_n) > 1 else ''}倒下"
            # 身上还有烟才写“先补一颗烟”（e6b R20：eco 只有沙鹰、没有道具）
            has_smoke = '烟' in UT._nades_at(ctx, U, f_tk)
            alt = (f"还可以在 {dst} 区一开打、身边没看到人时就切刀跑，到 {dst} 侧先补一颗烟再进" if has_smoke else
                   f"还可以在 {dst} 区一开打、身边没看到人时就切刀跑到 {dst} 侧，再找位置")
            facts = dict(src=src, dst=dst, fight_t=C.sec(f_t), fight_kind=f_kind, us=us, them=them,
                         alive=C.alive_str(us, them), walk_s=walk_i, walk_s_raw=round(walk, 2),
                         leave_t=C.sec(leave['t']), arrive_t=C.sec(arr_f['t']),
                         transit_s=None if transit is None else C.sec(transit), mates_down=dead_n,
                         mates_down_t=[C.sec(x[0]) for x in dead], buy=C.buy(ctx, rnd))
            ev = (f"第 {C.fmt_sec(f_t)} 秒 {dst} 区开打，你第 {C.fmt_sec(leave['t'])} 秒从 {src} 区动身、"
                  f"第 {C.fmt_sec(arr_f['t'])} 秒才到 {dst} 侧，离交火还远、附近也没有 T 的那一段一直在静步")
            # 一个时刻（开打那一秒），不是回合层面：回合层面判到你的问题页面上没有地方写（8d R10 整条没出现）
            hits.append(_hit('TIME-01', '问题', rnd, f_t, f_tk, 'moment', m.uname, True, '你的责任', facts, text, alt,
                             [], ev, merge_key=mkey))


def _rule_shift(m, hits):
    """第六轮拍板 4：没进另一侧守点区的挪位（_shifts）也按意图判：
    出发前目标一侧有你能拿到的动静（decisions #25）才夸（TIME-G1），句子写明意图（那边只剩一个人守 = 去补防）；
    只是补防、没有动静：合理，不写；没有理由的挪动由用到挪动的规则自己写（TEAM-06）。已经算成一次转点的不重复"""
    ctx, th = m.ctx, m.th
    for ri in C.rounds(ctx):
        rnd = ri['n']
        if C.user_side(ctx, rnd) != 'CT':
            continue
        plant = _plant(ctx, rnd)
        rots = _rotations(m, rnd)
        for sh in _shifts(m, rnd):
            src, dst, leave, arr = sh['src'], sh['dst'], sh['leave'], sh['arr']
            if plant and leave['t'] >= plant['t']:
                continue                                  # 下包后是回防，不是挪位
            if any(r['leave'] is not None and r['leave']['t'] <= arr['t'] and leave['t'] <= r['arr']['t'] for r in rots):
                continue                                  # 这一段已经算成一次转点
            it = _intent(m, rnd, leave, dst)
            if it['signal'] is None:
                continue
            keep = _anchors(m, rnd, leave['tick'], src)
            if len(keep) < th['other_side_mates']:
                continue
            s_t, s_tk, s_kind, s_txt = it['signal']
            ic = _intent_clause(ctx, it, dst)
            src_p = C.place_cn(leave['place'], default='')
            text = (f"{dst} 区有 T 的动静（{s_txt}）" + (f"，{ic}" if ic else '')
                    + f"，你就{'从' + src_p if src_p else ''}往 {dst} 那边挪，另一边还有队友守着")
            why = ((f'{dst} 一有动静、那边已经没人守了，就先往那边挪去补防' if not it['anchors'] else
                    f'{dst} 一有动静、那边又只剩一个人守，就先往那边挪去补防') if ic else f'{dst} 一有动静就往那边挪，回防的路短了')
            facts = dict(src=src, dst=dst, signal=s_txt, signal_kind=s_kind, signal_t=C.sec(s_t), leave_t=C.sec(leave['t']),
                         arrive_t=C.sec(arr['t']), intent=it['kind'], anchors=[C.name_of(ctx, x) for x in it['anchors']],
                         other_side_mates=[C.name_of(ctx, x) for x in keep], branch='挪位', why_good=why,
                         only_death=it['only_death'])
            ev = (f"第 {C.fmt_sec(s_t)} 秒{s_txt}，第 {C.fmt_sec(leave['t'])} 秒你开始往 {dst} 那边挪，"
                  f"第 {C.fmt_sec(arr['t'])} 秒停下")
            hits.append(_hit('TIME-G1', '好', rnd, leave['t'], leave['tick'], 'moment', m.uname, True, '—',
                             facts, text, '', [], ev, merge_key=f"rot:{rnd}:{arr['tick']}"))


# ================================================================ TIME-02  火还在烧就一人早到 A1
def _rule_a1(m, hits):
    ctx, th, U = m.ctx, m.th, m.U
    for ri in C.rounds(ctx):
        rnd = ri['n']
        if C.user_side(ctx, rnd) != 'T' or C.buy(ctx, rnd) not in th['long_or_force_buys']:
            continue
        fr = [f for f in m.frames(U, rnd) if f['alive']]
        runs, cur = [], None                                  # 连续同一报点的段
        for f in fr:
            if cur and cur[0] == f['place']:
                cur[2] = f
            else:
                if cur:
                    runs.append(cur)
                cur = [f['place'], f, f]
        if cur:
            runs.append(cur)
        R = (C.tl_round(ctx, rnd) or {})
        dth = _user_death(ctx, rnd)
        for p, fa, fb in runs:
            if p not in th['a1_places']:
                continue
            a, b = fa['t'], fb['t']
            if b - a < th['a1_stay_s']:
                continue
            seg = [f for f in fr if a <= f['t'] <= b]
            xs = np.array([f['x'] for f in seg])
            ys = np.array([f['y'] for f in seg])
            spread = float(np.quantile(np.hypot(xs - np.median(xs), ys - np.median(ys)), th['a1_spread_q']))
            if spread > th['a1_spread']:
                continue
            # ① 进来时 CT 的火还在这里烧（你自己看得到）
            burn = [u for u in R.get('enemy_util') or [] if u.get('land_place') == p
                    and u.get('kind') in ('molotov', 'incendiary') and u.get('det_t') is not None
                    and u['det_t'] <= a + th['burn_pre_s'] and (u.get('end_t') or -1e9) >= a - th['burn_post_s']]
            if not burn:
                continue
            # ③ 停留期间没有队友在这里待过一半以上
            mate_here = []
            for mt in C.mates(ctx, rnd):
                ff = m.seg(mt, rnd, a, b)
                if ff and sum(1 for f in ff if f['place'] == p) / len(seg) > th['a1_mate_frac']:
                    mate_here.append(mt)
            if mate_here:
                continue
            # ④ 吃了 CT 的道具伤害，或死在这里
            h = C.hurts(ctx, rnd)
            t0, t1 = fa['tick'], fb['tick'] + _secs(ctx, th['a1_hurt_after_s'])
            h = h[(h['vic'] == U) & (h['tick'] >= t0) & (h['tick'] <= t1) & h['wk'].isin(FIRE_DMG + HE_DMG)]
            h = h[[C.is_enemy(ctx, U, x, rnd) for x in h['att']]] if len(h) else h
            died = dth is not None and t0 <= dth[0] <= fb['tick'] + _secs(ctx, th['a1_death_after_s'])
            if not len(h) and not died:
                continue
            dmg = int(h['dmg'].sum()) if len(h) else 0
            kinds = sorted({'雷' if w in HE_DMG else '火' for w in h['wk']}, key=lambda x: ('雷', '火').index(x))
            t_hit = C.t_of(ctx, int(h['tick'].iloc[0]), rnd) if len(h) else a
            tk_hit = int(h['tick'].iloc[0]) if len(h) else fa['tick']
            stay = _r(b - a)
            pc = C.place_cn(p)
            # 不写等了几秒、掉了几血（用户写作规则：不写延迟秒数、伤害数字）
            text = f"CT 的火还在{pc}烧你就到了，队友都还没过来，你一个人在{pc}等着"
            if dmg:
                text += f"，中途吃{'和'.join(kinds)}掉了血"
            hp_left = None
            if not died:
                hp_left = int(C.hp(ctx, U, fb['tick']))
                if dmg and hp_left < 100:
                    text += "，进点时已经不是满血"
            if died:
                text += "，最后死在这里"
            alt = "还可以等 A1 的火烧完、队友到了再一起上，不白吃 CT 开局丢 A1 的道具，进点还是满血"
            bu = burn[0]
            facts = dict(place=pc, arrive_t=C.sec(a), leave_t=C.sec(b), stay=stay, spread_q=round(spread),
                         burn_by=bu.get('who'), burn_det_t=C.sec(bu['det_t']), burn_end_t=C.sec(bu.get('end_t')),
                         dmg=dmg, dmg_kinds=kinds, hit_t=C.sec(t_hit), hp=hp_left, died=died, buy=C.buy(ctx, rnd))
            ev = (f"CT 的火第 {C.fmt_sec(bu['det_t'])} 秒起、第 {C.fmt_sec(bu.get('end_t'))} 秒灭，你第 {C.fmt_sec(a)} 秒到{pc}，"
                  f"一直待到第 {C.fmt_sec(b)} 秒" + (f"，第 {C.fmt_sec(t_hit)} 秒吃道具" if dmg else ''))
            hits.append(_hit('TIME-02', '问题', rnd, t_hit, tk_hit, 'moment', m.uname, True, '你的责任', facts, text, alt,
                             [], ev,
                             death_t=C.t_of(ctx, dth[0], rnd) if died else None, death_of=m.uname if died else None))


# ================================================================ TIME-03  下水道有动静还原地架
def _rule_lane(m, hits):
    ctx, th, U = m.ctx, m.th, m.U
    for ri in C.rounds(ctx):
        rnd = ri['n']
        if C.user_side(ctx, rnd) != 'CT':
            continue
        dth = _user_death(ctx, rnd)
        if dth is None:
            continue
        d_tk, killer, _ = dth
        d_t = C.t_of(ctx, d_tk, rnd)
        my_p = _death_place(ctx, U, d_tk)
        lanes = th['lanes'].get(my_p)
        if not lanes or killer not in C.enemies(ctx, rnd) or _death_place(ctx, killer, d_tk) not in lanes:
            continue
        R = (C.tl_round(ctx, rnd) or {})
        sig = []
        for u in R.get('enemy_util') or []:
            if u.get('land_place') in lanes and u.get('det_t') is not None and u['det_t'] < d_t and m.heard_util(rnd, u):
                sig.append((float(u['det_t']), 'util', f"{C.place_cn(u['land_place'])}里{UTIL_LANE.get(u['kind'], '有道具')}"))
        cut = d_t - th['lane_radar_cut_s']
        for e in C.enemies(ctx, rnd):
            for f in m.frames(e, rnd):
                if f['t'] > cut:
                    break
                if f['alive'] and f['place'] in lanes and \
                        C.on_radar(ctx, e, f['tick'] - int(th['frame_step']) + 1, f['tick']):
                    sig.append((f['t'], 'radar', f"{C.place_cn(f['place'])}出现了 T"))
                    break
        if not sig:
            continue
        s_t, s_kind, s_txt = sorted(sig)[0]
        held = d_t - s_t
        if held < th['lane_hold_s']:
            continue
        s_tk = C.tick_of(ctx, rnd, s_t)
        moved = C.dist3(C.pos(ctx, U, s_tk), C.pos(ctx, U, d_tk - _secs(ctx, th['lane_pos_before_s'])))
        threw = [u for u in R.get('user_util') or [] if u.get('land_place') in lanes and s_t <= u.get('t', -1) <= d_t]
        if moved >= th['lane_move'] or threw:
            continue
        lane_cn = C.place_cn(lanes[0])
        held_i = _r(held)
        pc = C.place_cn(my_p)
        text = f"第 {C.fmt_sec(s_t)} 秒{s_txt}，你在{pc}原地又架了 {held_i} 秒没换位置，被{lane_cn}出来的人先打死"
        if s_kind == 'util':
            alt = f"还可以听到{lane_cn}有道具，就往{lane_cn}口回一颗道具，或换到{lane_cn}打不到的位置再架"
        else:
            alt = f"还可以{lane_cn}一有动静，就往{lane_cn}口回一颗道具，或换到{lane_cn}打不到的位置再架"
        killer_n = C.name_of(ctx, killer)
        facts = dict(sig_t=C.sec(s_t), sig=s_txt, sig_kind=s_kind, place=pc, lane=lane_cn, held=held_i,
                     death_t=C.sec(d_t), killer=killer_n, moved_small=True)
        ev = f"第 {C.fmt_sec(s_t)} 秒{s_txt}，你一直在{pc}，第 {C.fmt_sec(d_t)} 秒被{lane_cn}出来的 {killer_n} 打死"
        hits.append(_hit('TIME-03', '思路', rnd, d_t, d_tk, 'moment', m.uname, True, '—', facts, text, alt,
                         [], ev, death_t=d_t, death_of=m.uname))


# ================================================================ TIME-04  全队拖到太晚才打
def _first_entry(m, rnd):
    """我方（含你）第一个活着进包点（entry_places 报点）的人：(t, tick, 报点原文, sid) 或 None。
    先按帧找，再往前逐 tick 退到第一刻；同一刻按 steamid 排"""
    ctx, th = m.ctx, m.th
    places = set(th['entry_places'])
    best = None
    for sid in C.mates(ctx, rnd, include_self=True):
        prev = None
        for f in m.frames(sid, rnd):
            if f['alive'] and f['place'] in places:
                tk = f['tick']
                lo = prev['tick'] + 1 if prev is not None else f['tick']
                while tk - 1 >= lo and C.place_at(ctx, sid, tk - 1) in places and C.alive(ctx, sid, tk - 1, rnd):
                    tk -= 1
                cand = (tk, str(sid), C.place_at(ctx, sid, tk))
                if best is None or cand[:2] < best[:2]:
                    best = cand
                break
            prev = f
    if best is None:
        return None
    return C.t_of(ctx, best[0], rnd), best[0], best[2], best[1]


def _rule_late(m, hits):
    ctx, th, U = m.ctx, m.th, m.U
    for ri in C.rounds(ctx):
        rnd = ri['n']
        if C.user_side(ctx, rnd) != 'T' or C.buy(ctx, rnd) not in th['long_buys']:
            continue
        R = (C.tl_round(ctx, rnd) or {})
        if R.get('winner') == 'T':
            continue
        # 审核单 19（手册 T-A）：慢打默认本身没问题（首杀晚不影响胜负），看第一个 T 进包点的时间
        line = float(th['late_entry_s'])
        plant = _plant(ctx, rnd)
        if plant and plant['t'] < line:
            continue
        ent = _first_entry(m, rnd)
        if ent is not None and ent[0] < line:
            continue
        line_tk = C.tick_of(ctx, rnd, line)
        if ent is None and (line_tk > ri['end']
                            or not any(C.alive(ctx, x, line_tk, rnd) for x in C.mates(ctx, rnd, include_self=True))):
            continue                       # 不到这个时间人就死光了 / 回合已经结束：不是进点太晚
        # 到该定下打哪边的时候（late_ref_s）人数已经落后或只剩几个人：输在前面的交火，不是节奏（8d R22：60 秒 2v3、70 秒 1v3）
        us, them = C.alive_counts(ctx, C.tick_of(ctx, rnd, th['late_ref_s']))
        if us < th['late_min_alive'] or us < them:
            continue
        k = C.deaths(ctx, rnd)
        k = k[k['tick'] >= ri['start']]
        first = C.t_of(ctx, int(k['tick'].iloc[0]), rnd) if len(k) else None
        # 附带：进点前你最久待的报点（久蹲本身不判错）
        lim = ent[0] if ent is not None else line
        runs, cur = [], None
        for f in m.seg(U, rnd, 0, lim):
            if cur and cur[0] == f['place']:
                cur[2] = f['t']
            else:
                if cur:
                    runs.append(cur)
                cur = [f['place'], f['t'], f['t']]
        if cur:
            runs.append(cur)
        runs = [x for x in runs if C.place_cn(x[0], None)]
        stay = max(runs, key=lambda x: (x[2] - x[1], -x[1])) if runs else None
        stay_cn = C.place_cn(stay[0]) if stay else None
        if ent is not None:
            e_t, e_tk, e_place, e_sid = ent
            site_cn = C.place_cn(e_place)
            text = f"到第 {C.fmt_sec(e_t)} 秒才有人进{site_cn}，回合只剩不到半分钟"
            t, tk = e_t, e_tk
            ev = f"本回合第一个进包点的是 {C.name_of(ctx, e_sid)}，第 {C.fmt_sec(e_t)} 秒进{site_cn}"
        else:
            e_t = e_place = e_sid = site_cn = None
            text = f"到第 {C.fmt_sec(line)} 秒还没有人进包点，回合只剩不到半分钟"
            t, tk = line, line_tk
            ev = f"本回合到第 {C.fmt_sec(line)} 秒都没有人进包点"
        # 手册 T-A 改法：给“早点定下打哪边”，不再给中段弹墙闪探点
        alt = "还可以在回合中段就定下打哪边，给进点、下包留出时间"
        kb = []
        facts = dict(entry_t=C.sec(e_t), entry_place=site_cn, entry_by=C.name_of(ctx, e_sid) if e_sid else None,
                     late_line=C.sec(line), first_kill=C.sec(first), plant_t=C.sec(plant['t']) if plant else None,
                     stay_place=stay_cn, stay_from=C.sec(stay[1]) if stay else None, stay_to=C.sec(stay[2]) if stay else None,
                     winner=R.get('winner'), buy=C.buy(ctx, rnd))
        ev += (f"，你第 {C.fmt_sec(stay[1])} 到第 {C.fmt_sec(stay[2])} 秒一直在{stay_cn}" if stay_cn else '')
        hits.append(_hit('TIME-04', '问题', rnd, t, tk, 'round', '全队', False, '队友或团队', facts, text, alt, kb, ev))


# ================================================================ TIME-05  刚响过枪还一个人冲
def _blinds(ctx, sid=None, rnd=None):
    """player_blind（common 只给了 blind_left，这里直接取 common 读好的事件表）"""
    b = C._data(ctx).blind
    if not len(b) or 'blind_duration' not in b:
        return b.iloc[0:0] if len(b) else b
    if sid is not None:
        b = b[b['user_steamid'] == str(sid)]
    if rnd is not None:
        b = b[b['round'] == int(rnd)]
    return b


def _rule_run_into_heard(m, hits):
    ctx, th, U = m.ctx, m.th, m.U
    for ri in C.rounds(ctx):
        rnd = ri['n']
        dth = _user_death(ctx, rnd)
        if dth is None:
            continue
        d_tk, killer, _ = dth
        if killer not in C.enemies(ctx, rnd):
            continue
        d_t = C.t_of(ctx, d_tk, rnd)
        # Q15：T 方进点倒下、凶手 entry_trade_s 内被我方打死 = 进点被换了，不写
        if C.user_side(ctx, rnd) == 'T' and _death_place(ctx, U, d_tk) in th['entry_places'] \
                and _traded(ctx, rnd, killer, d_tk, th['entry_trade_s']):
            continue
        # 交火开始：死前 2 秒内凶手第一次被你看见
        see_tk = C.spots(ctx, U, killer, d_tk - _secs(ctx, th['see_back_s']), d_tk)
        if see_tk is None:
            see_tk = d_tk - _secs(ctx, th['see_default_s'])
        spd = C.move_speed(ctx, U, see_tk)
        if spd is None or spd < th['run_speed']:
            continue
        h = C.hurts(ctx, rnd)
        if len(h[(h['att'] == U) & (h['vic'] == killer) & (h['tick'] >= see_tk - _secs(ctx, th['hit_first_pre_s']))
                 & (h['tick'] <= d_tk)]):
            continue
        # 白屏的不在这里判（TIME-06）
        bl = _blinds(ctx, U, rnd)
        lo = see_tk - _secs(ctx, th['blind_pre_s'])
        if len(bl) and any(int(r.tick) <= d_tk and int(r.tick) + float(r.blind_duration) * C.TR >= lo for r in bl.itertuples()):
            continue
        # 死前 5 秒到交火开始前听到的敌方枪声，开枪人离你死前位置不远（= 你正往那边去）
        me = C.pos(ctx, U, d_tk - _secs(ctx, th['me_pos_before_s']))
        hs = C.heard_shots(ctx, U, d_tk - _secs(ctx, th['heard_back_s']), see_tk - _secs(ctx, th['heard_gap_s']),
                           shooters=C.enemies(ctx, rnd))
        # 击杀栏里交火开始前已经死了的开枪人不算（那声枪响的人已经没了）
        heard = [r for r in hs.itertuples() if C.dist3((r.user_X, r.user_Y, r.user_Z), me) <= th['heard_dst']
                 and C.alive(ctx, r.sid, see_tk, rnd)]
        if not heard:
            continue
        # 一个人：身边没有活着的队友，队友也没看到过凶手
        me_see = C.pos(ctx, U, see_tk)
        near = [x for x in C.mates(ctx, rnd) if C.alive(ctx, x, see_tk, rnd)
                and C.dist3(C.pos(ctx, x, see_tk), me_see) < th['alone_r']]
        saw = [x for x in C.mates(ctx, rnd) if C.spots(ctx, x, killer, see_tk - _secs(ctx, th['mate_see_pre_s']),
                                                        d_tk + _secs(ctx, th['mate_see_post_s'])) is not None]
        if near or saw:
            continue
        # 自己几秒前刚在这里拿下击杀：是接着打的同一波，不是“听到枪声还一个人冲”（51920faa R14）
        dd = C.deaths(ctx, rnd)
        if len(dd[(dd['att'] == U) & (dd['vic'] != U) & (dd['tick'] >= see_tk - _secs(ctx, th['own_kill_skip_s']))
                  & (dd['tick'] < see_tk)]):
            continue
        # 自己几秒前刚在开枪：人已经在这场交火里，不是“听到枪声还一个人冲”；写“一枪没开”也和刚打出去的那串对不上（56542d6d R1）
        f0 = C.fires(ctx, rnd, sid=U)
        if len(f0[(f0['tick'] >= see_tk - _secs(ctx, th['own_kill_skip_s'])) & (f0['tick'] < see_tk - _secs(ctx, th['shot_pre_s']))]):
            continue
        f = C.fires(ctx, rnd, sid=U)
        f = f[(f['tick'] >= see_tk - _secs(ctx, th['shot_pre_s'])) & (f['tick'] <= d_tk)]
        if len(f):
            fs = f.iloc[0]
            mv = C.move_word(ctx, C.fire_speed(ctx, fs), fs['wk'], bool(fs.get('user_is_scoped', False)))
            if mv == '停稳':
                continue                 # 撞上人时已经停稳开枪：不是“没做好开枪准备”
            how, first_shot = '撞上人时第一枪还没停稳，没中', mv
        else:
            how, first_shot = '一枪没开', None
        hr = heard[-1]
        sp = C.place_cn(C.place_at(ctx, hr.sid, int(hr.tick)), None)
        t_shot = C.t_of(ctx, int(hr.tick), rnd)
        awp = C.wkey(hr.wk) == 'awp'
        snd = '一枪 AWP' if awp else '枪'
        # 身边有没有队友：和 TEAM-04 同一个口径（阵亡那一刻 3D near_r 内活着的队友，8d R18 队友C 就在附近）
        pu = C.pos(ctx, U, d_tk)
        near = [x for x in C.mates(ctx, rnd) if x != U and C.alive(ctx, x, d_tk, rnd)
                and C.dist3(C.pos(ctx, x, d_tk), pu) <= ctx.th['team']['near_r']]
        alone = '抢在附近的队友前面' if near else '一个人'
        if sp:
            text = f"第 {C.fmt_sec(t_shot)} 秒{sp}方向刚响过{snd}，你还{alone}全速往{sp}那边冲，{how}，被打死"
        else:
            text = f"第 {C.fmt_sec(t_shot)} 秒前面刚响过{snd}，你还{alone}全速往前冲，{how}，被打死"
        alt = "还可以先停下静步，准星架住出人的口，看到人停稳再开枪"
        # 完整性复核：场上已经没有活着的队友（1vN）时没有“跟在队友身后”这条路（bd R24 66.1：1v4）
        if any(x != U and C.alive(ctx, x, see_tk, rnd) for x in C.mates(ctx, rnd)):
            alt += "；或跟在队友身后，他打响你能补"
        side = C.user_side(ctx, rnd)
        kb = []
        eco = C.buy(ctx, rnd) in th['eco_buys'] and C.buy(ctx, rnd, 'them') in th['long_buys']
        blame = '小问题' if eco else '你的责任'
        t_see = C.t_of(ctx, see_tk, rnd)
        killer_n = C.name_of(ctx, killer)
        facts = dict(t_shot=C.sec(t_shot), shot_place=sp, shooter=C.name_of(ctx, hr.sid), awp=awp, t_see=C.sec(t_see),
                     death_t=C.sec(d_t), killer=killer_n, first_shot=first_shot, eco=eco, near=bool(near),
                     buy=C.buy(ctx, rnd), enemy_buy=C.buy(ctx, rnd, 'them'))
        ev = (f"第 {C.fmt_sec(t_shot)} 秒听到{sp or '前面'}的枪声，第 {C.fmt_sec(t_see)} 秒你还在全速跑时撞上 {killer_n}，"
              f"第 {C.fmt_sec(d_t)} 秒被打死，" + ('附近的队友还没跟上来' if near else '身边没有队友'))
        hits.append(_hit('TIME-05', '问题', rnd, t_see, see_tk, 'moment', m.uname, True, blame, facts, text, alt, kb, ev,
                         death_t=d_t, death_of=m.uname, merge_key=f"death:{rnd}:{d_tk}"))


# ================================================================ TIME-06  被闪白了还往前冲
def _rule_blind_push(m, hits):
    ctx, th, U = m.ctx, m.th, m.U
    for ri in C.rounds(ctx):
        rnd = ri['n']
        bl = _blinds(ctx, U, rnd)
        if not len(bl):
            continue
        bl = bl[bl['blind_duration'] >= th['blind_min_s']].sort_values('tick', kind='mergesort')
        dth = _user_death(ctx, rnd)
        used = False
        for b in bl.itertuples():
            if used:
                break
            b_tk = int(b.tick)
            e_tk = b_tk + int(round(float(b.blind_duration) * C.TR))
            t0, t1 = C.t_of(ctx, b_tk, rnd), C.t_of(ctx, e_tk, rnd)
            seg = m.seg(U, rnd, t0, t1)
            if not seg:
                continue
            run_s = sum(1 for f in seg if f['spd'] > th['blind_run_speed']) * th['frame_step'] / C.TR
            if run_s < th['blind_run_s']:
                continue
            # 往前：白屏期间的位移在被闪前朝向上的分量 > 0
            yk = b_tk - _secs(ctx, th['blind_yaw_before_s'])
            p0 = C.pos(ctx, U, yk)
            yaw = math.radians(C.view(ctx, U, yk)[1])
            fwd = (seg[-1]['x'] - p0[0]) * math.cos(yaw) + (seg[-1]['y'] - p0[1]) * math.sin(yaw)
            if not fwd > 0:
                continue
            h = C.hurts(ctx, rnd)
            h = h[(h['vic'] == U) & (h['tick'] >= b_tk) & (h['tick'] <= e_tk + _secs(ctx, th['blind_after_s']))]
            h = h[[C.is_gun(w) and C.is_enemy(ctx, U, a, rnd) for w, a in zip(h['wk'], h['att'])]] if len(h) else h
            if not len(h):
                continue
            used = True
            f = C.fires(ctx, rnd, sid=U)
            n_shots = int(((f['tick'] >= b_tk) & (f['tick'] <= e_tk + _secs(ctx, th['blind_after_s']))).sum())
            died = dth is not None and b_tk <= dth[0] <= e_tk + _secs(ctx, th['blind_after_s'])
            # 白屏时被他打中、没退出来，几秒后被同一个人打死：也是这次往前冲的后果（bd R8：11.5 秒被 对手5（第4场） 打中，13.6 秒被他打死）
            late = (not died and dth is not None and str(dth[1]) == str(h['att'].iloc[0])
                    and 0 <= dth[0] - int(h['tick'].iloc[-1]) <= _secs(ctx, th['blind_follow_s']))
            died = died or late
            sp0 = C.move_speed(ctx, U, b_tk - _secs(ctx, th['blind_before_s']))
            sp0 = 0.0 if sp0 is None else sp0
            before = '本来在静步' if th['walk_lo'] <= sp0 <= th['walk_hi'] else ('本来站着' if sp0 < th['walk_lo'] else None)
            thrower = str(b.attacker_steamid)
            by_mate = thrower in C.mates(ctx, rnd)
            by_self = thrower == U
            tn = C.name_of(ctx, thrower)
            who = '自己' if by_self else (f"队友 {tn}" if by_mate else tn)
            # 旁边能先拉的队友（3D 近、活着、同一时刻没被闪白）
            alt_m = []
            me = C.pos(ctx, U, b_tk)
            for x in C.mates(ctx, rnd):
                if not C.alive(ctx, x, b_tk, rnd) or C.dist3(C.pos(ctx, x, b_tk), me) >= th['blind_mate_r']:
                    continue
                xb = _blinds(ctx, x, rnd)
                if len(xb) and (((xb['tick'] - b_tk).abs() < _secs(ctx, th['blind_mate_dt']))
                                & (xb['blind_duration'] > th['blind_mate_min_s'])).any():
                    continue
                alt_m.append(C.name_of(ctx, x))
            t_hit = C.t_of(ctx, dth[0], rnd) if died else C.t_of(ctx, int(h['tick'].iloc[0]), rnd)
            tk_hit = dth[0] if died else int(h['tick'].iloc[0])
            # 打死你的人按击杀栏（128 R15：第一下打中你的是 高分玩家11，打死你的是 高分玩家18）；只打中没打死时才用第一下打中你的人
            att_sid = dth[1] if died and C.is_enemy(ctx, U, dth[1], rnd) else str(h['att'].iloc[0])
            att_n = C.name_of(ctx, att_sid)
            # 白屏这段里你打死过人：写出来（128 R15：白着的时候打死了 高分玩家11）
            dd = C.deaths(ctx, rnd)
            kb_ = dd[(dd['att'] == U) & (dd['tick'] >= b_tk) & (dd['tick'] < tk_hit)]
            kb_n = [C.name_of(ctx, v) for v in kb_['vic'] if C.is_enemy(ctx, U, v, rnd)]
            pre = f"打死 {'、'.join(kb_n)} 后" if kb_n else ('被闪后没再开枪就' if n_shots == 0 else '')
            text = (f"第 {C.fmt_sec(t0)} 秒被{who}的闪光闪白，你{before + '，' if before else ''}没停也没退，反而往前跑，"
                    f"{pre}被 {att_n} " + ('打中，随后被他打死' if late else ('打死' if died else '打中')))    # 只说白屏这一段（之前开过枪的，8d R20）
            alt = "还可以被闪白后先往回退两步、等白屏过去再打" + (f"，让旁边没被闪的 {'、'.join(alt_m)} 先拉" if alt_m else '')
            facts = dict(t_blind=C.sec(t0), blind_end_t=C.sec(t1), by=tn, by_mate=by_mate, by_self=by_self,
                         before=before, run_s=round(run_s, 2), shots=n_shots, died=died, hit_t=C.sec(t_hit),
                         attacker=att_n, first_hit_by=C.name_of(ctx, h['att'].iloc[0]), killed_while_blind=kb_n,
                         mates_free=alt_m)
            ev = (f"第 {C.fmt_sec(t0)} 秒被{who}的闪光闪白到第 {C.fmt_sec(t1)} 秒，白屏时你在往前跑，"
                  f"第 {C.fmt_sec(t_hit)} 秒被 {facts['attacker']} {'打死' if died else '打中'}")
            blame = '你的责任 + 队友或团队' if by_mate else '你的责任'
            hits.append(_hit('TIME-06', '问题', rnd, t_hit, tk_hit, 'moment', m.uname, True, blame, facts, text, alt, [], ev,
                             death_t=t_hit if died else None, death_of=m.uname if died else None,
                             merge_key=f"death:{rnd}:{dth[0]}" if died else None))


# ================================================================ TIME-07  烟快散了还慢慢横穿
def _rule_smoke_cross(m, hits):
    ctx, th, U = m.ctx, m.th, m.U
    covers, shooters = th['smoke_covers'], th['smoke_shooters']
    for ri in C.rounds(ctx):
        rnd = ri['n']
        if C.user_side(ctx, rnd) != 'T':
            continue
        R = (C.tl_round(ctx, rnd) or {})
        sm = [dict(u, who=u.get('who') or m.uname) for u in (R.get('team_util') or []) + (R.get('user_util') or [])
              if u.get('kind') == 'smoke' and u.get('land_place') in covers and u.get('end_t') is not None]
        sm.sort(key=lambda u: (u['end_t'], u.get('det_t') or 0, u['who']))
        dth = _user_death(ctx, rnd)
        done = set()
        for s in sm:
            e = float(s['end_t'])
            lp = s['land_place']
            h = C.hurts(ctx, rnd)
            h = h[(h['vic'] == U) & (h['tick'] >= C.tick_of(ctx, rnd, e - th['smoke_pre_s']))
                  & (h['tick'] <= C.tick_of(ctx, rnd, e + th['smoke_post_s']))]
            h = h[[C.is_gun(w) and C.is_enemy(ctx, U, a, rnd) and _death_place(ctx, a, tk) in shooters[lp]
                   for w, a, tk in zip(h['wk'], h['att'], h['tick'])]] if len(h) else h
            if not len(h):
                continue
            h_tk = int(h['tick'].iloc[0])
            if h_tk in done:
                continue
            t_h = C.t_of(ctx, h_tk, rnd)
            my_p = _death_place(ctx, U, h_tk)
            if my_p not in covers[lp]:
                continue
            mv = [f['spd'] for f in m.seg(U, rnd, e - th['smoke_walk_from_s'], t_h) if f['spd'] > th['smoke_move_min']]
            if not mv or float(np.median(mv)) > th['smoke_walk']:
                continue
            fast = []
            for x in C.mates(ctx, rnd):
                fm = [f['spd'] for f in m.seg(x, rnd, e - th['smoke_mate_from_s'], e) if f['place'] in covers[lp]]
                if len(fm) >= th['smoke_mate_frames'] and float(np.median(fm)) > th['smoke_mate_speed']:
                    fast.append(C.name_of(ctx, x))
            done.add(h_tk)
            last_tk = int(h['tick'].iloc[-1])
            died = dth is not None and abs(dth[0] - last_tk) < _secs(ctx, th['smoke_died_s'])
            att = str(h['att'].iloc[0])
            shp = C.place_cn(_death_place(ctx, att, h_tk))
            spc = C.place_cn(lp)
            pc = C.place_cn(my_p)
            text = f"{spc}的烟第 {C.fmt_sec(e)} 秒就要散了，你还静步横穿{pc}，烟一散就被{shp}的人{'打死' if died else '打中'}"
            if fast:
                text += f"；{'、'.join(fast)} 同一段路是跑过去的"
            alt = f"还可以赶在{spc}烟散前切刀跑过{pc}，不在空地上静步等烟散；横穿前也可以往 VIP、梯子间一侧丢一颗闪再过"
            facts = dict(smoke_place=spc, smoke_by=s['who'], det_t=C.sec(s.get('det_t')), end_t=C.sec(e), place=pc,
                         shooter_place=shp, shooter=C.name_of(ctx, att), hit_t=C.sec(t_h), died=died,
                         death_t=C.sec(C.t_of(ctx, dth[0], rnd)) if died else None, fast_mates=fast)
            ev = (f"{s['who']} 的{spc}烟第 {C.fmt_sec(s.get('det_t'))} 秒起、第 {C.fmt_sec(e)} 秒散，"
                  f"你在{pc}一直静步，第 {C.fmt_sec(t_h)} 秒被{shp}的 {facts['shooter']} {'打死' if died else '打中'}")
            hits.append(_hit('TIME-07', '问题', rnd, t_h, h_tk, 'moment', m.uname, True, '你的责任', facts, text, alt,
                             [], ev, death_t=C.t_of(ctx, dth[0], rnd) if died else None,
                             death_of=m.uname if died else None,
                             merge_key=f"util:{rnd}:smoke:{C.sec(s.get('det_t'))}"))


# ================================================================ TIME-08 / TIME-09  开局中远被 AWP
def _awp_mid_kills(ctx, th, rnd):
    """击杀栏里这回合对面 AWP 在中远/中路杀我方的人：[(t, tick, 凶手 sid, 死者 sid)]"""
    ms = set(C.mates(ctx, rnd, include_self=True))
    out = []
    for r in C.deaths(ctx, rnd).itertuples():
        if C.wkey(r.wk) == 'awp' and r.vic in ms and r.att not in ms and _death_place(ctx, r.vic, r.tick) in th['mid_open']:
            out.append((C.t_of(ctx, r.tick, rnd), int(r.tick), r.att, r.vic))
    return out


def _arch_smoke(ctx, th, rnd, t, place=None):
    """t 时我方有没有生效中的拱门烟（place 给了就看这个报点的烟）"""
    R = (C.tl_round(ctx, rnd) or {})
    place = place or th['arch_place']
    for u in (R.get('team_util') or []) + (R.get('user_util') or []):
        if u.get('kind') == 'smoke' and u.get('land_place') == place and u.get('det_t') is not None \
                and u['det_t'] <= t <= (u.get('end_t') if u.get('end_t') is not None else 1e9):
            return True
    return False


def _vip_wait(m, rnd, vic, d_tk, att):
    """审核单 31 / Q16：T 方 vic 开局（<= vip_wait_death_s）死在中线（mid_open），凶手在 VIP 一侧（vip_lines），
    而他第一次到中远（mid_arrive）早于我方第一颗 VIP 烟（vip_smoke_places，起爆 <= vip_open_s）。
    满足返回 dict(arrive_t, vip_t, vip_by, death_t, place, killer_place, thru)，否则 None"""
    ctx, th = m.ctx, m.th
    if C.side_of(ctx, vic, rnd) != 'T' or not att or not C.is_enemy(ctx, vic, att, rnd):
        return None
    d_t = C.t_of(ctx, d_tk, rnd)
    if d_t > th['vip_wait_death_s']:
        return None
    vp = _death_place(ctx, vic, d_tk)
    kp = C.place_at(ctx, att, int(d_tk))
    if vp not in th['mid_open'] or kp not in th['vip_lines']:
        return None
    R = (C.tl_round(ctx, rnd) or {})
    vs = sorted(((float(u['det_t']), str(u.get('who') or m.uname)) for u in (R.get('team_util') or []) + (R.get('user_util') or [])
                 if u.get('kind') == 'smoke' and u.get('land_place') in th['vip_smoke_places'] and u.get('det_t') is not None
                 and float(u['det_t']) <= th['vip_open_s']))
    if not vs:
        return None
    arr = next((f for f in m.frames(vic, rnd) if f['alive'] and f['place'] in th['mid_arrive'] and f['tick'] <= d_tk), None)
    if arr is None or arr['t'] >= vs[0][0]:
        return None
    dd = C.deaths(ctx, rnd)
    dd = dd[(dd['vic'] == vic) & (dd['tick'] == int(d_tk))]
    thru = bool(dd['thrusmoke'].iloc[0]) if len(dd) and 'thrusmoke' in dd.columns else False
    return dict(arrive_t=arr['t'], vip_t=vs[0][0], vip_by=vs[0][1], death_t=d_t, place=vp, killer_place=kp, thru=thru)


def _rule_vip_wait(m, hits):
    """Q16：你在 VIP 烟起来前就上了中远、开局死在中线（凶手在 VIP 一侧）：这次死算合理代价，只给一条思路
    “还可以等 VIP 烟起来再上”（kind=思路，进 ideas）。同一次阵亡已经判了 TIME-08（已知的狙）就不再给"""
    ctx, th, U = m.ctx, m.th, m.U
    done = {(h['round'], h['death_t']) for h in hits if h['rule'] == 'TIME-08' and h['death_t'] is not None}
    for ri in C.rounds(ctx):
        rnd = ri['n']
        if C.user_side(ctx, rnd) != 'T':
            continue
        dth = _user_death(ctx, rnd)
        if dth is None:
            continue
        d_tk, killer, _ = dth
        w = _vip_wait(m, rnd, U, d_tk, killer)
        if w is None or (rnd, C.sec(w['death_t'])) in done:
            continue
        kn = C.name_of(ctx, killer)
        kpc, pc = C.place_cn(w['killer_place']), C.place_cn(w['place'])
        text = (f"VIP 烟还没起来你就上了中远，{'之后隔着烟' if w['thru'] else ''}被{kpc}的 {kn} 打死在{pc}")
        alt = "还可以等 VIP 烟起来再上"
        facts = dict(variant='vip_wait', arrive_t=C.sec(w['arrive_t']), vip_smoke_t=C.sec(w['vip_t']), vip_smoke_by=w['vip_by'],
                     death_t=C.sec(w['death_t']), killer=kn, killer_place=kpc, place=pc, thrusmoke=w['thru'],
                     death_verdict='合理代价', buy=C.buy(ctx, rnd))
        ev = (f"第 {C.fmt_sec(w['arrive_t'])} 秒你到中远，第 {C.fmt_sec(w['vip_t'])} 秒 VIP 烟才起，"
              f"第 {C.fmt_sec(w['death_t'])} 秒被{kpc}的 {kn} 打死")
        hits.append(_hit('TIME-08', '思路', rnd, w['death_t'], d_tk, 'moment', m.uname, True, '合理代价', facts, text, alt,
                         [], ev, death_t=w['death_t'], death_of=m.uname))


def _rule_mid_awp(m, hits):
    ctx, th, U = m.ctx, m.th, m.U
    halves = _halves(ctx)
    first_deaths = {}                                     # 半场 → [(回合, t, 有没有拱门烟, 死者)]
    for ri in C.rounds(ctx):
        rnd = ri['n']
        if C.user_side(ctx, rnd) != 'T' or C.buy(ctx, rnd) not in th['long_buys']:
            continue
        k = C.deaths(ctx, rnd)
        k = k[k['tick'] >= ri['start']]
        ms = set(C.mates(ctx, rnd, include_self=True))
        if len(k):
            fk = k.iloc[0]
            t_fk = C.t_of(ctx, int(fk['tick']), rnd)
            if fk['vic'] in ms and C.wkey(fk['wk']) == 'awp' and _death_place(ctx, fk['vic'], fk['tick']) in th['mid_open'] \
                    and t_fk <= th['fk_t_s']:
                apl = C.place_at(ctx, fk['att'], int(fk['tick']))
                thru = bool(fk['thrusmoke']) if 'thrusmoke' in k.columns else False
                first_deaths.setdefault(halves[rnd], []).append(
                    (rnd, t_fk, _arch_smoke(ctx, th, rnd, t_fk), C.name_of(ctx, fk['vic']), C.name_of(ctx, fk['att']),
                     apl, thru, _arch_smoke(ctx, th, rnd, t_fk, apl) if apl in th['awp_smoke_lines'] else False,
                     _vip_wait(m, rnd, fk['vic'], int(fk['tick']), fk['att']) is not None))
        # ---------------- TIME-08 个人：你自己开局被已知的狙秒
        dth = _user_death(ctx, rnd)
        if dth is None:
            continue
        d_tk, killer, wk = dth
        d_t = C.t_of(ctx, d_tk, rnd)
        my_p = _death_place(ctx, U, d_tk)
        if d_t > th['mid_awp_t_s'] or C.wkey(wk) != 'awp' or my_p not in th['mid_open']:
            continue
        hist = []
        for r2 in C.rounds(ctx):
            if r2['n'] < rnd and halves[r2['n']] == halves[rnd]:
                hist += [(r2['n'], x[0]) for x in _awp_mid_kills(ctx, th, r2['n']) if x[2] == killer]
        if not hist:
            continue
        f = C.fires(ctx, rnd, sid=U)
        if len(f[(f['tick'] >= d_tk - _secs(ctx, th['noshot_s'])) & (f['tick'] <= d_tk)]):
            continue
        in_b = [x for x in C.mates(ctx, rnd) if C.alive(ctx, x, d_tk, rnd) and C.place_at(ctx, x, d_tk) in th['b_exec']]
        if len(in_b) < th['mates_b']:
            continue
        ff = C.fires(ctx, rnd)
        ff = ff[(ff['tick'] >= ri['start']) & (ff['tick'] < d_tk)]
        b_fight = [r for r in ff.itertuples() if C.place_at(ctx, r.sid, int(r.tick)) in th['b_fight_places']]
        R = (C.tl_round(ctx, rnd) or {})
        b_fight += [u for u in R.get('team_util') or [] if u.get('land_place') in th['b_util_places']
                    and u.get('kind') in th['b_util_kinds'] and u.get('det_t') is not None and u['det_t'] < d_t]
        if b_fight:
            continue
        sp = C.move_speed(ctx, U, d_tk - _secs(ctx, th['run_before_s']))
        running = sp is not None and sp > th['run_word_speed']
        hs = C.heard_shots(ctx, U, ri['start'], d_tk - _secs(ctx, th['mid_awp_gap_s']), shooters=C.enemies(ctx, rnd))
        mid_awp = [r for r in hs.itertuples() if C.wkey(r.wk) == 'awp' and C.place_at(ctx, r.sid, int(r.tick)) in th['mid_awp_places']]
        awper = C.name_of(ctx, killer)
        pc = C.place_cn(my_p)
        n = len(hist)
        text = (f"对面 {awper} 的 AWP 之前已经 {n} 次在中路一带打死我们的人{'，这回合中路也响过狙' if mid_awp else ''}，"
                f"B 区还没开打你就{'跑着' if running else ''}上了{pc}，一枪没开被秒")
        alt = "还可以先卡在 B 小巷口防反推，听到 B 区开打再静步出去，活着牵制中路和 B 小"
        facts = dict(awper=awper, n=n, history=[dict(round=a, t=C.sec(b)) for a, b in hist], mid_awp_heard=bool(mid_awp),
                     mid_awp_t=C.sec(C.t_of(ctx, int(mid_awp[0].tick), rnd)) if mid_awp else None,
                     running=running, place=pc, death_t=C.sec(d_t), mates_in_b=[C.name_of(ctx, x) for x in in_b])
        ev = (f"{awper} 的 AWP 之前在第 {'、'.join(str(a) for a, _ in hist)} 回合中路一带杀过我们的人；"
              f"这回合第 {C.fmt_sec(d_t)} 秒你在{pc}被他打死，{C.num_cn(len(in_b))}个队友在 B 区，B 区还没有枪声和道具")
        hits.append(_hit('TIME-08', '问题', rnd, d_t, d_tk, 'moment', m.uname, True, '你的责任', facts, text, alt,
                         [], ev, death_t=d_t, death_of=m.uname, merge_key=f"death:{rnd}:{d_tk}"))
    # ---------------- TIME-09 团队：同一半场 ≥2 回合开局在中远被 AWP 首杀
    for h in sorted(first_deaths):
        fd = sorted(first_deaths[h])
        if len(fd) < th['min_rounds']:
            continue
        rs = [x[0] for x in fd]
        no_arch = [x[0] for x in fd if not x[2]]
        # 每一回合的狙从哪条线打过来：烟只建议给那条线（拱门 / VIP）；从下水道、中路打的，或隔着我们的烟打的不给烟
        # （e6b：第 7 回合从下水道、第 11 回合隔着 VIP 烟，原来却写“开局先补拱门烟”）
        groups, order = {}, []
        for x in fd:
            pl, thru, line_smoke, wait = x[5], x[6], x[7], x[8]
            if not pl:
                continue
            if wait:
                desc = f'从{C.place_cn(pl)}打的，VIP 烟起来前人就上了中远'
            elif pl in th['awp_smoke_lines'] and thru:
                desc = f'隔着烟从{C.place_cn(pl)}打的'
            elif pl in th['awp_smoke_lines'] and not line_smoke:
                desc = f'从{C.place_cn(pl)}打的，当时没有{C.place_cn(pl)}烟'
            else:
                desc = f'从{C.place_cn(pl)}打的'
            if desc not in groups:
                groups[desc] = []
                order.append(desc)
            groups[desc].append(x[0])
        where = '；'.join(f"第 {'、'.join(map(str, groups[d]))} 回合是{d}" for d in order)
        text = (f"第 {'、'.join(map(str, rs))} 回合的前 30 秒，都是我们在中远一带被对面 AWP 拿了首杀，一开局就少一人"
                + (f'；{where}' if where else ''))
        smoke_lines = []
        for x in fd:
            if x[5] in th['awp_smoke_lines'] and not x[6] and not x[7] and not x[8] and C.place_cn(x[5]) not in smoke_lines:
                smoke_lines.append(C.place_cn(x[5]))
        wait_rounds = [x[0] for x in fd if x[8]]
        # 还可以只给一个思路：中远的人比 VIP 烟先到 → 等 VIP 烟；有该补的烟（只剩 VIP，审核单 31 / Q17 不再建议拱门烟、过点烟）
        # → 补烟；都没有才说中远的站法
        if wait_rounds:
            alt = "还可以中远的人等 VIP 烟起来再上"
        elif smoke_lines:
            alt = f"还可以开局先补{'、'.join(smoke_lines)}烟"
        else:
            alt = "还可以中远只留一人时先卡 B 小巷口，等主攻那边打响再出"
        facts = dict(half=h, rounds=rs, no_arch=no_arch, smoke_lines=smoke_lines, vip_wait_rounds=wait_rounds,
                     detail=[dict(round=x[0], t=C.sec(x[1]), arch_smoke=x[2], victim=x[3], awper=x[4],
                                  awper_place=C.place_cn(x[5]), thrusmoke=x[6], line_smoke=x[7], vip_wait=x[8]) for x in fd])
        ev = '；'.join(f"第 {x[0]} 回合第 {C.fmt_sec(x[1])} 秒 {x[3]} 被 {x[4]} 的 AWP 打死" for x in fd)
        hits.append(_hit('TIME-09', '问题', 0, None, None, 'match', '全队', False, '队友或团队', facts, text, alt,
                         [], ev))


# ================================================================ 入口
def _dedupe(hits):
    """同一次阵亡同时命中 TIME-06 或 TIME-08 时，TIME-05 不再单独出（rules_v1 TIME-05 trigger）"""
    spec = {(h['round'], h['death_t']) for h in hits if h['rule'] in ('TIME-06', 'TIME-08') and h['death_t'] is not None}
    return [h for h in hits if not (h['rule'] == 'TIME-05' and (h['round'], h['death_t']) in spec)]


def run(ctx):
    """跑全部 TIME-* 规则，返回 Hit 列表（按回合、时刻、规则排好）"""
    m = _Memo(ctx)
    hits = []
    for f in (_rule_rotation, _rule_shift, _rule_a1, _rule_lane, _rule_late, _rule_run_into_heard, _rule_blind_push,
              _rule_smoke_cross, _rule_mid_awp, _rule_vip_wait):
        f(m, hits)
    hits = _dedupe(hits)
    order = {r: i for i, r in enumerate(RULE_IDS)}
    hits.sort(key=lambda h: (h['round'], -1 if h['t'] is None else h['t'], order[h['rule']], h['tick'] or 0))
    return hits
