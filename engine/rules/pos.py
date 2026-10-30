# -*- coding: utf-8 -*-
"""站位与拉枪规则：POS-01..08、POS-06W（弱守评价）、POS-G1、POS-G2。

依据：rules/rules_v1.json（trigger / blame / template / alt）+ rules/decisions_v1.md（#10 POS-01、#23 POS-06③、
审核单（手册 v1）第 8、9、14、15、30、36 条和 Q5/Q6/Q7/Q10/Q13/Q18）。
对外开放（给 TEAM-06 / TEAM-04 / TIME-G1 / POST-02 用）：move_intent(ctx, sid, rnd, t0, t1)、retake_pair(ctx, rnd, sid, t_to)。
原型：pro/analysis/match3/rules_proto/站位与视线.py（POS-01..08、G1）、好的操作.py（GOOD-07 → POS-G2）、
团队配合.py（TEAM-08 叠人 → POS-06①）。
所有 C-* 口径用 engine.common；阈值只从 ctx.th['pos'] / ctx.th['common'] 取。
入口：run(ctx) -> list[Hit]（Hit 结构见 engine/CONTRACT.md）。
"""
import math

import numpy as np
import pandas as pd

from engine import common as C
from engine.rules import util as UT

RULE_KIND = {'POS-01': '问题', 'POS-02': '问题', 'POS-03': '问题', 'POS-04': '问题', 'POS-05': '问题',
             'POS-06': '判定', 'POS-07': '判定', 'POS-08': '判定', 'POS-G1': '好', 'POS-G2': '好'}


# ================================================================ 小工具
def _th(ctx):
    return ctx.th['pos']


def _tr(ctx):
    return int(ctx.th['common'].get('tickrate', 64))


def _ticks(ctx, s):
    """秒 → tick 数"""
    return int(round(float(s) * _tr(ctx)))


def _t(ctx, tick, rnd):
    """facts 用的秒数（C.sec 取整）"""
    return C.sec(C.t_of(ctx, tick, rnd))


def _who(ctx, sid):
    """句子里的人：用户写"你"，其他人写名字"""
    return '你' if sid == C.user_sid(ctx) else C.name_of(ctx, sid)


def _plant(ctx, rnd):
    """这回合的下包 (tick, 包点 'A'/'B')，没下包 (None, None)"""
    for r in ctx.json('timeline.json')['rounds']:
        if int(r['round']) == int(rnd):
            p = r.get('plant') or {}
            return (int(p['tick']), p.get('site')) if p.get('tick') is not None else (None, None)
    return None, None


def _planted(ctx, rnd, tick):
    pt, _ = _plant(ctx, rnd)
    return pt is not None and pt <= int(tick)


def _chest(ctx, sid, tick):
    return C.eye(ctx, sid, tick) - np.array([0.0, 0.0, _th(ctx)['chest_drop']])


def _aim_at(ctx, a, b, tick, pitch=None, yaw=None, src=None):
    """a 的准星离 b 胸口的 3D 夹角（度）；pitch/yaw/src 不给时取帧上的"""
    if pitch is None:
        pitch, yaw = C.view(ctx, a, tick)
    src = C.eye(ctx, a, tick) if src is None else src
    err = C.aim_err(src, pitch, yaw, _chest(ctx, b, tick))[0]
    return float(err) if np.isfinite(err) else 999.0


def _spot1(ctx, a, b, tick):
    """这一帧 a 看见了 b（b 的 spotted_by 里有 a，两人都活着）"""
    return C.spots(ctx, a, b, int(tick), int(tick)) is not None


def _sees(ctx, a, b, tick):
    """a 看得见 b：spotted；漏判时两人准星互相对着兜底（a 对 b ≤ mutual_aim_deg，b 对 a ≤ mutual_aim_deg_other）"""
    if _spot1(ctx, a, b, tick):
        return True
    th = _th(ctx)
    rnd = C.round_of(ctx, tick)
    if not (C.alive(ctx, a, tick, rnd) and C.alive(ctx, b, tick, rnd)):
        return False
    return _aim_at(ctx, a, b, tick) <= th['mutual_aim_deg'] and _aim_at(ctx, b, a, tick) <= th['mutual_aim_deg_other']


def _frames(ctx, t0, t1):
    """[t0, t1] 每 frame_step 个 tick 取一帧"""
    step = int(_th(ctx)['frame_step'])
    t0, t1 = int(t0), int(t1)
    return list(range(t0, t1 + 1, step)) if t1 >= t0 else [t0]


def _seen_frac(ctx, a, b, t0, t1):
    """[t0, t1] 里 a 看得见 b（_sees）的帧比例"""
    fr = _frames(ctx, t0, t1)
    return sum(1 for t in fr if _sees(ctx, a, b, t)) / max(1, len(fr))


def _unseen_gap(ctx, a, b, t0, t1):
    """[t0, t1] 里 a 连续看不见 b（spotted）的最长时长（秒）"""
    best = cur = 0
    for t in _frames(ctx, t0, t1):
        cur = 0 if _spot1(ctx, a, b, t) else cur + 1
        best = max(best, cur)
    return best * int(_th(ctx)['frame_step']) / _tr(ctx)


def _last_spot(ctx, a, b, t0, t1):
    """[t0, t1] 里 a 最后一次看见 b 的 tick（spotted），没有 None"""
    for t in reversed(_frames(ctx, t0, t1)):
        if _spot1(ctx, a, b, t):
            return t
    return None


def _util_ev(ctx, name):
    """道具事件表（flashbang_detonate / inferno_startburn）：先读 cache/ev_<name>.csv，再退到 sight_s4 缓存的 ev。
    common.py 只给了烟（C.smokes），闪和火没有公共接口，这里自己读（见 needs_from_others）"""
    df = None
    try:
        df = ctx.cache('ev_' + name, dtype={'user_steamid': str})
    except (FileNotFoundError, KeyError, OSError):
        for src in ('sight_s4', 'sight_s8', 'sight', 'daoju'):
            try:
                raw = ctx.cache(src)
            except (FileNotFoundError, KeyError, OSError):
                continue
            v = raw.get('ev', {}).get(name) if isinstance(raw, dict) else None
            if isinstance(v, pd.DataFrame):
                df = v
                break
    if df is None or not len(df):
        return []
    out = []
    for r in df.itertuples():
        sid = r.user_steamid
        sid = str(int(sid)) if isinstance(sid, float) and sid == sid else str(sid)
        out.append(dict(t=int(r.tick), x=float(r.x), y=float(r.y), by=sid))
    return sorted(out, key=lambda u: (u['t'], u['by']))


def _team_util_near(ctx, team_sid, rnd, xy, t0, t1, r, kinds=('flash', 'smoke', 'fire')):
    """[t0, t1] 里 team_sid 一队往 xy 平面 r 内扔过的闪 / 烟（生效中）/ 火"""
    key = '_pos_util'
    cache = getattr(ctx, key, None)
    if cache is None:
        cache = dict(flash=_util_ev(ctx, 'flashbang_detonate'), fire=_util_ev(ctx, 'inferno_startburn'))
        try:
            setattr(ctx, key, cache)
        except AttributeError:
            pass
    team = set(C.mates(ctx, rnd, team_sid, include_self=True))
    pts = []
    for k in ('flash', 'fire'):
        if k in kinds:
            pts += [u for u in cache[k] if t0 <= u['t'] <= t1 and u['by'] in team]
    if 'smoke' in kinds:
        pts += [dict(t=s['t0'], x=s['x'], y=s['y'], by=s['by']) for s in C.smokes(ctx, rnd)
                if s['by'] in team and s['t0'] <= t1 and s['t1'] >= t0]
    return [u for u in pts if math.hypot(u['x'] - xy[0], u['y'] - xy[1]) <= r]


def _kb(ctx, ids, side, place):
    """M6：知识库条目按阵营和地区配，对不上就不挂"""
    area = _th(ctx)['kb_area']
    out = []
    for i in ids:
        it = C.kb_item(i)
        if not it:
            continue
        if it.get('side') not in (side, '通用'):
            continue
        a = it.get('area')
        if a != '全图' and place not in area.get(a, []):
            continue
        out.append(i)
    return out


def _tidy(ctx, s):
    """句子排版：common.tidy（人名整体保护）"""
    return C.tidy(s, C.player_names(ctx))


def _hit(ctx, rule, rnd, tick, subject, blame, text, alt, facts, kb=(), evidence='', death_tick=None,
         death_sid=None, merge_key=None, kind=None, scope='moment'):
    """拼一个 Hit（CONTRACT）"""
    U = C.user_sid(ctx)
    return dict(rule=rule, kind=kind or RULE_KIND[rule], round=int(rnd), t=_t(ctx, tick, rnd), tick=int(tick),
                scope=scope, subject=C.name_of(ctx, subject), is_user=subject == U, blame=blame,
                death_t=None if death_tick is None else _t(ctx, death_tick, rnd),
                death_of=None if death_sid is None else C.name_of(ctx, death_sid),
                merge_key=merge_key, facts=facts, text=_tidy(ctx, text), alt=_tidy(ctx, alt), kb=list(kb),
                evidence=_tidy(ctx, evidence))


def _killer_of(ctx, sid, rnd):
    """sid 这回合的 (阵亡 tick, 凶手 sid, 死亡行)"""
    D = C.deaths(ctx, rnd)
    w = D[D['vic'] == sid]
    if not len(w):
        return None, None, None
    r = w.iloc[0]
    return int(r['tick']), r['att'], r


# ================================================================ POS-01 打完/开完枪不收
def pos01(ctx):
    th, tr = _th(ctx), _tr(ctx)
    U = C.user_sid(ctx)
    D, H = C.deaths(ctx), C.hurts(ctx)
    evs = []
    for k in D[(D['att'] == U) & (D['vic'] != U)].itertuples():
        if k.round > 0 and C.is_enemy(ctx, U, k.vic, k.round):
            evs.append((int(k.tick), 'kill', k.vic, int(k.round), k.wk))
    hu = H[H['att'] == U]
    for f in C.fires(ctx, sid=U).itertuples():
        if f.round <= 0 or not C.is_sniper(f.wk):
            continue
        w = hu[(hu['tick'] >= f.tick) & (hu['tick'] <= f.tick + int(th['pos01_miss_ticks']))]
        w = w[[C.is_enemy(ctx, U, v, f.round) for v in w['vic']]] if len(w) else w
        if not len(w):
            evs.append((int(f.tick), 'miss', None, int(f.round), f.wk))
    hits, used = [], set()
    for tE, kind, vic, rnd, wk in sorted(evs, key=lambda e: (e[0], e[1])):
        if not C.alive(ctx, U, tE, rnd):
            continue
        foes = [e for e in C.alive_list(ctx, rnd, tE + 2, enemy=True) if e != vic]
        if not foes:
            continue
        us_a, them_a = C.alive_counts(ctx, tE + 1, lag_s=0)
        clutch = kind == 'kill' and us_a <= th['pos01_clutch_us']
        hi = th['pos01_clutch_s'] if clutch else th['pos01_hit_max_s']
        hv = H[(H['vic'] == U) & (H['tick'] > tE + _ticks(ctx, th['pos01_hit_min_s'])) & (H['tick'] <= tE + _ticks(ctx, hi))
               & (H['att'].isin(foes)) & (H['dmg'] > 0)]
        if not len(hv):
            continue
        A, td = hv.iloc[0]['att'], int(hv.iloc[0]['tick'])
        if (A, td) in used:
            continue
        dtk, K, drow = _killer_of(ctx, U, rnd)
        died = dtk is not None and K == A and dtk >= td
        move = C.dist3(C.pos(ctx, U, tE), C.pos(ctx, U, td))
        frac = _seen_frac(ctx, A, U, tE + 6, td)
        in_std = td <= tE + _ticks(ctx, th['pos01_hit_max_s'])
        ok = in_std and (frac >= th['pos01_seen_frac'] or move < th['pos01_stay_move'])
        if not ok and clutch and died and dtk <= tE + _ticks(ctx, th['pos01_clutch_s']):
            ok = C.dist3(C.pos(ctx, U, tE), C.pos(ctx, U, dtk)) < th['pos01_clutch_move']
        if not ok:
            continue
        # 杀完以后是你先朝 A 开枪、先打中了他，或者这场对枪最后是你把 A 打死：这是你自己挑的、赢下来的对枪，
        # 不是“没收、还留在他枪线上”（128 R8：19.9 秒先打中 高分玩家14，20.4 秒把他打死）
        if len(H[(H['att'] == U) & (H['vic'] == A) & (H['tick'] > tE) & (H['tick'] <= td) & (H['dmg'] > 0)]):
            continue
        if len(D[(D['att'] == U) & (D['vic'] == A) & (D['tick'] > td) & (D['tick'] <= td + _ticks(ctx, th['pos01_follow_s']))]):
            continue
        # 隔烟被打死交给 POS-08
        if died and dtk <= td + _ticks(ctx, th['pos01_smoke_after_s']) and bool(drow['thrusmoke']):
            continue
        used.add((A, td))
        # 间隔：杀完到挨枪；挨枪后还在他枪线上、follow_s 内被他打死的算到被打死（decisions #10：R14 停了 1.4 秒）
        t_end = td
        more = H[(H['vic'] == U) & (H['att'] == A) & (H['tick'] >= td) & (H['tick'] <= td + _ticks(ctx, th['pos01_follow_s']))]
        if len(more):
            t_end = int(more['tick'].max())
        if died and dtk <= td + _ticks(ctx, th['pos01_follow_s']):
            t_end = max(t_end, dtk)
        gap = (t_end - tE) / tr
        gap1 = (td - tE) / tr
        # 页面上写的“杀完 N 秒”用两个显示出来的秒数相减（8d R13：47.2 → 47.6 写成 0.3 秒）
        gap_d = round(_t(ctx, t_end, rnd) - _t(ctx, tE, rnd), 1)
        gap1_d = round(_t(ctx, td, rnd) - _t(ctx, tE, rnd), 1)
        branch = 'stay' if gap >= th['pos01_retreat_s'] else 'spot'
        # 审核单第 36 条：杀完往回收、侧移、前压都算挪开了（5 秒内阵亡都在 12–18%，原地 34–36%）；
        # “你还站在原地”只给真的没挪的：杀完到挨枪 / 被打死平面挪了 > pos01_still_r 的不判
        still_mv = C.dist2(C.pos(ctx, U, tE), C.pos(ctx, U, t_end))
        if branch == 'stay' and still_mv > th['pos01_still_r']:
            continue
        # Q7：CT 守点（没下包、杀之前已经在这个位置停着架了 pos01_ct_hold_s 秒）杀完原地继续架，不按 T 方的“杀完没收”判
        ct_hold = False
        if branch == 'stay' and kind == 'kill' and C.side_of(ctx, U, rnd) == 'CT' and not _planted(ctx, rnd, tE):
            h0 = tE - _ticks(ctx, th['pos01_ct_hold_s'])
            ct_hold = C.path_len(ctx, U, h0, tE) <= th['pos01_ct_hold_path']
            if ct_hold:
                continue
        others, knife = [], False
        if kind == 'miss' and branch == 'spot':
            # decisions #10：不到 retreat_s 秒没机会退，不判“没收”，改看开这一枪时站的位置是不是两个人都打得到
            # （8d R8：VIP 狙空 对手1 后原地切刀，对手1、对手3 都在朝这个位置开枪，不到半秒挨了两发）。
            # 只有一个人打得到你、你还挪开了的，是对枪输了，交给 SHOT-04/05
            if move > th['pos01_first_spot_r']:
                continue
            w0, w1 = tE - _ticks(ctx, th['pos01_miss_other_pre_s']), t_end
            for B in sorted(foes):
                if B == A:
                    continue
                hit_me = len(H[(H['att'] == B) & (H['vic'] == U) & (H['tick'] >= w0) & (H['tick'] <= w1)])
                fb = C.fires(ctx, rnd, sid=B)
                fb = fb[(fb['tick'] >= w0) & (fb['tick'] <= w1)]
                aim = any(_aim_at(ctx, B, U, int(x.tick), pitch=x.user_pitch, yaw=x.user_yaw) <= th['pos01_line_deg']
                          for x in fb.itertuples())
                if hit_me or aim:
                    others.append(B)
            if not others:
                continue
            knife = C.wkey(C.weapon_at(ctx, U, td)) == 'knife'
        # 已知程度（P1：只用你自己看见过 / 互相打中过）
        k0 = tE - _ticks(ctx, th['pos01_known_s'])
        last_seen = _last_spot(ctx, U, A, k0, tE)
        a_hit_me = bool(len(H[(H['att'] == A) & (H['vic'] == U) & (H['tick'] >= k0) & (H['tick'] < tE)]))
        i_hit_a = bool(len(H[(H['att'] == U) & (H['vic'] == A) & (H['tick'] >= k0) & (H['tick'] < tE)]))
        # 狙空的那一枪就是对着 A 开的（C-TARGET + C-SEEN），也算你刚看到过他
        aimed = kind == 'miss' and any(x['tick'] == tE and x['target'] == A and x['seen'] in tuple('abcdef')
                                       for x in C.shots(ctx) if x['round'] == rnd)
        if aimed and last_seen is None:
            last_seen = tE
        known = last_seen is not None or a_hit_me or i_hit_a
        first_tick = None
        if branch == 'spot' and kind == 'kill':
            # decisions #10：不到 0.8 秒没机会退，改看打第一枪时站的位置是不是两个人都打得到
            bs = [b for b in C.bursts(ctx) if b['round'] == rnd and b['target'] == vic and b['tick'] <= tE]
            first_tick = int(bs[-1]['tick']) if bs else tE
            same_spot = C.dist3(C.pos(ctx, U, first_tick), C.pos(ctx, U, tE)) <= th['pos01_first_spot_r']
            # 两个人都打得到：这段时间他看得见你（spotted / 互瞄），或者他的准星一直压在你站的这个位置上
            fr = _frames(ctx, first_tick, tE)
            exposed = any(_sees(ctx, A, U, t) for t in fr)
            on_line = sum(1 for t in fr if _aim_at(ctx, A, U, t) <= th['pos01_line_deg']) / max(1, len(fr))
            exposed = exposed or on_line >= th['pos01_line_frac']
            if not (same_spot and exposed):
                continue
        a_name, v_name = C.name_of(ctx, A), (C.name_of(ctx, vic) if vic else None)
        a_place = C.place_cn_at(ctx, A, td)
        hidden_ph, rev_tk = C.hidden_phrase(ctx, A, tE)
        sit = C.alive_str(us_a, them_a) if kind == 'kill' else None
        facts = dict(t=_t(ctx, tE, rnd), event=kind, victim=v_name, attacker=a_name, attacker_place=a_place,
                     t_hit=_t(ctx, td, rnd), t_end=_t(ctx, t_end, rnd), gap=round(gap, 2), gap_first=round(gap1, 2),
                     branch=branch, died=died, alive=sit, enemies_left=len(foes), seen_frac=round(frac, 2),
                     move=round(move), known=known, last_seen_t=None if last_seen is None else _t(ctx, last_seen, rnd),
                     attacker_hit_you=a_hit_me, you_hit_attacker=i_hit_a, aimed_at_attacker=aimed, weapon=C.weapon_cn(wk),
                     t_first_shot=None if first_tick is None else _t(ctx, first_tick, rnd), place=C.place_cn_at(ctx, U, tE),
                     last_revealed_t=None if rev_tk is None else _t(ctx, rev_tk, rnd),
                     others=[C.name_of(ctx, b) for b in others], knife=knife, still_move=round(still_mv))
        ts = C.fmt_sec(C.t_of(ctx, tE, rnd))
        side = C.side_of(ctx, U, rnd)
        # 你身上带着包：守住 / 退回来更要紧（8d R14：C4 在你身上）
        bomb = UT.has_c4(ctx, U, tE)
        facts['bomb'] = bomb
        if branch == 'spot' and kind == 'miss':
            # 狙空、不到 retreat_s 秒就挨枪，开枪的位置另一个人也在打（decisions #10 站位句式的狙空版）
            tgt = next((x['target'] for x in C.shots(ctx) if x['round'] == rnd and x['tick'] == tE and x.get('target')), None)
            tg_n = C.name_of(ctx, tgt) if tgt else None
            facts['target'] = tg_n
            on = f"用狙打{C.place_cn_at(ctx, tgt, tE)}的 {tg_n}没中" if tg_n else '用狙开了一枪没中'
            two = '、'.join(dict.fromkeys([a_name] + facts['others']))
            stay = '切了刀还站在原地' if knife else '还站在原地'
            hurt = f'马上就被 {a_name} 打中' + ('并打死' if died else '')
            text = f"第 {ts} 秒在{facts['place']}{on}；开枪的位置 {two} 都打得到，狙空后你{stay}，{hurt}"
            facts['clause'] = f'开枪的位置 {two} 都打得到，狙空后你{stay}，{hurt}'
            facts['short'] = f"第 {ts} 秒狙空后{stay}，{hurt}"
            one = tg_n or a_name
            alt = f'还可以开这一枪前先找一个只露给 {one} 一个人的位置，狙空了马上缩回掩体再拉栓'
            kb = _kb(ctx, [], side, C.place_at(ctx, U, tE))
            blame = '你的责任' if known else '小问题'
            ob = '、'.join(facts['others'])
            ev = f'你开枪后马上就被 {a_name} 打中，这段时间 {ob} 也在朝你这个位置开枪'
            hits.append(_hit(ctx, 'POS-01', rnd, tE, U, blame, text, alt, facts, kb, ev,
                             death_tick=dtk if died else None, death_sid=U if died else None,
                             merge_key=f'snipe:{rnd}:{tE}'))
            continue
        if branch == 'stay':
            evt = f'打死 {v_name}，打成 {sit}' if kind == 'kill' else '用狙开了一枪没中'
            left = f'场上还有 {len(foes)} 个敌人，' if kind == 'miss' else ''
            # 完整性复核：主语先写他的名字，不写“他……，X 在……”（“他”读着像是指刚被你打死的那个人，bd R19、10ce）
            if last_seen is not None and (tE - last_seen) <= _ticks(ctx, th['pos01_just_seen_s']):
                kn = f'你刚看到过 {a_name}，他'
            elif last_seen is not None:
                kn = f'几秒前你看到过 {a_name}，他'
            elif a_hit_me:
                kn = f'{a_name} 刚打中过你，之后又'
            elif i_hit_a:
                kn = f'你刚打中过 {a_name}，他'
            else:
                kn = None                          # P1：全队雷达 + 击杀栏，整回合看
            c4 = 'C4 在你身上，' if bomb else ''
            # 审核单第 36 条：“你没有往回收”→“你还站在原地”（侧移、前压也算挪开，只有原地不动才判，见上面 pos01_still_r）
            # 完整性复核：stay 分支本来就是“还有时间退”（>= retreat_s），不写“很快打中了你”（和证据“过了一会儿才”打架）；
            # 他没露过面时主语直接写他的名字，不写“他……，X 在……”（bd R19 的“他”读着像是指刚被打死的 对手5（第4场））
            who = f'{kn}在{a_place}' if kn is not None else f'{a_name} {hidden_ph}，后来在{a_place}'
            text = (f'第 {ts} 秒{evt}后你还站在原地；{c4}{left}{who}'
                    f'打中了你' + ('，随后被他打死' if died else ''))
            alt = '还可以开完枪或拿到击杀先退回刚才的掩体，再决定下一步'
            if C.is_sniper(wk):
                alt += f'；{C.weapon_cn(wk)}打空先缩回去再拉栓' if kind == 'miss' else f'；{C.weapon_cn(wk)}拿到人也先缩回去再拉栓'
            kb = _kb(ctx, [], side, C.place_at(ctx, U, tE))
            blame = '你的责任'
            if kind == 'miss':
                # 和同一枪的 SHOT-04（狙空枪）并成一句时只写没收这半句，不再把“开了一枪没中”说第二遍（bd R22）
                facts['clause'] = (f'狙空后你还站在原地，{a_name} 在{a_place}打中了你'
                                   + ('，随后被他打死' if died else ''))
            # decisions #10 按“杀完到被打死”（挨了第一枪还在他枪线上时算到最后一枪 / 被打死）分 0.8 秒：
            # 证据先写这段时间，第一枪什么时候打中放在括号里（8d R14：1.4 秒才被打死，0.4 秒时先挨了一枪）
            did = '杀人' if kind == 'kill' else '开枪'
            if died and t_end == dtk and t_end != td:
                # 不写延迟秒数（用户写作规则）：0.8 秒的分界只在判法里用
                ev = f'你{did}后还有时间退回去，过了一会儿才被 {a_name} 打死（他先打中了你）'
            elif t_end != td:
                ev = f'你{did}后还有时间退回去，之后一直挨 {a_name} 的枪'
            else:
                ev = f'{a_name} 在你{did}后过了一会儿才打中你'
            ev += '；这段时间他一直看得见你' if frac >= th['pos01_seen_frac'] else '；这段时间你几乎没挪位置'
        else:
            if known:
                kn = '几秒前你在这边看到过他' + ('，还打中过他' if i_hit_a else '')
            else:
                kn = '他' + hidden_ph
            text = (f'第 {ts} 秒打死 {v_name}，打成 {sit}；但打他时站的位置 {a_name} 也打得到（{kn}），'
                    f'杀完马上就被他打中' + ('并打死' if died else ''))
            # 他之前没露过面：你不知道他在哪，“换到他打不到的位置”是拿结果说话（玩家视角），改成只露给一个口子的通用说法
            alt = (f'还可以打 {v_name} 之前先换到 {a_name} 那边打不到的位置，每次只对一个人' if known else
                   f'还可以打 {v_name} 时找一个只露给他那一个口子的位置，让还没清过的方向打不到你')
            # 并进同一次击杀的夸奖时只用问题这半句（“打死 X，打成 NvM”主句已经写过）
            facts['clause'] = (f'打他时站的位置 {a_name} 也打得到（{kn}），杀完马上就被他打中'
                               + ('并打死' if died else ''))
            kb = []
            blame = '你的责任' if known else '小问题'
            # 同样按“杀完到被打死”写（8d R13：0.4 秒就被打死，0.3 秒时先挨了一枪）
            ev = (f'从第一枪到杀人你几乎没换地方，{a_name} 那边正对着这个位置；'
                  + ('杀完马上就被他打死' if died and t_end == dtk else '杀完马上他就打中了你'))
        hits.append(_hit(ctx, 'POS-01', rnd, tE, U, blame, text, alt, facts, kb, ev,
                         death_tick=dtk if died else None, death_sid=U if died else None,
                         merge_key=f'snipe:{rnd}:{tE}' if kind == 'miss' else None))
    return hits


# ================================================================ POS-02 同一条线重复拉
def _repeek(ctx, K, V, tp, tk, kind):
    """V 再拉出来的那一刻（审核单第 15 条）：tp 之后 K 连续看不见 V >= pos02_break_s 的最后一段结束后，
    第一次又互相看见（spotted 任一方向）的那一帧；找不到用 tk。
    前一件事 = tp（队友倒下 / 第一次挨枪）；self 分支再往后推到断视线之前两人最后一次交手（他打中你 / 你朝他开枪），
    断得很短又出来的是同一场对枪里晃身再出。返回 (再拉 tick, 离前一件事的秒数, 前一件事 tick)"""
    th, tr = _th(ctx), _tr(ctx)
    step = int(th['frame_step'])
    need = max(1, int(math.ceil(th['pos02_break_s'] * tr / step)))
    run, gap_start, t_re = 0, None, None
    for t in _frames(ctx, tp, tk):
        if _spot1(ctx, K, V, t) or _spot1(ctx, V, K, t):
            if run >= need:
                gap_start, t_re = t - run * step, t
            run = 0
        else:
            run += 1
    seen_again = t_re is not None
    if t_re is None:
        t_re = tk
    prev = tp
    if kind == 'self':
        # 复核修正：没找到“又互相看见”的那一帧（隔墙 / 隔烟一直没 spotted）时，前一件事推到再拉（= 阵亡）之前两人最后一次交手
        # （8d R17：85.4 之后 86.5 又打中过，原来按 85.1 算成隔了 1.7 秒）
        hi = gap_start if gap_start is not None else t_re - 1
        H = C.hurts(ctx, C.round_of(ctx, tp))
        hs = [int(x) for x in H[(H['att'] == K) & (H['vic'] == V) & (H['tick'] >= tp) & (H['tick'] <= hi)]['tick']]
        fs = [s['tick'] for s in C.shots(ctx, V) if tp <= s['tick'] <= hi and s.get('target') == K]
        prev = max([tp] + hs + fs)
    return int(t_re), (int(t_re) - prev) / tr, int(prev), seen_again


def pos02(ctx, rnd):
    th, tr = _th(ctx), _tr(ctx)
    U = C.user_sid(ctx)
    team = C.mates(ctx, rnd, U, include_self=True)
    D, H = C.deaths(ctx, rnd), C.hurts(ctx, rnd)
    w = _ticks(ctx, th['pos02_window_s'])
    hits = []
    for dv in D.itertuples():
        V, K, tk = dv.vic, dv.att, int(dv.tick)
        if V not in team or K in team or not C.is_enemy(ctx, V, K, rnd):
            continue
        prior = []
        for pk in D[(D['att'] == K) & (D['tick'] < tk - _ticks(ctx, th['pos02_prior_gap_s'])) & (D['tick'] >= tk - w)].itertuples():
            if pk.vic in team and pk.vic != V:
                prior.append(('mate', int(pk.tick), pk.vic))
        for ph in H[(H['att'] == K) & (H['vic'] == V) & (H['tick'] < tk - _ticks(ctx, th['pos02_hurt_gap_s']))
                    & (H['tick'] >= tk - w) & (H['dmg'] >= th['pos02_hurt_min'])].itertuples():
            prior.append(('self', int(ph.tick), V))
        for kind, tp, T0 in prior:
            kp0, kp1 = C.pos(ctx, K, tp), C.pos(ctx, K, tk)
            if C.dist3(kp0, kp1) > th['pos02_killer_still']:
                continue
            # 死者挨的伤害大部分来自他（被两个人交叉打死的不是“同一条线被同一个人架死”）
            hv = H[(H['vic'] == V) & (H['tick'] >= tk - w) & (H['tick'] <= tk) & (H['dmg'] > 0)]
            if len(hv) and hv[hv['att'] == K]['dmg'].sum() < th['pos02_killer_dmg_share'] * hv['dmg'].sum():
                continue
            if kind == 'mate':
                p0, pv = C.pos(ctx, T0, tp - 1), C.pos(ctx, V, tk)
                b0 = math.degrees(math.atan2(p0[1] - kp1[1], p0[0] - kp1[0]))
                b1 = math.degrees(math.atan2(pv[1] - kp1[1], pv[0] - kp1[0]))
                same = abs((b0 - b1 + 180) % 360 - 180) <= th['pos02_same_line_deg'] or C.dist2(p0, pv) <= th['pos02_same_spot']
                if not same:
                    continue
                if not C.on_radar(ctx, K, tp - _ticks(ctx, th['pos02_radar_before_s']),
                                  tp + _ticks(ctx, th['pos02_radar_after_s']), team_sid=V):
                    continue
            else:
                # V 自己也把他打残了（≥50 或不少于挨的），再拉去补合理
                back = H[(H['att'] == V) & (H['vic'] == K) & (H['tick'] >= tk - w) & (H['tick'] <= tk)]['dmg'].sum()
                took = H[(H['att'] == K) & (H['vic'] == V) & (H['tick'] >= tk - w)
                         & (H['tick'] < tk - _ticks(ctx, th['pos02_hurt_gap_s']))]['dmg'].sum()
                if back >= th['pos02_dmg_back_max'] or back >= took:
                    continue
            if _unseen_gap(ctx, K, V, tp, tk - _ticks(ctx, th['pos02_break_end_s'])) < th['pos02_break_s']:
                continue
            if _team_util_near(ctx, V, rnd, kp1, tp, tk, th['pos02_util_r']):
                continue
            # 审核单第 15 条（手册 POS-C）：再拉那一次离前一件事（队友倒下 / 第一次挨枪；同一场对枪里你最后一次和他交手）
            # <= pos02_trade_s 是正常补枪 / 晃身再出；凶手在前一件事里被你们打掉 >= pos02_tagged_hp 也是合理的补人
            t_re, gap_s, prev, seen_again = _repeek(ctx, K, V, tp, tk, kind)
            if not seen_again:
                # 复核修正（第 15 条）：一直没有重新互相看见——穿墙 / 隔烟打死的不算“又拉出去”；其余要死者断视线后真的挪过
                # （8d R17 队友D 原地站着被穿墙打死）
                if bool(getattr(dv, 'penetrated', 0)) or bool(getattr(dv, 'thrusmoke', False)):
                    continue
                mv0 = tk - _ticks(ctx, th['pos02_repeek_look_s'])
                if C.path_len(ctx, V, mv0, tk) < th['pos02_repeek_move']:
                    continue
            tag_dmg = int(H[(H['vic'] == K) & H['att'].isin(team) & (H['tick'] >= tp - _ticks(ctx, th['pos02_tag_back_s']))
                            & (H['tick'] <= t_re)]['dmg'].sum())
            if gap_s <= th['pos02_trade_s'] or tag_dmg >= th['pos02_tagged_hp']:
                continue
            blame = '你的责任' if V == U else '队友或团队'
            pt, _ = _plant(ctx, rnd)
            left = None
            if pt is not None and pt <= tk and C.side_of(ctx, V, rnd) == 'CT':
                left = th['pos02_bomb_s'] - (tk - pt) / tr
                if left <= th['pos02_bomb_left_s']:
                    blame = '合理代价'
            kn, kpl = C.name_of(ctx, K), C.place_cn_at(ctx, K, tk)
            vw = _who(ctx, V)
            if kind == 'mate':
                p0, p1 = C.place_cn_at(ctx, T0, tp), C.place_cn_at(ctx, V, tk)
                # 两个人倒下的报点不一样时不写“在这里”，写各自的位置（635 高分玩家50 R1）；同一条线是看凶手开枪的位置
                where = '刚在这里' if p0 == p1 else f'刚在{p0}'
                text = (f'{_who(ctx, T0)} {where}被 {kn} 打死，{vw}' + ('' if p0 == p1 else f'在{p1}')
                        + f'隔了一会儿又从同一条线原样拉出去，{kn} 还在{kpl}原地架着，把{vw}打死了')
            else:
                # 凶手写名字：“他还在……”紧跟在队友后面读着像队友在架（128 R22 高分玩家04 / 高分玩家12）
                text = f'{vw}刚被 {kn} 打掉不少血，隔了一会儿又从同一条线原样拉出去，{kn} 还在{kpl}原地架着，把{vw}打死了'
            if blame == '合理代价':
                text += '；包快炸了，只能去拉'
            facts = dict(t=_t(ctx, tk, rnd), t_prior=_t(ctx, tp, rnd), variant=kind, victim=C.name_of(ctx, V),
                         first=C.name_of(ctx, T0), killer=kn, killer_place=kpl, killer_weapon=C.weapon_cn(dv.wk),
                         bomb_left=None if left is None else round(left, 1), t_repeek=_t(ctx, t_re, rnd),
                         repeek_gap_s=round(gap_s, 2), t_prev=_t(ctx, prev, rnd), killer_tagged=tag_dmg)
            alt = '还可以换个位置、换个时机，或者先给一颗闪再出；这条线他刚开过枪，正等着下一个人'
            kb = _kb(ctx, [], C.side_of(ctx, V, rnd), C.place_at(ctx, V, tk))
            ev = (f'{kn} 两次开枪几乎在同一个位置，中间{vw}有一段时间不在他视野里，又出现在同一条线上'
                  if kind == 'mate' else f'{kn} 第一次打中{vw}后没挪位置，{vw}离开他视野后又拉回同一条线')
            hits.append(_hit(ctx, 'POS-02', rnd, tk, V, blame, text, alt, facts, kb, ev, death_tick=tk, death_sid=V,
                             merge_key=f'killer:{rnd}:{K}', kind='判定' if blame == '合理代价' else None))
            break
    return hits


# ================================================================ POS-03 同一个点和狙/好枪硬拼
def pos03(ctx, rnd):
    th = _th(ctx)
    U = C.user_sid(ctx)
    F = C.fires(ctx, rnd, sid=U)
    if not len(F):
        return []
    rows = list(F.itertuples())
    gap = _ticks(ctx, th['pos03_gap_s'])
    groups, cur = [], [rows[0]]
    for r in rows[1:]:
        if int(r.tick) - int(cur[-1].tick) <= gap:
            cur.append(r)
        else:
            groups.append(cur)
            cur = [r]
    groups.append(cur)
    hits = []
    for g in groups:
        t0, t1 = int(g[0].tick), int(g[-1].tick)
        if len(g) < th['pos03_min_shots'] or (t1 - t0) < _ticks(ctx, th['pos03_min_dur_s']):
            continue
        best, bc = None, 0
        for e in C.enemies(ctx, rnd, U):
            c = sum(1 for r in g if C.alive(ctx, e, int(r.tick), rnd)
                    and _aim_at(ctx, U, e, int(r.tick), r.user_pitch, r.user_yaw) <= th['pos03_aim_deg'])
            if c > bc:
                best, bc = e, c
        if best is None or bc < len(g) * th['pos03_aim_frac']:
            continue
        E = best
        pts = np.array([[r.user_X, r.user_Y, r.user_Z] for r in g], dtype=float)
        med = np.median(pts, axis=0)
        if float(np.mean(np.linalg.norm(pts - med, axis=1) <= th['pos03_spot_r'])) < th['pos03_spot_frac']:
            continue
        H = C.hurts(ctx, rnd)
        dmg = H[(H['att'] == U) & (H['vic'] == E) & (H['tick'] >= t0) & (H['tick'] <= t1 + 8)]['dmg'].sum()
        if dmg > th['pos03_dmg_max']:
            continue
        ew, mw = C.weapon_at(ctx, E, t0), g[0].wk
        if not (C.is_sniper(ew) or C.gun_tier(ew) >= C.gun_tier(mw) + 1):
            continue
        en, ep, pl = C.name_of(ctx, E), C.place_cn_at(ctx, E, t0), C.place_cn_at(ctx, U, t0)
        ts = C.fmt_sec(C.t_of(ctx, t0, rnd))
        text = (f'第 {ts} 秒起你在{pl}一直从同一个点出来，和{ep}拿{C.weapon_cn(ew)}的 {en} 对枪，'
                f'几次出来前后一共开了 {len(g)} 枪，一枪没中，{en} 只要一直瞄着这个点就行')
        who = '狙' if C.is_sniper(ew) else '他'
        alt = f'还可以每次只出来打一枪，不中就缩回去换个出枪点，或者先撇个闪再看，让{who}每次都要重新找你'
        facts = dict(t=_t(ctx, t0, rnd), t_end=_t(ctx, t1, rnd), shots=len(g), enemy=en, enemy_place=ep,
                     enemy_weapon=C.weapon_cn(ew), my_weapon=C.weapon_cn(mw), place=pl, aimed_shots=bc)
        # 和同一时刻 SHOT-03 的“连开五枪”是两个数：那条只数第一串，这里数这一段所有出枪（8d R18 46.6）
        ev = ((f'这 {len(g)} 枪准星都对着 {en}' if bc == len(g) else f'{len(g)} 枪里 {bc} 枪准星对着 {en}')
              + '，几乎都从同一个位置打，没有一枪打中他')
        hits.append(_hit(ctx, 'POS-03', rnd, t0, U, '你的责任', text, alt, facts,
                         _kb(ctx, [], C.side_of(ctx, U, rnd), C.place_at(ctx, U, t0)), ev))
    return hits


# ================================================================ POS-04 暴露后换位被没露面的人打
def pos04(ctx, rnd, skip_ticks):
    th = _th(ctx)
    U = C.user_sid(ctx)
    tk, K, drow = _killer_of(ctx, U, rnd)
    if tk is None or tk in skip_ticks or not C.is_enemy(ctx, U, K, rnd):
        return []
    w0, w1 = tk - _ticks(ctx, th['pos04_engage_s']), tk - _ticks(ctx, th['pos04_engage_end_s'])
    others = [e for e in C.enemies(ctx, rnd, U) if e != K]
    engaged = {}
    for f in C.fires(ctx, rnd, sid=U).itertuples():
        if not (w0 <= f.tick <= w1):
            continue
        for A in others:
            if A not in engaged and _spot1(ctx, A, U, f.tick) and not _spot1(ctx, K, U, f.tick):
                engaged[A] = int(f.tick)
    H = C.hurts(ctx, rnd)
    for h in H[(H['att'] == U) & (H['tick'] >= w0) & (H['tick'] <= w1)].itertuples():
        if h.vic in others and h.vic not in engaged:
            engaged[h.vic] = int(h.tick)
    # 只算你倒下时还活着的人：已经被你打死的人不是“还在和他对枪”（635 R1：两个人都已经死了）
    engaged = {A: t for A, t in engaged.items() if C.alive(ctx, A, tk, rnd)}
    if not engaged:
        return []
    # 倒下前 quiet_s 秒内自己还在开枪、或还在挨凶手以外的人打：是还在对枪时被第二个人打死，句子不写“换位”（R12 61.6）
    quiet = tk - _ticks(ctx, th['pos04_quiet_s'])
    F_u = C.fires(ctx, rnd, sid=U)
    dueling = bool(len(F_u[(F_u['tick'] >= quiet) & (F_u['tick'] < tk)])) or         bool(len(H[(H['vic'] == U) & (H['att'] != K) & (H['tick'] >= quiet) & (H['tick'] < tk) & (H['dmg'] > 0)]))
    mv = C.path_len(ctx, U, tk - _ticks(ctx, th['pos04_move_from_s']), tk - _ticks(ctx, th['pos04_move_to_s']))
    if mv < th['pos04_move']:
        return []
    if C.on_radar(ctx, K, tk - _ticks(ctx, th['pos04_unseen_s']), tk - _ticks(ctx, th['pos04_unseen_end_s'])):
        return []
    kp = C.pos(ctx, K, tk)
    if _team_util_near(ctx, U, rnd, kp, tk - _ticks(ctx, th['pos04_util_s']), tk, th['pos04_util_r'], kinds=('flash', 'smoke')):
        return []
    heard = len(C.heard_shots(ctx, U, tk - _ticks(ctx, th['pos04_heard_s']), tk - _ticks(ctx, th['pos04_move_to_s']),
                              shooters=[K])) > 0
    us, them = C.alive_counts(ctx, tk)
    cost = C.side_of(ctx, U, rnd) == 'CT' and _planted(ctx, rnd, tk) and us < them
    kn, kpl = C.name_of(ctx, K), C.place_cn_at(ctx, K, tk)
    eng = '、'.join(f'{C.place_cn_at(ctx, A, t)}的 {C.name_of(ctx, A)}' for A, t in sorted(engaged.items(), key=lambda x: (x[1], x[0])))
    ts = C.fmt_sec(C.t_of(ctx, tk, rnd))
    hid, rev_tk = C.hidden_phrase(ctx, K, tk - _ticks(ctx, th['pos04_unseen_end_s']))
    fight_c = f'你还在和{eng}对枪' if dueling else f'你刚和{eng}交过火'
    if cost:
        text = (f'第 {ts} 秒包已经下了、人比对面少，只能硬回防；{fight_c}' + ('' if dueling else '还在换位') +
                f'，被 {kn} 从{kpl}打死（他{hid}）')
    else:
        known = hid + ('，只在那边开过枪' if heard else '')
        text = (f'第 {ts} 秒{fight_c}、位置已经暴露，' + ('人还在走动' if dueling else '还在换位')
                + f'；{kn} {known}，从{kpl}把你打死')
    alt = '还可以暴露以后先退回刚才的掩体或往队友那边收，换位前先用闪或烟挡一下还没清过的方向，不往没清过的地方走'
    facts = dict(t=_t(ctx, tk, rnd), engaged=[C.name_of(ctx, a) for a in sorted(engaged)], killer=kn, killer_place=kpl,
                 heard=heard, alive=C.alive_str(us, them), planted=_planted(ctx, rnd, tk), move=round(mv),
                 place=C.place_cn_at(ctx, U, tk), last_revealed_t=None if rev_tk is None else _t(ctx, rev_tk, rnd),
                 still_dueling=dueling)
    ev = f'倒下前几秒你一直在走动，你倒下前 {C.fmt_sec(th["pos04_unseen_s"])} 秒里 {kn} 一直没被你们看到过' + ('，只听得到他的枪声' if heard and not cost else '')
    return [_hit(ctx, 'POS-04', rnd, tk, U, '合理代价' if cost else '你的责任', text, alt, facts,
                 _kb(ctx, [], C.side_of(ctx, U, rnd), C.place_at(ctx, U, tk)), ev,
                 death_tick=tk, death_sid=U, kind='判定' if cost else None)]


# ================================================================ 挪动意图（审核单第 8 条，手册 POS-E；拍板 4、Q5、Q6、Q18）
_NADE_DEATH = ('hegrenade', 'molotov', 'incgrenade', 'inferno', 'flashbang', 'smokegrenade', 'decoy')


def _spawn_center(ctx, sid, rnd):
    """sid 这一方本回合开局（冻结结束）全队站位的中心：本方出生点（“往回补”的方向）"""
    st = C.round_info(ctx, rnd)['start']
    ps = [C.pos(ctx, m, st) for m in C.mates(ctx, rnd, sid, include_self=True)]
    ps = [p for p in ps if np.all(np.isfinite(p))]
    return np.mean(ps, axis=0) if ps else None


def _site_side(ctx, sid, tick, site):
    """sid 这一帧在不在 site（'A'/'B'）这一侧：包点报点（pos06w_site_a/b）或平面离包点 <= intent_site_r"""
    th = _th(ctx)
    places = th['pos06w_site_a'] if site == 'A' else th['pos06w_site_b']
    if C.place_at(ctx, sid, tick) in places:
        return True
    xy = th['intent_site_xy'][site]
    p = C.pos(ctx, sid, tick)
    return math.hypot(p[0] - xy[0], p[1] - xy[1]) <= th['intent_site_r']


def _closer(p0, p1, q):
    """从 p0 走到 p1，平面离 q 近了多少"""
    return math.hypot(p0[0] - q[0], p0[1] - q[1]) - math.hypot(p1[0] - q[0], p1[1] - q[1])


def move_intent(ctx, sid, rnd, t0, t1):
    """审核单第 8 条（手册 POS-E；拍板 4）：sid 从 t0 走到 t1 这一段挪动的意图，T、CT 都做。只用玩家当时能拿到的信息
    （击杀栏、雷达、听得到的枪声、自己身上的东西），不看后来的结果。给 POS-05 用，也开放给 TEAM-06 / TEAM-04 / TIME-G1。
    返回按优先排好的 [dict(kind, why, ...)]，空列表 = 无因：
      plant       T、身上带包，[t0, t1 + intent_plant_lead_s] 里自己下了包
      retake      CT、包已下，离包近了 >= intent_closer
      stop_defuse T、包已下，离包近了 >= intent_closer（手里拿着火 / 雷时 >= intent_closer_nade）（Q18：去拦拆）
      pickup      途中捡了枪档更高的枪（item_pickup），捡完到 t1 走了不到 intent_closer
      refill      补位（Q5）：[t0 - intent_refill_s, t1] 击杀栏报了队友倒下，你往他倒下的地方靠近了 >= intent_closer，或往本方出生点回
      anchor      补防（CT）：往某个包点靠近了 >= intent_closer，那一侧活着的队友 <= intent_anchor_max
      block       堵路（CT）：前 intent_block_s 秒有敌人上过你们雷达或开过你听得到的枪，你在往他露面的地方靠近 >= intent_closer
      chase       追人：前 intent_chase_s 秒你们把某个敌人打掉 >= intent_chase_dmg，你在往他最后露面的地方走"""
    th = _th(ctx)
    t0, t1 = int(t0), int(t1)
    if t1 <= t0:
        return []
    side = C.side_of(ctx, sid, rnd)
    p0, p1 = C.pos(ctx, sid, t0), C.pos(ctx, sid, t1)
    tr = _tr(ctx)
    out = []
    pt, psite = _plant(ctx, rnd)
    # 下包
    if side == 'T' and pt is not None and t0 <= pt <= t1 + _ticks(ctx, th['intent_plant_lead_s']):
        r = C.tl_round(ctx, rnd) or {}
        if (r.get('plant') or {}).get('planter') == C.name_of(ctx, sid):
            out.append(dict(kind='plant', why='去下包'))
    # 回防 / 拦拆：包已经下了，往包那边走
    if pt is not None and pt <= t1:
        bomb = _bomb_xy(ctx, rnd)
        # 手里拿着火 / 雷往包那边挪（Q18：8d R21 拿着燃烧弹从 A 楼梯往包那边挪）：离包近一点就算
        nade_hand = C.wkey(C.weapon_at(ctx, sid, t1)) in ('molotov', 'incgrenade', 'hegrenade')
        need = th['intent_closer_nade'] if nade_hand else th['intent_closer']
        if bomb is not None and _closer(p0, p1, bomb) >= need:
            if side == 'CT':
                out.append(dict(kind='retake', why='往包那边回防'))
            else:
                out.append(dict(kind='stop_defuse', why='往包那边去拦拆'))
    # 捡枪
    try:
        pk = ctx.cache('ev_item_pickup', dtype={'user_steamid': str})
    except (FileNotFoundError, KeyError, OSError):
        pk = None
    if pk is not None and len(pk):
        w0 = C.weapon_at(ctx, sid, t0)
        mine = pk[(pk['user_steamid'].astype(str) == str(sid)) & (pk['tick'] >= t0) & (pk['tick'] <= t1)]
        for r in mine.itertuples():
            # 捡完以后又走了一大段的，这一段挪动就不是为了捡枪（bd R2：捡了 Galil 又在 A 点走出去）
            if C.is_gun(r.item) and C.gun_tier(r.item) > max(C.gun_tier(w0), 0)                     and C.path_len(ctx, sid, int(r.tick), t1) < th['intent_closer']:
                out.append(dict(kind='pickup', why=f'去捡{C.weapon_cn(r.item)}', item=C.weapon_cn(r.item)))
                break
    # 补位（Q5）：击杀栏报了队友倒下，往那边补或往本方出生点回
    D = C.deaths(ctx, rnd)
    team = C.mates(ctx, rnd, sid)
    lost = D[D['vic'].isin(team) & (D['tick'] >= t0 - _ticks(ctx, th['intent_refill_s'])) & (D['tick'] <= t1)]
    if len(lost):
        home = _spawn_center(ctx, sid, rnd)
        for r in lost.sort_values('tick').itertuples():
            dpos = C.pos(ctx, r.vic, int(r.tick) - 1)
            # 从击杀栏报出来那一刻（或 t0，取晚的）算起往哪边走（a32 R10：中路两人倒下后你从 B 后巷掉头回中路）
            pa = C.pos(ctx, sid, max(t0, int(r.tick)))
            toward = _closer(pa, p1, dpos) >= th['intent_closer']
            back = home is not None and _closer(pa, p1, home) >= th['intent_closer']
            if toward or back:
                out.append(dict(kind='refill', why=f'{C.name_of(ctx, r.vic)} 倒下后去补那一路',
                                mate=C.name_of(ctx, r.vic), mate_place=C.place_cn_at(ctx, r.vic, int(r.tick) - 1)))
                break
    # 补防（CT）：往只剩一个人（或没人）守的包点靠
    if side == 'CT' and not (pt is not None and pt <= t1):
        for site in sorted(th['intent_site_xy']):
            if _closer(p0, p1, th['intent_site_xy'][site]) < th['intent_closer'] or _site_side(ctx, sid, t0, site):
                continue
            anchors = [m for m in team if C.alive(ctx, m, t0, rnd) and _site_side(ctx, m, t0, site)]
            if len(anchors) <= th['intent_anchor_max']:
                out.append(dict(kind='anchor', why=f'{site} 只剩' + (f"{'、'.join(C.name_of(ctx, m) for m in anchors)} 一个人守"
                                                                    if anchors else '下没人守') + '，去补防', site=site,
                                anchors=[C.name_of(ctx, m) for m in anchors]))
                break
    foes = C.enemies(ctx, rnd, sid)
    # 堵路（CT）：敌人在这条路上露过面
    if side == 'CT':
        lo = t0 - _ticks(ctx, th['intent_block_s'])
        seen = [(e, tk) for e, tk, _ in C.radar_enemies(ctx, t0, look_s=th['intent_block_s'], team_sid=sid)]
        hs = C.heard_shots(ctx, sid, lo, t0, shooters=foes)
        seen += [(r.sid, int(r.tick)) for r in hs.itertuples()]
        for e, tk in sorted(seen, key=lambda x: (-x[1], x[0])):
            if _closer(p0, p1, C.pos(ctx, e, tk)) >= th['intent_closer']:
                out.append(dict(kind='block', why=f'{C.name_of(ctx, e)} 在那条路上露过面，去堵', enemy=C.name_of(ctx, e)))
                break
    # 追人：这个敌人刚被你们打中过
    H = C.hurts(ctx, rnd)
    hh = H[H['att'].isin(C.mates(ctx, rnd, sid, include_self=True)) & H['vic'].isin(foes) & (H['dmg'] > 0)
           & (H['tick'] >= t0 - _ticks(ctx, th['intent_chase_s'])) & (H['tick'] <= t1)]
    for e in sorted(set(hh['vic'])):
        dm = int(hh[hh['vic'] == e]['dmg'].sum())
        if dm < th['intent_chase_dmg'] or not C.alive(ctx, e, t1, rnd):
            continue
        last = C.last_on_radar(ctx, e, t1, look_s=th['intent_chase_s'], team_sid=sid)
        q = C.pos(ctx, e, last if last is not None else int(hh[hh['vic'] == e]['tick'].max()))
        if _closer(p0, p1, q) >= th['intent_closer'] * th['intent_chase_closer_x']:
            by = sorted(set(hh[hh['vic'] == e]['att']))
            out.append(dict(kind='chase', why=f'去补刚被打中的 {C.name_of(ctx, e)}', enemy=C.name_of(ctx, e),
                            by=[C.name_of(ctx, b) for b in by], dmg=dm))
            break
    return out


def _bomb_xy(ctx, rnd):
    """包的位置：下包那一刻下包人脚下（timeline 的 plant.planter）"""
    pt, _ = _plant(ctx, rnd)
    if pt is None:
        return None
    who = ((C.tl_round(ctx, rnd) or {}).get('plant') or {}).get('planter')
    sid = C.sid_of(ctx, who) if who else None
    return C.pos(ctx, sid, pt) if sid else None


def retake_pair(ctx, rnd, sid, t_to):
    """Q6（审核单第 8 条“两路回防看两人到点差多久”）：包下了以后 sid 和走另一条路回防的队友各自“到点”的时刻，
    到点差 > retake_sync_s 算“没对上”。到点 = 第一次离包 3D <= retake_arrive_r，或者离包 <= retake_near_r 时第一次被敌人
    spotted（已经和点上的人接触了）。另一条路 = 两人到点时的报点不同、下包后都往包那边近了 >= intent_closer。
    只看 t_to（含）之前。返回 dict(mate, t_me, t_mate, place_me, place_mate, gap_s, synced) 或 None（给 POST-02 / TEAM-06 用）"""
    th = _th(ctx)
    pt, _ = _plant(ctx, rnd)
    bomb = _bomb_xy(ctx, rnd)
    if pt is None or bomb is None or C.side_of(ctx, sid, rnd) != 'CT' or int(t_to) <= pt:
        return None
    step = int(th['pos06w_enemy_step_ticks'])
    foes = C.enemies(ctx, rnd, sid)

    def _arr(m):
        for t in range(pt, int(t_to) + 1, step):
            if not C.alive(ctx, m, t, rnd):
                return None
            d = C.dist3(C.pos(ctx, m, t), bomb)
            if d <= th['retake_arrive_r'] or (d <= th['retake_near_r'] and any(_spot1(ctx, e, m, t) for e in foes)):
                return t
        return None
    t_me = _arr(sid)
    if t_me is None:
        return None
    best = None
    for m in sorted(C.mates(ctx, rnd, sid)):
        if not C.alive(ctx, m, pt, rnd):
            continue
        t_m = _arr(m)
        if t_m is None or C.place_at(ctx, m, t_m) == C.place_at(ctx, sid, t_me):
            continue
        if _closer(C.pos(ctx, m, pt), C.pos(ctx, m, t_m), bomb) < th['intent_closer']                 and _closer(C.pos(ctx, sid, pt), C.pos(ctx, sid, t_me), bomb) < th['intent_closer']:
            continue
        gap = abs(t_m - t_me) / _tr(ctx)
        if best is None or gap < best['gap_s']:
            best = dict(mate=C.name_of(ctx, m), t_me=_t(ctx, t_me, rnd), t_mate=_t(ctx, t_m, rnd),
                        place_me=C.place_cn_at(ctx, sid, t_me), place_mate=C.place_cn_at(ctx, m, t_m), gap_s=round(gap, 2))
    if best is not None:
        best['synced'] = best['gap_s'] <= th['retake_sync_s']
    return best


# ================================================================ POS-05 人多时主动接五五开的枪
def pos05(ctx, rnd):
    """审核单第 8、9 条（手册 CT-B、POS-E）：人多（CT 没下包 / T 下了包）时，这次交火是不是你发起的——
    接触前 pos05_init_s 秒里你在动（>= pos05_init_speed）才算；停着架、对峙不判；被雷 / 火炸死不判。
    挪动先看意图：补防、补位、捡枪、去拦拆（凶手没露过面时）都不判；去补刚被打中的人（Q13）记小问题；
    下包后往包那边挪、凶手刚露过面（Q18）写“挪位置时走进了 X 的枪线”"""
    th = _th(ctx)
    U = C.user_sid(ctx)
    tk, K, drow = _killer_of(ctx, U, rnd)
    if tk is None or not C.is_enemy(ctx, U, K, rnd):
        return []
    # 被雷 / 火 / 道具炸死：不是对枪（a32 R4：被 对手5（第2场） 的雷炸死）
    if C.wkey(drow['wk']) in _NADE_DEATH:
        return []
    c0 = tk - _ticks(ctx, th['pos05_contact_s'])
    cand = [x for x in (C.spots(ctx, K, U, c0, tk), C.spots(ctx, U, K, c0, tk)) if x is not None]
    # spotted 每 1/8 秒才更新、偶尔整段没有：都没记到时按两人之间第一次打中 / 你对他开的第一枪算开打；
    # 复核修正：原来退回“阵亡前 1 秒”，会落在一步无关的走动上（bd R2 隔烟、a32 R13），两样都没有就不判
    if not cand:
        H0 = C.hurts(ctx, rnd)
        ph = H0[(((H0['att'] == U) & (H0['vic'] == K)) | ((H0['att'] == K) & (H0['vic'] == U))) & (H0['dmg'] > 0)
                & (H0['tick'] >= c0) & (H0['tick'] <= tk)]
        cand = [int(x) for x in ph['tick']]
        cand += [int(x['tick']) for x in C.shots(ctx, U) if x['round'] == rnd and x['target'] == K and c0 <= x['tick'] <= tk]
    if not cand:
        return []
    st = min(cand)
    us, them = C.alive_counts(ctx, st)
    if not (us >= them + 1 and them <= th['pos05_them_max']):
        return []
    side = C.side_of(ctx, U, rnd)
    planted = _planted(ctx, rnd, st)
    if not ((side == 'CT' and not planted) or (side == 'T' and planted)):
        return []
    # 审核单第 9 条：交火那一刻你停着架 / 对峙不判（8d R6 35.0：从 A 楼梯挪到拱门去堵中路，交火时是停着的）
    init_spd = max([v for v in (C.move_speed(ctx, U, t) for t in
                                range(st - _ticks(ctx, th['pos05_init_s']), st + 1, 4)) if v is not None], default=0.0)
    if init_spd < th['pos05_init_speed']:
        return []
    # 复核修正（第 9 条“看是不是你发起的”）：开打前 pos05_init_win_s 秒你自己的平面净位移 >= pos05_init_net，
    # 并且不比凶手的小（是你走向他 / 拉出去，不是他跑过来找你；635 R28 原地晃、bd R2 / a32 R13 是对面跑过来）
    wi = st - _ticks(ctx, th['pos05_init_win_s'])
    my_net = C.dist2(C.pos(ctx, U, wi), C.pos(ctx, U, st))
    his_net = C.dist2(C.pos(ctx, K, wi), C.pos(ctx, K, st))
    if not (my_net >= th['pos05_init_net'] and my_net >= his_net):
        return []
    s0 = st - _ticks(ctx, th['pos05_look_s'])
    # 走路从你最后一次交火（杀人、打中人、挨打）之后算起（635 R1：在 A 楼梯拿了两杀，8 秒窗口一直往回算到 CT 出生点）
    D, H = C.deaths(ctx, rnd), C.hurts(ctx, rnd)
    fights = [int(x) for x in D[(D['att'] == U) & (D['tick'] >= s0) & (D['tick'] < st)]['tick']]
    fights += [int(x) for x in H[((H['att'] == U) | (H['vic'] == U)) & (H['dmg'] > 0) & (H['tick'] >= s0) & (H['tick'] < st)]['tick']]
    if fights:
        s0 = max(fights)
    # 也从“人数占优”开始那一刻算起（128 R22：5v2 在 31.1 秒才出现，之前一直在下水道）
    adv = None
    for dtk in sorted({int(x) for x in D['tick'] if s0 <= int(x) < st}, reverse=True):
        a_us, a_them = C.alive_counts(ctx, dtk + 1)
        if a_us >= a_them + 1:
            adv = dtk
        else:
            break
    if adv is not None:
        b_us, b_them = C.alive_counts(ctx, max(s0, adv - 1))
        if not (b_us >= b_them + 1):
            s0 = max(s0, adv)
    # 窗口开始前你原地站着守的位置：窗口里又回到过这个位置，就从最后一次回到那里算起（128 R22）
    hold, run, best = None, [], []
    for tt in range(int(s0) - _ticks(ctx, th['pos05_look_s']), int(s0) + 1, 8):
        v = C.move_speed(ctx, U, tt)
        if v is not None and v < th['pos05_still_speed']:
            run.append(tt)
            if len(run) > len(best):
                best = list(run)
        else:
            run = []
    if best and (best[-1] - best[0]) >= _ticks(ctx, th['pos05_hold_s']):
        hold = C.pos(ctx, U, best[-1])
        back = [tt for tt in range(int(s0), int(st) + 1, 8) if C.dist3(C.pos(ctx, U, tt), hold) <= th['pos05_hold_r']]
        if back:
            s0 = max(s0, back[-1])
    pl_len = C.path_len(ctx, U, s0, st)
    net = C.dist2(C.pos(ctx, U, s0), C.pos(ctx, U, st))
    # 审核单第 9 条：不看走了多远，距离短但你主动拉出去也算；只去掉原地晃一下（pos05_move / pos05_net 很小）
    if pl_len < th['pos05_move'] or net < th['pos05_net']:
        return []
    if C.facing_deg(ctx, K, st, C.pos(ctx, U, st)) > th['pos05_face_deg']:
        return []
    # 审核单第 8 条：先判意图
    intents = move_intent(ctx, U, rnd, s0, st)
    kinds = [x['kind'] for x in intents]
    kn = C.name_of(ctx, K)
    # 凶手开打前露过面（雷达 / 你听得到他开枪 / 击杀栏）：走进他的枪线是看得到的
    k_lo, k_hi = st - _ticks(ctx, th['pos05_known_s']), st - _ticks(ctx, th['pos05_known_lag_s'])
    known = C.on_radar(ctx, K, k_lo, k_hi) or len(C.heard_shots(ctx, U, k_lo, k_hi, shooters=[K])) > 0 \
        or len(D[(D['att'] == K) & (D['tick'] >= k_lo) & (D['tick'] <= k_hi)]) > 0
    # Q13：凶手开打前刚被你们打中过（>= intent_chase_dmg）——你主动出去是去补他，记小问题（bd R25：3v1 切 USP 跑出 CT 出生点）
    hk = H[(H['vic'] == K) & H['att'].isin(C.mates(ctx, rnd, U, include_self=True)) & (H['dmg'] > 0)
           & (H['tick'] >= st - _ticks(ctx, th['intent_chase_s'])) & (H['tick'] <= st)]
    chase = next((x for x in intents if x['kind'] == 'chase' and x['enemy'] == kn), None)
    if chase is None and int(hk['dmg'].sum()) >= th['intent_chase_dmg']:
        chase = dict(kind='chase', enemy=kn, by=[C.name_of(ctx, b) for b in sorted(set(hk['att']))], dmg=int(hk['dmg'].sum()))
    variant = 'walk'
    if any(k in kinds for k in ('plant', 'retake', 'pickup', 'refill', 'anchor', 'block')):   # 复核修正：堵路也是理由（第 8 条）
        return []                     # 有理由的挪动：补防、补位、捡枪……不算“走出去接枪”
    if 'stop_defuse' in kinds:
        if not known:
            return []                 # Q18：下包后往包那边挪是去拦拆，凶手没露过面不判
        variant = 'line'
    if chase is not None:
        variant = 'chase'
    fp, pl = C.place_cn_at(ctx, U, s0), C.place_cn_at(ctx, U, st)
    ta = '他' if them == 1 else '他们'            # 对面只剩一个人时不写“他们”（128 R19 5v1）
    why = f'包已经下了，可以等{ta}来拆' if planted else f'对面还没下包，可以等{ta}来'
    tsS = C.fmt_sec(C.t_of(ctx, st, rnd))
    far = pl_len >= th['pos05_far']
    walk = (f'你从{fp}走到{pl}' if fp != pl else f'你在{pl}走了一大段') if far else f'你在{pl}主动拉出去'
    hand = C.weapon_cn(C.weapon_at(ctx, U, st))
    if variant == 'line':
        # Q18 + 审核单第 36 条：“走出去找人”→“挪位置时走进了 X 的枪线”，按手里拿的东西写意图
        carry = f'拿着{hand}' if hand in ('火', '雷', '闪', '烟') else ''
        text = (f'第 {tsS} 秒 {C.alive_str(us, them)} 人数占优、{why}，你{carry}往包那边挪位置时走进了 {kn} 的枪线，被打死'
                + (f'（{kn} 刚露过面）' if known else ''))
    elif variant == 'chase':
        who = '、'.join('你' if b == C.name_of(ctx, U) else b for b in chase['by'])
        text = (f'第 {tsS} 秒 {C.alive_str(us, them)} 人数占优、{why}，{kn} 刚被{who}打中，'
                f'{walk}{"去" if far else ""}补他，被他打死')
    else:
        text = f'第 {tsS} 秒 {C.alive_str(us, them)} 人数占优、{why}，{walk}{"去" if far else ""}接 {kn} 的正面枪，被打死'
    if side == 'CT':
        alt = '还可以留在原来的位置和队友交叉架，让对面来找你，人多的时候不用去接五五开的枪'
        kb = _kb(ctx, [], side, C.place_at(ctx, U, st))
    else:
        # 身上有火才写“火留到听到拆包再扔”（e6b R1：手枪局只有双枪，没有道具）
        alt = ('还可以留在掩体后拿枪等拆包声再打，火留到听到拆包再扔' if '火' in UT._nades_at(ctx, U, st)
               else '还可以留在掩体后拿枪等拆包声再打')
        kb = _kb(ctx, [], side, C.place_at(ctx, U, st))
    facts = dict(t=_t(ctx, tk, rnd), t_start=_t(ctx, st, rnd), alive=C.alive_str(us, them), planted=planted,
                 from_place=fp, place=pl, killer=kn, killer_place=C.place_cn_at(ctx, K, st), path=round(pl_len), net=round(net),
                 init_speed=round(init_spd), init_net=round(my_net), killer_net=round(his_net), intents=[x['kind'] for x in intents], variant=variant, killer_known=bool(known),
                 hand=hand)
    if chase is not None:
        facts.update(chase_by=chase['by'], chase_dmg=chase['dmg'])
    ev = (f'开打前你在动，{kn} 正对着你过来的方向' if not far else f'开打前你走了一大段路，{kn} 正对着你过来的方向')
    # Q13：人多时主动跑出去补刚被打中的人，你主动发起，记小问题
    blame = '小问题' if variant == 'chase' else '你的责任'
    return [_hit(ctx, 'POS-05', rnd, tk, U, blame, text, alt, facts, kb, ev, death_tick=tk, death_sid=U)]


# ================================================================ POS-06 / POS-06W 队友倒下没人补得上
def _saw_killer(ctx, M, K, tk):
    """M 在 [tk - saw_before_s, tk + saw_after_s] 看见过凶手：spotted，或准星对着凶手开过枪"""
    th = _th(ctx)
    t0, t1 = tk - _ticks(ctx, th['pos06_saw_before_s']), tk + _ticks(ctx, th['pos06_saw_after_s'])
    if C.spots(ctx, M, K, t0, t1) is not None:
        return True
    for f in C.fires(ctx, sid=M).itertuples():
        if t0 <= f.tick <= t1 and C.alive(ctx, K, int(f.tick)):
            if _aim_at(ctx, M, K, int(f.tick), f.user_pitch, f.user_yaw) <= th['pos06_saw_aim_deg']:
                return True
    return False


def pos06(ctx, rnd):
    th, tr = _th(ctx), _tr(ctx)
    U = C.user_sid(ctx)
    team = C.mates(ctx, rnd, U, include_self=True)
    D = C.deaths(ctx, rnd)
    rinfo = C.round_info(ctx, rnd)
    hits, weak, skip_at = [], [], {}
    for dv in D.itertuples():
        V, K, tk = dv.vic, dv.att, int(dv.tick)
        if V not in team or not C.is_enemy(ctx, V, K, rnd) or tk > rinfo['end']:
            continue
        mates = [m for m in team if m != V and C.alive(ctx, m, tk - 1, rnd)]
        ve = C.eye(ctx, V, tk - 1)
        traded = len(D[(D['vic'] == K) & (D['tick'] > tk) & (D['tick'] <= tk + _ticks(ctx, th['pos06_trade_s']))]) > 0
        info = []
        for M in mates:
            me = C.eye(ctx, M, tk - 1)
            info.append(dict(sid=M, d3=C.dist3(me, ve), dxy=C.dist2(me, ve), dz=me[2] - ve[2], saw=_saw_killer(ctx, M, K, tk)))
        kn, kpl, vpl = C.name_of(ctx, K), C.place_cn_at(ctx, K, tk), C.place_cn_at(ctx, V, tk)
        pen = int(getattr(dv, 'penetrated', 0) or 0) > 0
        # ①② 叠人 / 贴身看不到
        stack = [m for m in info if not m['saw'] and ((m['dxy'] <= th['pos06_stack_xy'] and abs(m['dz']) >= th['pos06_stack_dz'])
                                                       or m['d3'] <= th['pos06_close_3d'])]
        if stack and not traded and not pen:
            stacked = [m for m in stack if m['dxy'] <= th['pos06_stack_xy'] and abs(m['dz']) >= th['pos06_stack_dz']]
            vw = _who(ctx, V)
            if stacked:
                p = stacked[0]
                pw = _who(ctx, p['sid'])
                up = '上面' if p['dz'] > 0 else '下面'
                text = f'{vw} 在{vpl}和 {pw} 叠在一起（{pw}在{up}），被{kpl}的 {kn} 打死，{pw}的位置看不到凶手，补不上'
                sub, partners = '叠人', [p['sid']]
            else:
                partners = [m['sid'] for m in sorted(stack, key=lambda m: (m['d3'], m['sid']))]
                pw = '、'.join(_who(ctx, s) for s in partners)
                text = f'{vw} 在{vpl}被{kpl}的 {kn} 打死时，{pw}就贴在{"你" if V == U else "他"}身边，但看不到凶手，补不上'
                sub = '贴身'
            blame = '你的责任' if V == U else '队友或团队'
            facts = dict(t=_t(ctx, tk, rnd), sub=sub, victim=C.name_of(ctx, V), killer=kn, killer_place=kpl, place=vpl,
                         partner=[C.name_of(ctx, s) for s in partners], user_partner=U in partners,
                         suppress=['TEAM-05', 'TEAM-06'])
            alt = '还可以叠之前先往会露的方向补一颗烟；或者两个人分开、一人看一个方向，倒一个另一个能补'
            ev = ('两人几乎叠在同一个点上、一个在上一个在下' if sub == '叠人' else '两人离得很近') + f'，{_who(ctx, partners[0])}这段时间一直没看到 {kn}'
            hits.append(_hit(ctx, 'POS-06', rnd, tk, V, blame, text, alt, facts,
                             _kb(ctx, [], C.side_of(ctx, V, rnd), C.place_at(ctx, V, tk)), ev,
                             death_tick=tk, death_sid=V, merge_key=f'death:{rnd}:{V}',
                             kind='问题' if V == U else None))
        # ③ → POS-06W 一个人守包点
        w = _pos06w(ctx, rnd, V, K, tk, info, traded)
        if w and w.get('_anchor_skip'):
            skip_at[w['site']] = min(skip_at.get(w['site'], w['tick']), w['tick'])
        elif w:
            weak.append(w)
    # 审核单第 14 条：同一包点一回合只写一条——先留问题（没尝试、点丢了），再留守得好的；同类取先发生的
    # 完整性复核二：同一包点更早倒下的守点人不写（Q10 / 拍板 2）时，后来在这个包点倒下的是补防的人，也不写
    seen_site = set()
    for w in sorted(weak, key=lambda h: (0 if h['facts'].get('verdict') == 'bad' else 1, h['tick'], h['subject'])):
        if w['facts']['site'] in seen_site or w['tick'] > skip_at.get(w['facts']['site'], float('inf')):
            continue
        seen_site.add(w['facts']['site'])
        hits.append(w)
    return hits


def _anchor_util_on(ctx, rnd, V, came, tk):
    """守点人 V 这回合在 tk 之前扔、落在这几个进攻的人身上起了作用的道具（第六轮拍板 1“尝试去拖”）：
    烟 / 火：生效那段时间里（起爆到散 / 灭，截到 tk）有来的人在落点同一报点或 3D <= pos06w_util_r；
    闪：白了来的人；雷：炸到来的人。返回 ([{n, t, tick}], 最早起作用的 tick | None)"""
    th = _th(ctx)
    r = C.tl_round(ctx, rnd) or {}
    U = C.user_sid(ctx)
    if V == U:
        us = r.get('user_util') or []
    else:
        us = [u for u in r.get('team_util') or [] if u.get('who') == C.name_of(ctx, V)]
    names = {C.name_of(ctx, e) for e in came}
    step = int(th['pos06w_enemy_step_ticks'])
    out = []
    for u in sorted(us, key=lambda x: (x.get('t') or 0)):
        n = UT.UTIL_KIND.get(u.get('kind'))
        det = u.get('det_t')
        if n is None or not isinstance(det, (int, float)):
            continue
        t_det = C.tick_of(ctx, rnd, det)
        if t_det >= tk:
            continue
        hit_t = None
        if n in ('烟', '火'):
            end = u.get('end_t')
            t1 = min(tk, C.tick_of(ctx, rnd, end)) if isinstance(end, (int, float)) else min(tk, t_det + _ticks(ctx, 1.0))
            lxy = u.get('land_xy') or []
            lz = (float(lxy[0]), float(lxy[1]), float(u['land_z'])) if len(lxy) == 2 and u.get('land_z') is not None else None
            for t_ in range(t_det, t1 + 1, step):
                for e in came:
                    if not C.alive(ctx, e, t_, rnd):
                        continue
                    if C.place_at(ctx, e, t_) == u.get('land_place') or (
                            lz is not None and C.dist3(C.pos(ctx, e, t_), lz) <= th['pos06w_util_r']):
                        hit_t = t_
                        break
                if hit_t is not None:
                    break
        else:
            key = 'blinded' if n == '闪' else 'damage'
            if any(x.get('enemy') and x.get('name') in names for x in u.get(key) or []):
                hit_t = t_det
        if hit_t is not None:
            out.append(dict(n=n, t=C.sec(det), tick=hit_t))
    return out, (min(x['tick'] for x in out) if out else None)


def _anchor_ok(ctx, rnd, V, t_c):
    """审核单第 14 条（手册 CT-A）：守点人 = 第一次接触那一刻已经在这个包点守着的人；补防过来的不算
    （第 4 场 R17 队友A 从 CT 出生点补到超市）。看接触前 pos06w_anchor_s 秒（开局 pos06w_anchor_setup_s 秒内
    从出生点走过去的路不算）他离接触时的位置平面最远多远：<= pos06w_anchor_r 是一直守在这块的人。返回 (是不是, 最远)"""
    th = _th(ctx)
    rinfo = C.round_info(ctx, rnd)
    w0 = max(t_c - _ticks(ctx, th['pos06w_anchor_s']), rinfo['start'] + _ticks(ctx, th['pos06w_anchor_setup_s']))
    if w0 >= t_c:
        return True, 0.0
    pc = C.pos(ctx, V, t_c)
    far = max(C.dist2(C.pos(ctx, V, t_), pc) for t_ in list(range(w0, t_c, int(th['pos06w_enemy_step_ticks']))) + [t_c])
    return far <= th['pos06w_anchor_r'], far


def _pos06w(ctx, rnd, V, K, tk, info, traded):
    """decisions #23 + 审核单第 14 条（手册 CT-A）：一个人守包点。
    拖够了 = 点丢（这个包点下包）之前回防的人赶到了（进了这个包点的报点或离守点人 3D <= pos06w_arrive_r），
    或者这个点最后没丢；不看守点人活了几秒。
    尝试去拖 = 扔道具（接触前 pos06w_util_pre_s 秒起）/ 往后退着打 / 打一枪换一个位置；
    拿到 >= pos06w_good_kills 个人头（或守到来的人只剩一个）也算尽到了守点人的事。
    判法：尝试了 + 拖够了 → 好（用户：合理代价）；尝试了但回防没赶上 → 不写（Q10）；没尝试、点也丢了 → 问题；
    没尝试但点没丢 → 不写（拍板 2）。秒死（接触 pos06w_quick_death_s 秒内倒下、0 杀）不评成好。
    补防过来的人不算守点人；接触以后才赶到的队友是“回防赶上了”，不是“两个人守”"""
    th, tr = _th(ctx), _tr(ctx)
    U = C.user_sid(ctx)
    raw = C.place_at(ctx, V, tk - 1)
    site = 'A' if raw in th['pos06w_site_a'] else 'B' if raw in th['pos06w_site_b'] else None
    if site is None or C.side_of(ctx, V, rnd) != 'CT' or traded or _planted(ctx, rnd, tk):
        return None
    us, them = C.alive_counts(ctx, tk, sid=V)
    if us < th['pos06w_min_alive']:
        return None
    rinfo = C.round_info(ctx, rnd)
    # 回看：这块地方实际来了几个人（结果，只用来描述）
    vp = C.pos(ctx, V, tk - 1)
    step = int(th['pos06w_enemy_step_ticks'])
    came = []
    back0 = tk - _ticks(ctx, th['pos06w_enemy_back_s'])
    for e in C.enemies(ctx, rnd, V):
        # 回看窗口开始时还活着的才算；窗口里被他打死的人也是来过这块地方的（只看 tk-1 活着会漏掉他刚杀的人）
        if not C.alive(ctx, e, max(back0, rinfo['start']), rnd):
            continue
        ts = range(back0, tk + _ticks(ctx, th['pos06w_enemy_fwd_s']), step)
        if min(C.dist3(C.pos(ctx, e, t), vp) for t in ts) <= th['pos06w_iso_r']:
            came.append(e)
    c0 = max(rinfo['start'], tk - _ticks(ctx, th['pos06w_contact_back_s']))
    # 这段时间里他打死的人也是来打这块包点的（bd R27：队友B 39.8 秒打死 Jungle 的 对手5（第4场））
    D0 = C.deaths(ctx, rnd)
    for v in D0[(D0['att'] == V) & (D0['tick'] >= c0) & (D0['tick'] <= tk)]['vic']:
        if C.is_enemy(ctx, V, v, rnd) and v not in came:
            came.append(v)
    if len(came) < th['pos06w_enemies']:
        return None
    # 第一次接触：他看见对面 / 对面看见他 / 他开枪 / 互相打中
    t_info = min([x for x in (C.spots(ctx, V, e, c0, tk) for e in came) if x is not None], default=None)
    t_seen = min([x for x in (C.spots(ctx, e, V, c0, tk) for e in came) if x is not None], default=None)
    # 开火只算对着来的人开的（C-TARGET），开局试枪、乱扫不算接触
    t_fire = next((x['tick'] for x in C.shots(ctx, V) if x['round'] == rnd and c0 <= x['tick'] <= tk and x['target'] in came), None)
    H = C.hurts(ctx, rnd)
    hin = H[(H['vic'] == V) & (H['att'].isin(came)) & (H['tick'] >= c0) & (H['tick'] <= tk) & (H['dmg'] > 0)]
    hout = H[(H['att'] == V) & (H['vic'].isin(came)) & (H['tick'] >= c0) & (H['tick'] <= tk) & (H['dmg'] > 0)]
    t_hurt = int(hin['tick'].min()) if len(hin) else tk
    t_dmg = int(hout['tick'].min()) if len(hout) else None
    t_c = min([x for x in (t_info, t_seen, t_fire, t_hurt, t_dmg) if x is not None])
    # 补防过来的人不算守点人（审核单第 14 条）
    is_anchor, anchor_far = _anchor_ok(ctx, rnd, V, t_c)
    if not is_anchor:
        return None
    # 不是一个人守：接触那一刻已经有活着的队友在这块包点（报点）或在他身边 iso_r 内
    site_places = th['pos06w_site_a'] if site == 'A' else th['pos06w_site_b']
    pv_c = C.pos(ctx, V, t_c)
    rot_cand = []
    for m in sorted(C.mates(ctx, rnd, V)):
        if not C.alive(ctx, m, t_c, rnd):
            continue
        if C.place_at(ctx, m, t_c) in site_places or C.dist3(C.pos(ctx, m, t_c), pv_c) <= th['pos06w_iso_r']:
            return None
        rot_cand.append(m)
    # 接触那一刻不在这边、之后在他倒下前看见了凶手的队友 = 回防赶到了（下面算拖够了）；其余能看见凶手的人不存在（上面已排除）
    lead = _ticks(ctx, th['pos06w_info_lead_s'])
    saw_first = t_info is not None and t_info <= t_hurt - lead
    shot_first = t_dmg is not None and t_dmg < t_hurt
    info_ok = saw_first or shot_first
    pt, psite = _plant(ctx, rnd)
    lost = pt is not None and psite == site and pt > tk
    # 接触之前就往这几个人来的路上扔、并且起了作用的道具也是“尝试去拖”（8d R12 队友C：火、烟封 B 二楼）
    u_hits, t_util = _anchor_util_on(ctx, rnd, V, came, tk)
    t_start = min(t_c, t_util) if t_util is not None else t_c
    # 弱守到这几个人只剩 pos06w_end_left 个就结束了：之后是一对一，不再是一个人拖人多（8d R8 队友B 打到 5v1 后倒下）
    t_end = tk
    alive_came = [e for e in came if C.alive(ctx, e, t_start, rnd)]
    for dv in C.deaths(ctx, rnd).sort_values('tick').itertuples():
        if dv.vic in alive_came and t_start <= int(dv.tick) < tk:
            alive_came = [e for e in alive_came if e != dv.vic]
            if len(alive_came) <= th['pos06w_end_left']:
                t_end = int(dv.tick)
                break
    ended_early = t_end < tk
    held = C.sec(C.t_of(ctx, t_end, rnd)) - C.sec(C.t_of(ctx, t_start, rnd))     # 只进 facts，页面不写秒数
    # ---- 尝试去拖（手册 CT-A tried）
    nade_kinds = [x['n'] for x in UT._throws(ctx, V, rnd)
                  if t_c - _ticks(ctx, th['pos06w_util_pre_s']) <= x['tick'] <= tk] + [u['n'] for u in u_hits]
    p_k = C.pos(ctx, V, tk - 1)

    def _nearest(t_):
        ds = [C.dist3(C.pos(ctx, V, t_), C.pos(ctx, e, t_)) for e in came if C.alive(ctx, e, t_, rnd)]
        return min(ds) if ds else None
    n_c, n_k = _nearest(t_c), _nearest(tk - 1)
    # 往后退着打：接触后离开接触位置 >= fall_move，并且离最近的进攻的人远了 >= fall_gain
    fell_back = (C.dist3(p_k, pv_c) >= th['pos06w_fall_move'] and n_c is not None and n_k is not None
                 and n_k - n_c >= th['pos06w_fall_gain'])
    # 打一枪换一个位置：两串开火之间换了 >= repos_shot 的位置；或者走了 >= repos_path、中间断过对面的视线
    bs = [b for b in C.bursts(ctx, V) if b['round'] == rnd and t_c <= b['tick'] <= tk and b['target'] in came]
    shot_move = any(C.dist3(C.pos(ctx, V, a['tick']), C.pos(ctx, V, b['tick'])) >= th['pos06w_repos_shot']
                    for a, b in zip(bs, bs[1:]))
    hid = False
    if not shot_move and C.path_len(ctx, V, t_c, tk - 1) >= th['pos06w_repos_path']:
        gap = 0
        for t_ in _frames(ctx, t_c, tk - 1):
            gap = gap + 1 if not any(_spot1(ctx, e, V, t_) for e in came) else 0
            if gap * int(th['frame_step']) / tr >= th['pos06w_repos_hide_s']:
                hid = True
                break
    moved = shot_move or hid
    places = []
    for t_ in list(range(t_c, tk, step)) + [tk - 1]:
        pl_ = C.place_at(ctx, V, t_)
        if pl_ and (not places or places[-1] != pl_):
            places.append(pl_)
    util_k = sorted(set(nade_kinds), key=lambda k: list(UT.NADE_ORDER).index(k) if k in UT.NADE_ORDER else 9)
    attempts = dict(util=util_k, fallback=bool(fell_back), repos=bool(moved and not fell_back))
    D = C.deaths(ctx, rnd)
    kills = int(((D['att'] == V) & D['vic'].isin(came) & (D['tick'] >= t_c - tr) & (D['tick'] <= tk)).sum())
    dmg = int(H[(H['att'] == V) & (H['vic'].isin(C.enemies(ctx, rnd, V))) & (H['tick'] >= t_c - tr) & (H['tick'] <= tk)]['dmg'].sum())
    tried = bool(util_k or fell_back or moved)
    fought = kills >= th['pos06w_good_kills'] or ended_early
    # 秒死：接触后很快倒下、没拿到人头，而且接触前也没有道具卡住过来的人。接触前就用道具把对面卡了很久的不算秒死
    # （8d R12 队友C：火、烟封 B 二楼，对面停了将近 30 秒，真正接触后才倒下；e6b R19 高分玩家84 同理）
    quick_death = (tk - t_c < _ticks(ctx, th['pos06w_quick_death_s']) and kills == 0
                   and not any(u['tick'] < t_c for u in u_hits))
    # ---- 拖够了（手册 CT-A enough）：点丢之前回防的人赶到了，或者这个点最后没丢
    arrived = []
    if lost:
        for m in rot_cand:
            for t_ in list(range(t_c, pt, step)) + [pt]:
                if not C.alive(ctx, m, t_, rnd):
                    break
                if C.place_at(ctx, m, t_) in site_places or C.dist3(C.pos(ctx, m, t_), vp) <= th['pos06w_arrive_r']:
                    arrived.append((t_, m))
                    break
        # 复核修正（Q10）：他一点伤害没打出来、道具也没卡住来的人、回防的人都是他倒下 pos06w_arrive_after_s 秒以后才到的——
        # 这段守点没拖出时间，是“尝试了但没拖住”（bd R17 队友B：倒下后好几秒 队友A 才到）
        if arrived and kills == 0 and dmg < th['pos06w_dmg_say'] and not u_hits                 and all(t_ > tk + _ticks(ctx, th['pos06w_arrive_after_s']) for t_, _ in arrived):
            arrived = []
        # 完整性复核二（Q10）：接触后不到 pos06w_q10_held_s 秒就倒下、一点伤害没打出来、道具也没卡住人——
        # 就是 Q10 说的“扔了火想拖但一接触就倒下”，后面队友赶到也不是他拖出来的，不算拖够了（bd R17 队友B）
        if arrived and kills == 0 and dmg < th['pos06w_dmg_say'] and not u_hits and held < th['pos06w_q10_held_s']:
            arrived = []
    arrived = [m for _, m in sorted(arrived)]          # 按赶到的先后
    enough = (not lost) or bool(arrived)
    enough_by = 'not_lost' if not lost else 'arrived' if arrived else None
    good = enough and (tried or fought) and not quick_death
    bad = not enough and not tried and not fought
    if not (good or bad):
        # 尝试了但回防没赶上（Q10）、没尝试但点没丢（拍板 2）：不写。完整性复核二：他仍是这个包点这回合的守点人，
        # 之后在同一包点倒下的人是补防过来的（审核单第 14 条），pos06 用这个标记不再给后来的人写弱守（bd R17 队友A）
        return dict(_anchor_skip=True, site=site, tick=tk)
    jobs = dict(info=info_ok, delay=enough, fight=kills >= 1 or dmg >= th['pos06w_dmg_ok'])
    n_ok = sum(1 for k in ('info', 'delay', 'fight') if jobs[k])
    # 其他 CT 在他倒下那一刻在干什么（tk 前 move_back_s 秒到 tk）：出招 / 往这边靠 / 往另一边靠 / 守别处；之前倒下的写“已经倒下”
    fwd, rot, away, hold, dead = [], [], [], [], []
    t_now = tk - 1
    mb = max(rinfo['start'], t_now - _ticks(ctx, th['pos06w_move_back_s']))
    for m in sorted(C.mates(ctx, rnd, V)):
        if not C.alive(ctx, m, t_now, rnd):
            dead.append(m)
            continue
        mp = C.place_at(ctx, m, t_now)
        closer = C.dist3(C.pos(ctx, m, mb), vp) - C.dist3(C.pos(ctx, m, t_now), vp)
        # 最后一小段往哪边走：中途掉头往这边来的人按掉头之后算（e6b R19：高分玩家04 33 秒掉头、高分玩家81 33.5 秒动身往 B）
        mr = max(rinfo['start'], t_now - _ticks(ctx, th['pos06w_recent_s']))
        recent = C.dist3(C.pos(ctx, m, mr), vp) - C.dist3(C.pos(ctx, m, t_now), vp)
        if mp in th['pos06w_forward']:
            fwd.append((m, C.place_cn_at(ctx, m, t_now)))
        elif recent >= th['pos06w_recent_close'] or (closer >= th['pos06w_rotate_close'] and recent > -th['pos06w_recent_close']):
            rot.append(m)
        elif recent <= -th['pos06w_recent_close'] or closer <= -th['pos06w_rotate_close']:
            away.append(m)
        else:
            hold.append(m)
    parts = []
    if fwd:
        parts.append('、'.join(f'{_who(ctx, m)} 在{p}' for m, p in fwd) + '出招')
    if rot:
        parts.append('、'.join(_who(ctx, m) for m in rot) + ' 正往这边靠')
    if away:
        parts.append('、'.join(_who(ctx, m) for m in away) + ' 在往另一边靠')
    if hold:
        parts.append('、'.join(_who(ctx, m) for m in hold) + ' 在守别处')
    if dead:
        parts.append('、'.join(_who(ctx, m) for m in dead) + ' 已经倒下')
    others = '当时' + '，'.join(parts) if parts else '当时其他人都已经倒下'
    vw = _who(ctx, V)
    he = '你' if V == U else '他'
    pl = C.place_cn(raw)
    if saw_first:
        info_c = f'{he}先看到了对面的人'
    elif shot_first:
        info_c = f'{he}先打中了对面'
    else:
        info_c = f'{he}没有提前拿到信息'
    if kills:
        kd = f'打死 {kills} 个人'              # 不写伤害数字（用户写作规则）
    elif dmg >= th['pos06w_dmg_say']:
        kd = '打出了不少伤害，没打死人'
    elif dmg:
        kd = '几乎没打到人'                 # 个位数的伤害不写数字（“他打掉 1 血”读着别扭）
    else:
        kd = '没打到人'
    tk_s = C.fmt_sec(C.t_of(ctx, tk, rnd))
    # 结论写成一句话，不列打分表（e6b R19）；做到的也写上。拖够了 / 没拖够写在倒下那一句里，结论里不再说一遍（第六轮拍板 1）
    done_w = dict(info='信息拿到了', fight='打死了人' if kills else '打出了伤害')
    miss_w = dict(info='没提前拿到信息', fight='没拿到人头' if dmg >= th['pos06w_dmg_say'] else '没打出伤害')
    done_l = [done_w[k] for k in ('info', 'fight') if jobs[k]]
    miss_l = [miss_w[k] for k in ('info', 'fight') if not jobs[k]]
    done_s = '，'.join(done_l[:1] + ['也' + x for x in done_l[1:]])
    miss_s = '，'.join(miss_l[:1] + ['也' + x for x in miss_l[1:]])
    if good:
        if V == U:
            verdict = (f'一个人守点，{done_s}' if done_s else '一个人守点') + (f'，只是{miss_s}' if miss_s else '')
        else:
            verdict = f'一个人守点，{done_s}' if done_s else '一个人守点'     # 夸队友的时刻里不夹缺点（8d R12 队友C）
        blame = '合理代价' if V == U else '—'      # 队友守得好：不当问题列出来（kind=好）
    else:
        verdict = (f'一个人守点，{done_s}，但{miss_s}' if done_s and miss_s else
                   f'一个人守点，{done_s}' if done_s else f'一个人守点，{miss_s}' if miss_s else '')
        blame = ('你的责任' if not (info_ok or jobs['fight']) else '小问题') if V == U else '队友或团队'
    # 接触时身上已经没有道具了：“用道具拖时间”做不到，写换位置拖（e6b R19：闪和烟在接触前都扔完了）
    left = UT._nades_at(ctx, V, t_c)
    has_util = bool(left)

    def _util_c(ks):
        # 烟、火才是卡路口；闪、雷另写（bd R17“扔了火、雷卡路口”把雷也说成了卡路口）
        blk = [k for k in ks if k in ('烟', '火')]
        oth = [k for k in ks if k not in ('烟', '火')]
        if not ks:
            return ''
        return (f"扔了{'、'.join(blk)}卡路口" + (f"，也扔了{'、'.join(oth)}" if oth else '')) if blk else f"扔了{'、'.join(oth)}"
    # 接触前就扔在来的人身上的道具写在接触那一句前面（先封路、后接触，按发生的顺序读）
    order = list(UT.NADE_ORDER)
    pre_k = sorted({u['n'] for u in u_hits if u['tick'] < t_c}, key=lambda k: order.index(k) if k in order else 9)
    post_k = [k for k in attempts['util'] if k not in pre_k]
    pre_u = f'{he}{_util_c(pre_k)}' if pre_k else ''
    did = [('又' if pre_k else '') + _util_c(post_k)] if post_k else []     # 接触前已经写过一次“扔了…卡路口”（bd R25）
    if attempts['fallback']:
        did.append('往后退着打')
    elif attempts['repos']:
        first_cn = C.place_cn(places[0], default='') if len(places) > 1 else ''
        last_cn = C.place_cn(places[-1], default='') if len(places) > 1 else ''
        did.append(f'从{first_cn}换到{last_cn}让对面清点' if first_cn and last_cn and first_cn != last_cn
                   else '打一枪换一个位置让对面清点')
    instant = tk - t_c < _ticks(ctx, th['pos06w_instant_s'])
    left_n = len([e for e in came if C.alive(ctx, e, t_end, rnd)]) if ended_early else None
    if good:
        end_ok = (f'一直守到来打这边的只剩 {left_n} 个人' if ended_early else f'第 {tk_s} 秒倒下')
        # 拖够了的依据写出来：回防的人赶到了（最多点两个名字，多了写“回防的队友”）/ 这个点最后没丢
        if arrived:
            arr_c = (f"，{'、'.join(_who(ctx, m) for m in arrived)} 在点丢之前赶到了"
                     if len(arrived) <= th['pos06w_arrive_names'] else '，回防的队友在点丢之前赶到了')
        else:
            arr_c = '，这个点最后没丢' if not ended_early else ''
        hold_c = '，'.join(did + [end_ok]) + '，拖够了时间' + arr_c
    else:
        end_c = f'第 {tk_s} 秒一接触就倒下' if instant else f'第 {tk_s} 秒倒下'
        # 这回合一颗道具都没带 / 没扔过：写“身上没有道具”；扔过但不是扔在这几个人身上：写“道具已经扔完了”（10ce R2 eco）
        threw = any(x['tick'] < t_c for x in UT._throws(ctx, V, rnd))
        why_short = [f"身上的{UT._nades_text(left)}没扔" if has_util
                     else '接触时道具已经扔完了' if threw else '身上没有道具']
        # 一接触就倒下：来不及往后退、换位置，不拿这个说他
        if not instant:
            why_short.append('也没往后退或换位置')
        hold_c = f'{end_c}，没拖够时间：' + '，'.join(why_short)
    kd_c = f'{he}{kd}'
    if good and V != U:
        # 夸队友的时刻：句子只写事实，结论只放“为什么好”（8d R12）；没做到的那件事不夹进夸奖里，有人出招时才写（弱守的前提）
        text = (f'{vw} 一个人守{pl}，对面来了 {len(came)} 个人；{pre_u + "；" if pre_u else ""}{info_c}，{hold_c}'
                + (f'；{kd_c}' if jobs['fight'] else '')
                + (f"；当时{'、'.join(f'{_who(ctx, m)} 在{p}' for m, p in fwd)}出招" if fwd else ''))
    else:
        text = (f'{vw} 一个人守{pl}，对面来了 {len(came)} 个人；{pre_u + "；" if pre_u else ""}{info_c}，{hold_c}；{kd_c}；{others}'
                + (f'。{verdict}' if verdict else ''))
    facts_why = None
    delay_w = '用道具拖时间' if has_util else '换个位置拖时间'
    if good and V != U:
        alt = ''
        facts_why = (f'一个人守点，{done_s}，' if done_s else '一个人守点，') + (
            '给队友回防争取了时间' if arrived else '拖住了这一波进攻' if tried else '回防时对面少了人，队友打得动')
    elif good and not info_ok:
        alt = '还可以先找个能早看到人的位置拿信息，再拖时间等队友回防'
    elif good:
        alt = ''                          # 做到了的合理代价：没有要改的（“继续这样守”是夸奖，不是另一种思路）
    elif not info_ok:
        alt = f'还可以先找个能早看到人的位置拿信息，看到人就往后收、{delay_w}，等队友回防再一起打'
    else:
        alt = f'还可以拿到信息后先往后收、{delay_w}，等队友回防再一起打'
    facts = dict(t=_t(ctx, tk, rnd), victim=C.name_of(ctx, V), killer=C.name_of(ctx, K), place=pl, site=site,
                 enemies_came=len(came), came=[C.name_of(ctx, e) for e in came], t_contact=_t(ctx, t_c, rnd),
                 t_hold_start=_t(ctx, t_start, rnd), t_hold_end=_t(ctx, t_end, rnd), ended_early=ended_early,
                 util_on=[dict(n=u['n'], t=u['t']) for u in u_hits],
                 held_s=C.sec(held), attempts=attempts, tried=tried, fought=fought, quick_death=quick_death,
                 enough=enough, enough_by=enough_by, arrived=[C.name_of(ctx, m) for m in arrived],
                 anchor_far=round(anchor_far), verdict='good' if good else 'bad',
                 info=info_ok, saw_first=saw_first, shot_first=shot_first, kills=kills, dmg=dmg,
                 jobs_done=n_ok, alive=C.alive_str(us, them),
                 t_plant=_t(ctx, pt, rnd) if lost else None,
                 others_forward=[C.name_of(ctx, m) for m, _ in fwd], others_rotate=[C.name_of(ctx, m) for m in rot],
                 others_away=[C.name_of(ctx, m) for m in away],
                 others_hold=[C.name_of(ctx, m) for m in hold], others_dead=[C.name_of(ctx, m) for m in dead])
    ev = f'{vw}身边没有能补枪的队友，倒下前后对面 {len(came)} 个人到过这块地方'
    if facts_why:
        facts['why_good'] = facts_why
    facts['branch'] = '弱守'          # decisions #23：替代 POS-06 分支③，规则 id 仍是 POS-06
    if ended_early:
        # 他倒下时弱守已经结束（来的人只剩一个）：这次死另有原因（POS-01 之类），弱守不再挂在这次死上（8d 队友B R8）
        if not good:
            return None
        if V == U:
            facts['why_good'] = (f'一个人守点，{done_s}，' if done_s else '一个人守点，') + '给队友回防争取了时间'
            text = f'{vw} 一个人守{pl}，对面来了 {len(came)} 个人；{pre_u + "；" if pre_u else ""}{info_c}，{hold_c}；{kd_c}'
        return _hit(ctx, 'POS-06', rnd, t_start, V, '—', text, '', facts,
                    _kb(ctx, [], 'CT', raw), ev, merge_key=f'hold:{rnd}:{V}', kind='好')
    return _hit(ctx, 'POS-06', rnd, tk, V, blame, text, alt, facts,
                _kb(ctx, [], 'CT', raw), ev, death_tick=tk, death_sid=V,
                merge_key=f'death:{rnd}:{V}',
                kind=('好' if good and V != U else '问题' if V == U and blame != '合理代价' else None))


# ================================================================ POS-07 一块地方被两个方向连杀
def pos07(ctx, rnd):
    th = _th(ctx)
    U = C.user_sid(ctx)
    team = C.mates(ctx, rnd, U, include_self=True)
    rinfo = C.round_info(ctx, rnd)
    D = C.deaths(ctx, rnd)
    dd = [r for r in D.itertuples() if r.vic in team and C.is_enemy(ctx, r.vic, r.att, rnd) and r.tick <= rinfo['end']]
    hits, used_b = [], set()
    for i in range(len(dd)):
        for j in range(i + 1, len(dd)):
            a, b = dd[i], dd[j]
            if b.tick - a.tick > _ticks(ctx, th['pos07_window_s']):
                break
            if (int(b.tick), b.vic) in used_b:
                continue                 # 同一次后倒下的人只配一对（dc2a R1：高分玩家69、高分玩家13 各配一条“你先后死在 B 二楼”，写了两遍）
            pa, pb = (a.user_X, a.user_Y, a.user_Z), (b.user_X, b.user_Y, b.user_Z)
            ka, kb_ = (a.attacker_X, a.attacker_Y, a.attacker_Z), (b.attacker_X, b.attacker_Y, b.attacker_Z)
            if C.dist2(pa, pb) > th['pos07_victim_r'] or C.dist2(ka, kb_) < th['pos07_killer_apart']:
                continue
            if C.dist3(ka, pa) < th['pos07_long'] or C.dist3(kb_, pb) < th['pos07_long']:
                continue
            if (b.tick - rinfo['start']) > _ticks(ctx, th['pos07_tmax_s']) or _planted(ctx, rnd, int(b.tick)):
                continue
            if C.place_at(ctx, a.vic, int(a.tick)) in th['pos07_site_places']:
                continue
            if a.att == b.att:
                continue                 # 同一个人换了位置连杀两个，不是“两个方向”（d5a R19：一个被 A 点的人打死、一个被 A 点的人打死）
            v0, v1 = _who(ctx, a.vic), _who(ctx, b.vic)
            pl = C.place_cn_at(ctx, a.vic, int(a.tick))
            pl_b = C.place_cn_at(ctx, b.vic, int(b.tick))
            kp0, kp1 = C.place_cn_at(ctx, a.att, int(a.tick)), C.place_cn_at(ctx, b.att, int(b.tick))
            if kp0 == kp1:
                continue                 # 两个凶手在同一个报点：句子写不出“两个方向”
            t0s, t1s = C.fmt_sec(C.t_of(ctx, a.tick, rnd)), C.fmt_sec(C.t_of(ctx, b.tick, rnd))
            # 两个人倒下的报点不一样时各写各的（635 R17：高分玩家47 死在 Jungle、你死在拱门）
            where = f'先后死在{pl}' if pl == pl_b else f'先后死在{pl}和{pl_b}'
            spot = '这个位置' if pl == pl_b else '这一块'
            text = f'第 {t0s}–{t1s} 秒 {v0}、{v1} {where}，一个被{kp0}的人打死、一个被{kp1}的人打死：{spot}同时露给两个方向'
            us, them = C.alive_counts(ctx, int(b.tick) + 1, lag_s=0)
            user_in = U in (a.vic, b.vic)
            facts = dict(t=_t(ctx, b.tick, rnd), t_first=_t(ctx, a.tick, rnd), victims=[C.name_of(ctx, a.vic), C.name_of(ctx, b.vic)],
                         killers=[C.name_of(ctx, a.att), C.name_of(ctx, b.att)], killer_places=[kp0, kp1], place=pl,
                         places=[pl, pl_b],
                         alive_after=C.alive_str(us, them), user_in=user_in)
            alt = '还可以先给其中一个方向补烟，再在这块地方站人；或者两个人分开站，不挤在同一块地方'
            ev = f'两个凶手在两个不同方向、离得都很远，{v0}和{v1}死的位置挨得很近'
            subj = U if user_in else b.vic
            dt = int(a.tick) if a.vic == subj else int(b.tick)
            used_b.add((int(b.tick), b.vic))
            hits.append(_hit(ctx, 'POS-07', rnd, int(b.tick), subj, '队友或团队', text, alt, facts,
                             _kb(ctx, [], C.side_of(ctx, a.vic, rnd), C.place_at(ctx, a.vic, int(a.tick))), ev,
                             death_tick=dt, death_sid=subj))
    return hits


# ================================================================ POS-08 贴着烟站被隔烟打死
def pos08(ctx, rnd):
    th = _th(ctx)
    U = C.user_sid(ctx)
    tk, K, drow = _killer_of(ctx, U, rnd)
    if tk is None or not bool(drow['thrusmoke']):
        return []
    me = C.pos(ctx, U, tk - 1)
    near = [s for s in C.smokes(ctx, rnd) if s['t0'] <= tk <= s['t1'] and math.hypot(s['x'] - me[0], s['y'] - me[1]) <= th['pos08_smoke_r']]
    if not near:
        return []
    # 复核修正：死前 pos08_move_s 秒一直在跑（速度均值 > pos08_still_ups）的不是“贴着烟站”（461d R9：从 B 小往拱门跑着过去）
    vs = [C.move_speed(ctx, U, t) for t in range(tk - _ticks(ctx, th['pos08_move_s']), tk, 8)]
    vs = [v for v in vs if v is not None]
    if vs and float(np.mean(vs)) > th['pos08_still_ups']:
        return []
    us, them = C.alive_counts(ctx, tk - _ticks(ctx, th['pos08_alive_back_s']))     # 判责：死前 2 秒的人数
    cost = us <= them
    us_d, them_d = C.alive_counts(ctx, tk, lag_s=0)                                    # 句子：倒下那一刻的人数
    kn, pl = C.name_of(ctx, K), C.place_cn_at(ctx, U, tk)
    ts = C.fmt_sec(C.t_of(ctx, tk, rnd))
    if cost:
        text = f'第 {ts} 秒你贴着烟站在{pl}，被 {kn} 隔着烟打死；当时人数 {C.alive_str(us_d, them_d)}，能守的位置不多'
    else:
        text = f'第 {ts} 秒你人数占优，还贴着烟站在{pl}，被 {kn} 隔着烟打死'
    alt = '还可以在对面开始隔着烟开枪时离开烟边、换个位置；烟边适合等人从烟里出来，但打完或听到对面隔烟扫就挪个位置'
    sm = near[0]
    facts = dict(t=_t(ctx, tk, rnd), killer=kn, killer_weapon=C.weapon_cn(drow['wk']), place=pl, alive=C.alive_str(us_d, them_d),
                 alive_2s_before=C.alive_str(us, them), smoke_place=C.place_cn(C.nearest_place(ctx, (sm['x'], sm['y'], sm['z']), rnd)), planted=_planted(ctx, rnd, tk))
    ev = f'{kn} 是隔着烟把你打死的，你倒下时就站在一颗还没散的烟边上'
    return [_hit(ctx, 'POS-08', rnd, tk, U, '合理代价' if cost else '你的责任', text, alt, facts,
                 _kb(ctx, [], C.side_of(ctx, U, rnd), C.place_at(ctx, U, tk)), ev,
                 death_tick=tk, death_sid=U, kind='判定' if cost else '问题')]


# ================================================================ POS-G1 被打残/残局拿到击杀后马上收
def pos_g1(ctx, rnd, pos01_ticks):
    th, tr = _th(ctx), _tr(ctx)
    U = C.user_sid(ctx)
    H, D = C.hurts(ctx, rnd), C.deaths(ctx, rnd)
    cand = []
    for h in H[(H['vic'] == U) & (H['dmg'] > 0)].itertuples():
        # 只认枪打出来的掉血，而且这一轮交火掉得够多：雷 / 火炸掉一截不是对枪（e6b R2/R4/R8），
        # 一枪擦掉几滴血也不叫“被打残”（128 R19 雷炸 6 血、8d R12 一枪 3 血）
        if not C.is_gun(h.wk):
            continue
        ex = H[(H['vic'] == U) & (H['tick'] <= int(h.tick)) & (H['tick'] >= int(h.tick) - _ticks(ctx, th['g1_exchange_s']))]
        drop = sum(int(x.dmg) for x in ex.itertuples() if C.is_gun(x.wk) and C.is_enemy(ctx, U, x.att, rnd))
        if drop < th['g1_min_drop']:
            continue
        if C.is_enemy(ctx, U, h.att, rnd) and 0 < h.health <= th['g1_hp'] \
                and C.alive(ctx, h.att, int(h.tick) + _ticks(ctx, th['g1_attacker_alive_s']), rnd):
            cand.append((int(h.tick), '残血', h.att))
    for k in D[(D['att'] == U) & (D['vic'] != U)].itertuples():
        if not C.is_enemy(ctx, U, k.vic, rnd):
            continue
        us, them = C.alive_counts(ctx, int(k.tick) + 1, lag_s=0)
        if them < 1 or us > th['g1_clutch_us']:
            continue
        w = _ticks(ctx, th['g1_seen_s'])
        if any(C.spots(ctx, e, U, int(k.tick) - w, int(k.tick) + w) is not None
               for e in C.enemies(ctx, rnd, U) if e != k.vic and C.alive(ctx, e, int(k.tick), rnd)):
            cand.append((int(k.tick), '残局击杀', k.vic))
    hits, last = [], None
    for t0, kind, who in sorted(cand, key=lambda c: (c[0], c[1])):
        if last is not None and t0 - last < _ticks(ctx, th['g1_gap_s']):
            continue
        if not C.alive(ctx, U, t0 + 1, rnd):
            continue
        if any(0 <= t0 - p <= _ticks(ctx, th['g1_after_pos01_s']) for p in pos01_ticks):
            continue
        foes = C.alive_list(ctx, rnd, t0 + 2, enemy=True)
        if not foes:
            continue
        brk = None
        for t in _frames(ctx, t0, t0 + _ticks(ctx, th['g1_break_s'])):
            if not any(_spot1(ctx, e, U, t) for e in foes):
                brk = t
                break
        if brk is None:
            continue
        if any(_spot1(ctx, e, U, t) for t in _frames(ctx, brk, brk + _ticks(ctx, th['g1_hold_s'])) for e in foes):
            continue
        if not C.alive(ctx, U, t0 + _ticks(ctx, th['g1_survive_s']), rnd):
            continue
        if C.dist3(C.pos(ctx, U, t0), C.pos(ctx, U, brk + _ticks(ctx, th['g1_hold_s']))) < th['g1_move']:
            continue
        if len(H[(H['vic'] == U) & (H['tick'] > t0) & (H['tick'] <= t0 + _ticks(ctx, th['g1_survive_s'])) & (H['dmg'] > 0)]):
            continue
        # 退出视线后几秒内又朝同一个人开枪：还在和他对枪，不是“退出了视线”（128 R3：35.7 秒被打残、36 秒又拿沙鹰打 高分玩家12）
        hi_t = brk + _ticks(ctx, th['g1_survive_s'])
        if kind == '残血' and any(s_['round'] == rnd and t0 < s_['tick'] <= hi_t and s_.get('target') == who
                                 for s_ in C.shots(ctx, U)):
            continue
        last = t0
        why = '被打残' if kind == '残血' else '残局拿到击杀'
        ts = C.fmt_sec(C.t_of(ctx, t0, rnd))
        # 只看了 g1_survive_s 秒：这回合后来还是死了的，不写“活了下来”（635 高分玩家50 R4）
        survived = C.alive(ctx, U, C.round_info(ctx, rnd)['end'], rnd)
        text = f'第 {ts} 秒{why}后马上退出了对面的视线，' + ('没有再挨枪，活了下来' if survived else '接下来几秒没再挨枪')
        facts = dict(t=_t(ctx, t0, rnd), sub=kind, other=C.name_of(ctx, who), t_out=_t(ctx, brk, rnd),
                     place=C.place_cn_at(ctx, U, brk), survived=bool(survived),
                     why_good=(('被打残后没有站着继续对枪，先退出了对面的视线' + ('，人保住了' if survived else ''))
                               if kind == '残血' else '残局拿到击杀后没有原地等着被补，马上退出了对面的视线'))
        ev = f'很快就退到没有敌人看得见你的地方，接下来几秒也没再挨枪'
        hits.append(_hit(ctx, 'POS-G1', rnd, t0, U, '—', text, '', facts,
                         _kb(ctx, [], C.side_of(ctx, U, rnd), C.place_at(ctx, U, t0)), ev))
    # 一回合只夸一次（e6b R8 同一句夸奖写了两遍）
    return hits[:int(th['g1_max_per_round'])]


# ================================================================ POS-G2 AWP 击杀后换位
def _g2_effect(ctx, rnd, m, g):
    """审核单第 30 条：挪开以后 g2_effect_s 秒内对面有没有找你原来的位置（只看对面的动作，3D）：
    shot：有敌人朝原来开枪位置的眼睛开枪（离那一点 <= max(g2_old_deg, g2_old_half_bodies 个半身宽)，而且没对着你现在的位置）；
    util：敌人的闪 / 雷 / 火 / 烟落在原来位置平面 g2_util_r 内（从击杀算起）；
    aim：敌人在 g2_aim_dist 内、准星压着原来的位置连续 >= g2_aim_s 秒（同样没对着你现在的位置）。
    返回第一个起作用的 {kind, by, t, clause, why, ev}，没有 None"""
    th = _th(ctx)
    U = C.user_sid(ctx)
    kt, tmv = m['kt'], m['tmv']
    end = tmv + _ticks(ctx, th['g2_effect_s'])
    old_eye = C.eye(ctx, U, kt)
    foes = [e for e in C.enemies(ctx, rnd, U) if e not in [x['vic'] for x in g]]

    def _lim(src, dst):
        return max(th['g2_old_deg'], th['g2_old_half_bodies'] * C.half_body_deg(ctx, C.dist3(src, dst)))
    found = []
    for e in foes:
        fe = C.fires(ctx, rnd, sid=e)
        for f in fe[(fe['tick'] > tmv) & (fe['tick'] <= end)].itertuples():
            src = np.array([f.user_X, f.user_Y, f.user_Z + th['g2_fire_eye_z']])
            err_old = C.aim_err(src, f.user_pitch, f.user_yaw, old_eye)[0]
            if not (np.isfinite(err_old) and err_old <= _lim(src, old_eye)):
                continue
            new_eye = C.eye(ctx, U, int(f.tick))
            err_new = C.aim_err(src, f.user_pitch, f.user_yaw, new_eye)[0]
            if np.isfinite(err_new) and err_new <= _lim(src, new_eye) * th['g2_new_sep_x']:
                continue
            found.append((int(f.tick), 'shot', e))
            break
    cache = getattr(ctx, '_pos_g2_util', None)
    if cache is None:
        cache = dict(flash=_util_ev(ctx, 'flashbang_detonate'), fire=_util_ev(ctx, 'inferno_startburn'),
                     he=_util_ev(ctx, 'hegrenade_detonate'))
        try:
            setattr(ctx, '_pos_g2_util', cache)
        except AttributeError:
            pass
    pts = [u for k in ('flash', 'fire', 'he') for u in cache[k]]
    pts += [dict(t=s_['t0'], x=s_['x'], y=s_['y'], by=s_['by']) for s_ in C.smokes(ctx, rnd)]
    for u in pts:
        if u['by'] in foes and kt < u['t'] <= end and math.hypot(u['x'] - old_eye[0], u['y'] - old_eye[1]) <= th['g2_util_r']:
            found.append((int(u['t']), 'util', u['by']))
    need = max(1, int(math.ceil(th['g2_aim_s'] * _tr(ctx) / int(th['frame_step']))))
    for e in foes:
        run = 0
        for t in _frames(ctx, tmv, end):
            if not C.alive(ctx, e, t, rnd):
                break
            src = C.eye(ctx, e, t)
            if C.dist3(src, old_eye) > th['g2_aim_dist']:
                run = 0
                continue
            p_, y_ = C.view(ctx, e, t)
            err_old = C.aim_err(src, p_, y_, old_eye)[0]
            new_eye = C.eye(ctx, U, t)
            err_new = C.aim_err(src, p_, y_, new_eye)[0]
            on_old = np.isfinite(err_old) and err_old <= _lim(src, old_eye)
            on_new = np.isfinite(err_new) and err_new <= _lim(src, new_eye) * th['g2_new_sep_x']
            run = run + 1 if on_old and not on_new else 0
            if run >= need:
                found.append((t, 'aim', e))
                break
    if not found:
        return None
    t, kind, e = sorted(found, key=lambda x: (x[0], x[1], x[2]))[0]
    n = C.name_of(ctx, e)
    clause = dict(shot=f'{n} 随后朝你原来的位置开枪，打了个空',
                  util=f'{n} 随后往你原来的位置扔了道具，你已经不在那里',
                  aim=f'{n} 随后瞄着你原来的位置找过来，你已经换了角度')[kind]
    why = dict(shot='对面随后朝你原来的位置开枪，打空了', util='对面随后往你原来的位置扔了道具，扔空了',
               aim='对面随后瞄着你原来的位置找过来，你已经换了角度')[kind]
    ev = dict(shot=f'{n} 挪开后不久朝原来开枪的位置开了枪', util=f'{n} 的道具落在原来开枪的位置附近',
              aim=f'{n} 的准星一直压在原来开枪的位置上')[kind]
    return dict(kind=kind, by=n, t=_t(ctx, t, rnd), clause=clause, why=why, ev=ev)


def pos_g2(ctx, rnd):
    th, tr = _th(ctx), _tr(ctx)
    U = C.user_sid(ctx)
    D = C.deaths(ctx, rnd)
    H = C.hurts(ctx, rnd)
    kills = [k for k in D[(D['att'] == U) & (D['vic'] != U)].itertuples()
             if C.is_enemy(ctx, U, k.vic, rnd) and C.wkey(k.wk) in th['g2_weapons']]
    moves = []
    for k in kills:
        kt = int(k.tick)
        if C.alive_counts(ctx, kt + 1, lag_s=0)[1] == 0:
            continue
        end = kt + _ticks(ctx, th['g2_repos_s'])
        if not C.alive(ctx, U, end, rnd):
            continue
        p0 = C.pos(ctx, U, kt)
        tmv = next((t for t in _frames(ctx, kt + 1, end) if C.dist3(C.pos(ctx, U, t), p0) > th['g2_repos_r']), None)
        if tmv is None:
            continue
        moves.append(dict(kt=kt, tmv=tmv, p0=p0, vic=k.vic))
    # 同一次挪动对应好几次击杀：前一次的挪动发生在下一次击杀之后，就并到下一次
    groups = []
    for m in moves:
        if groups and groups[-1][-1]['tmv'] > m['kt']:
            groups[-1].append(m)
        else:
            groups.append([m])
    hits = []
    F = C.fires(ctx, rnd, sid=U)
    for g in groups:
        m = g[-1]
        nxt = F[(F['wk'].isin(th['g2_weapons'])) & (F['tick'] > m['tmv']) & (F['tick'] <= m['tmv'] + _ticks(ctx, th['g2_next_s']))]
        nxt = [f for f in nxt.itertuples() if C.dist3((f.user_X, f.user_Y, f.user_Z), m['p0']) > th['g2_repos_r']]
        pay = None
        if nxt:
            f0 = int(nxt[0].tick)
            pk = D[(D['att'] == U) & (D['tick'] >= f0) & (D['tick'] <= f0 + int(th['g2_next_kill_ticks']))]
            pk = pk[[C.is_enemy(ctx, U, v, rnd) for v in pk['vic']]] if len(pk) else pk
            if len(pk):
                pay = pk.iloc[0]
        dt = (m['tmv'] - m['kt']) / tr
        kts = '、'.join(f'第 {C.fmt_sec(C.t_of(ctx, x["kt"], rnd))} 秒' for x in g)
        quick = dt < th['g2_quick_s']
        # 挪开之前在原地扔了道具（8d R7：拿到人后 4.6 秒才走，中间扔了火和雷）：不写“马上 / 就挪开了”
        util_before = [x['n'] for x in UT._throws(ctx, U, rnd) if m['kt'] < x['tick'] <= m['tmv']]
        # 过了好几秒才走、中间也没扔道具：不算“换位”；这段时间你还在原地挨了枪的（POS-01 狙空后没收）更不能夸
        # “对面找过来时你已经不在那里”（bd R22：26.3 秒拿到人，28.2 秒狙空后还站着被 对手1（第4场） 打中，33 秒才挪开）
        hit_between = len(H[(H['vic'] == U) & (H['tick'] > m['kt']) & (H['tick'] <= m['tmv']) & (H['dmg'] > 0)
                            & H['att'].isin(C.enemies(ctx, rnd))])
        if not quick and (not util_before or hit_between):
            continue
        # 审核单第 30 条（手册 POS-B）：AWP 拿到人后挪开是基本功，不每次都夸；挪开以后真起了作用才夸——
        # 对面朝你原来的位置开枪 / 扔道具，或者瞄着原来的位置找过来，或者你从新角度拿下了下一个人
        eff = _g2_effect(ctx, rnd, m, g)
        if eff is None and pay is None:
            continue
        # 句子不写挪开用了几秒（P6），只分“马上”和“先扔了道具再挪”
        if util_before and not quick:
            ut = '、'.join(sorted(set(util_before), key=UT.NADE_ORDER.index))
            text = f'{kts} AWP {"连着" if len(g) > 1 else ""}拿到人后，先在原地扔了{ut}，再挪开了原来开枪的位置'
        else:
            text = f'{kts} AWP {"连着" if len(g) > 1 else ""}拿到人后，马上挪开了原来开枪的位置'
        if eff is not None:
            text += '，' + eff['clause']
        text_short = text
        if pay is not None:
            text += (f'；第 {C.fmt_sec(C.t_of(ctx, pay["tick"], rnd))} 秒又从新的位置打死了 {C.name_of(ctx, pay["vic"])}'
                     if eff is not None else
                     f'，换了位置之后第 {C.fmt_sec(C.t_of(ctx, pay["tick"], rnd))} 秒又打死了 {C.name_of(ctx, pay["vic"])}')
        facts = dict(t=_t(ctx, m['tmv'], rnd), kill_t=[_t(ctx, x['kt'], rnd) for x in g], dt=round(dt, 1),
                     dt_round=1 if dt < 1 else int(math.floor(dt + 0.5)), primary=pay is not None,
                     victims=[C.name_of(ctx, x['vic']) for x in g],
                     t2=None if pay is None else _t(ctx, int(pay['tick']), rnd),
                     victim2=None if pay is None else C.name_of(ctx, pay['vic']), quick=bool(quick),
                     util_before=util_before, text_short=text_short,
                     effect=None if eff is None else eff['kind'], effect_by=None if eff is None else eff['by'],
                     effect_t=None if eff is None else eff['t'])
        ev = '击杀后离开了开枪的位置' + (f"，{eff['ev']}" if eff is not None else '') + ('，下一枪是在新位置开的' if pay is not None else '')
        alt = '还可以换完位置后隔一会儿再回来，从斜边重新架，让对方猜不到你在哪'
        head = '拿到人后先用道具拖住，再换了位置' if util_before and not quick else '拿到人后马上挪开了开枪的位置'
        facts['why_good'] = head + '，' + (eff['why'] if eff is not None else '从新的角度又拿下了下一个人')
        hits.append(_hit(ctx, 'POS-G2', rnd, m['tmv'], U, '—', text, alt, facts,
                         _kb(ctx, [], C.side_of(ctx, U, rnd), C.place_at(ctx, U, m['kt'])), ev,
                         merge_key=f'awp_move:{rnd}:{g[0]["kt"]}'))
    return hits


# ================================================================ 入口
def run(ctx):
    """跑全部站位规则，返回按 (回合, tick, 规则) 排好的 Hit 列表"""
    U = C.user_sid(ctx)
    hits = pos01(ctx)
    # POS-01 已报过的用户阵亡，POS-04 不重复报
    p01_died = {h['round'] for h in hits if h['death_t'] is not None}
    p01_ticks = [h['tick'] for h in hits]
    for r in C.rounds(ctx):
        rnd = r['n']
        if C.team_of(ctx, U, rnd) == 0:
            continue
        tk = C.death_tick(ctx, U, rnd)
        skip = {tk} if (rnd in p01_died and tk is not None) else set()
        hits += pos02(ctx, rnd) + pos03(ctx, rnd) + pos04(ctx, rnd, skip) + pos05(ctx, rnd) + pos06(ctx, rnd) \
            + pos07(ctx, rnd) + pos08(ctx, rnd) + pos_g1(ctx, rnd, [t for t in p01_ticks if C.round_of(ctx, t) == rnd]) \
            + pos_g2(ctx, rnd)
    return sorted(hits, key=lambda h: (h['round'], h['tick'], h['rule'], h['subject']))
