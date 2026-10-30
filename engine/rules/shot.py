# -*- coding: utf-8 -*-
"""「开枪与准星」规则：SHOT-01..05（问题）、SHOT-G1..G4（好的操作）。

依据 rules_v1.json 同名条目（trigger / blame / template / alt），原型 rules_proto/开枪与准星.py、好的操作.py
（GOOD-01/02/04/06）、下包后_回防_保枪.py（PP-GOOD-01 并进 SHOT-G1）。
C-* 口径（停稳、目标归属、可见、分串、烟、人数、买法）一律用 engine/common.py；阈值只读 ctx.th['shot'] / ctx.th['common']。

对枪动作判定（拍板 7、8；审核单第 2/4/5/6/10/11/12/13/18/25/35 条；手册 1.13）在 engine/rules/duel.py：
  - 每一串都带 duel.signals（拉枪方式 peek、看得见你的敌人、抢枪、续扫、部位……），用来改 SHOT-01/02/03/05 的句式和门槛；
  - 用户每一次被枪打死出一条 SHOT-06（kind 判定 / 问题，facts.duel_carrier=True，facts.display='summary'），
    text = 判定整句；同一个判定 dict 也挂在这次死已有的开枪类命中上（facts.duel）。字段和合并层用法见 duel.py 顶部。
入口：run(ctx) -> list[Hit]（CONTRACT 的 Hit）。facts 里额外放：
  display  'moment'（单独成为一个时刻）/ 'note'（只作那个时刻的附注）/ 'summary'（只计入 SUM-01/02）/ 'merged'（同一串
           已合进别的规则的句子，见 merged_into）
  also     合进本句的其他规则 id（同一串，规则里写的“合成一句”）
  clause   本条作从句时的半句（给合并层用）
句子里不写坐标、速度数字、角度、百分比；报点只用 C.place_cn。
"""
import math
import re

import numpy as np

from engine import common as C
from engine.rules import duel as DU
from engine.rules import util as UT

RULES = ('SHOT-01', 'SHOT-02', 'SHOT-03', 'SHOT-04', 'SHOT-05', 'SHOT-06', 'SHOT-G1', 'SHOT-G2', 'SHOT-G3', 'SHOT-G4')
ORD = {1: '一', 2: '二', 3: '三', 4: '四', 5: '五', 6: '六', 7: '七', 8: '八', 9: '九', 10: '十'}
UTIL_KEYS = ('hegrenade', 'flashbang', 'smokegrenade', 'molotov', 'incgrenade', 'decoy', 'knife', 'c4')
A_AREA = ('BombsiteA', 'Palace', 'PalaceInterior', 'Scaffolding', 'TRamp', 'Stairs', 'Jungle', 'PalaceAlley')
B_AREA = ('BombsiteB', 'Apartments', 'Catwalk', 'Shop', 'Market', 'Truck', 'BackAlley', 'SideAlley')
SEEN_EVID = {'a': '他在你的视野里', 'b': '这一枪打中了他', 'c': '他刚打中过你', 'd': '他正朝你开枪',
             'e': '他看得见你、你正对着他打', 'f': '你正对着他打、他刚露过面', 'chain': '你正连着打他'}



def _seen_ev(info):
    """C-SEEN 的证据半句。'c'（他 ±1 秒内打中过你）只有在他第一次打中你早于你第一枪时才写“他刚打中过你”，
    否则写“他紧接着就打中了你”（R22 63.9：你先开的枪）"""
    code = info.get('seen_code')
    if code == 'c':
        eft = info.get('enemy_first_hit_tick')
        if eft is None or eft >= info['s0']['tick']:
            return '他紧接着就打中了你'
    return SEEN_EVID.get(code, '他在你的视野里')


def _walked_toward(S, info):
    """SHOT-02“你走出去碰到他”：他出现前 walk_back_s 秒里你朝他那边真的走了 walk_toward_u 以上，
    并且他出现前 enemy_set_s 秒里一直站着没动（“早就站在那里”）"""
    ctx, th = S.ctx, S.th
    fs, tgt = info['appear_tick'], info['tgt']
    p0, p1 = C.pos(ctx, S.u, fs - S.ticks(th['walk_back_s'])), C.pos(ctx, S.u, fs)
    pe = C.pos(ctx, tgt, fs)
    dx, dy = pe[0] - p1[0], pe[1] - p1[1]
    n = math.hypot(dx, dy)
    if not n or not all(_f(v) for v in (*p0, *p1)):
        return False
    toward = ((p1[0] - p0[0]) * dx + (p1[1] - p0[1]) * dy) / n
    if toward < th['walk_toward_u']:
        return False
    for tk in range(fs - S.ticks(th['enemy_set_s']), fs + 1, 8):
        v = C.move_speed(ctx, tgt, tk)
        if v is None or v > th['enemy_still_speed']:
            return False
    return True


# 按阵营和地点补一句位置相关的准星提示（通用的预瞄做法，自己写的句子）
KB_TIP = {
    'aim_vip': '从中路看 VIP 时，出去前就把准星放在 VIP 那边站着的人头的高度',
    'aim_bshort': '往 B 小方向推时，准星先放在最近那个角的头线，清完一个再看下一个',
    'aim_a_apps': '在 A 二楼里往出口走时，一个角一个角地清，准星一直停在头的高度',
    'aim_a_exit': '从 A 二楼出口对 Jungle 时，出去前想好要看的角度，一次拉开，别在出口来回小幅度探',
}


# ================================================================ 小工具
class _S:
    """一次运行共用的东西：阈值、用户、名字、敌人开枪方向缓存"""

    def __init__(self, ctx):
        self.ctx = ctx
        self.th = ctx.th['shot']
        self.cm = ctx.th['common']
        self.tr = int(self.cm.get('tickrate', 64))
        self.u = C.user_sid(ctx)
        self.uname = C.name_of(ctx, self.u)
        self.names = C.player_names(ctx)           # 排版时整体保护的人名
        self.aims = {}

    def ticks(self, s):
        return int(round(float(s) * self.tr))

    def meters(self, units):
        return float(units) * self.th['unit_m']


_DIG = '零一二三四五六七八九'


def _cn(n):
    """枪数的中文：1..10 用 common.num_cn（两枪），11..99 写成十一、十九、二十三（句子里不出阿拉伯数字）"""
    n = int(n)
    if n <= 10:
        return C.num_cn(n)
    if n < 100:
        t, o = divmod(n, 10)
        return ('' if t == 1 else _DIG[t]) + '十' + (_DIG[o] if o else '')
    return '很多'


def _f(x):
    """数值是不是有限"""
    return x is not None and isinstance(x, (int, float, np.floating, np.integer)) and math.isfinite(float(x))


def _place(ctx, sid, tick):
    return C.place_cn_at(ctx, sid, tick)


def _hit(S, rule, kind, rnd, t, tick, blame, facts, text, alt='', kb=None, evidence='', death=None, merge_key=None,
         scope='moment'):
    """组装一条 Hit（CONTRACT 字段齐全）"""
    facts = dict(facts)
    facts.setdefault('t', C.sec(t))
    facts.setdefault('round', int(rnd))
    facts.setdefault('user', S.uname)
    return dict(rule=rule, kind=kind, round=int(rnd), t=C.sec(t), tick=None if tick is None else int(tick), scope=scope,
                subject=S.uname, is_user=True, blame=blame,
                death_t=None if death is None else C.sec(death[0]), death_of=None if death is None else S.uname,
                merge_key=merge_key, facts=facts, text=C.tidy(text, S.names), alt=C.tidy(alt or '', S.names), kb=list(kb or []),
                evidence=C.tidy(evidence, S.names))


def _user_death(S, rnd):
    """(阵亡秒, tick, 凶手 sid) 或 None"""
    dt = C.death_tick(S.ctx, S.u, rnd)
    if dt is None:
        return None
    d = C.deaths(S.ctx, rnd)
    row = d[(d['vic'] == S.u) & (d['tick'] == dt)]
    att = row['att'].iloc[0] if len(row) else None
    return C.t_of(S.ctx, dt, rnd), int(dt), att


def _pair_hurts(S, a, b, t0, t1):
    """[t0, t1] 内 a 打 b 或 b 打 a 的 player_hurt（按 tick 排）"""
    h = C.hurts(S.ctx)
    i0, i1 = np.searchsorted(h['tick'].to_numpy(), [int(t0), int(t1) + 1])
    w = h.iloc[i0:i1]
    return w[((w['att'] == a) & (w['vic'] == b)) | ((w['att'] == b) & (w['vic'] == a))]


def _deaths_in(S, t0, t1):
    d = C.deaths(S.ctx)
    i0, i1 = np.searchsorted(d['tick'].to_numpy(), [int(t0), int(t1) + 1])
    return d.iloc[i0:i1]


def _off_head(S, shot, words_ok):
    """这一枪准星离他头（眼睛）多远、往哪偏：(离头单位, 左右单位, 方向词)；方向词只取 words_ok 里的"""
    th = S.th
    tot, lr, ud = shot['err_head']
    dist = shot['dist']
    if not (_f(lr) and _f(ud) and _f(dist)):
        return None
    lr_u = abs(math.tan(math.radians(lr))) * dist
    v_u = abs(math.tan(math.radians(ud))) * dist
    words = []
    if ud < 0 and v_u > th['off_head_u'] * th['word_frac']:
        words.append('比头低' if v_u < th['waist_u'] else '低到了腰')
    elif ud > 0 and v_u > th['off_head_u'] * th['word_frac']:
        words.append('比头高')
    if lr_u > th['side_u']:
        words.append('偏在身侧' if lr_u <= th['on_body_lr_u'] else '偏出了身体')
    words = [w for w in words if w in words_ok]
    return dict(off_u=math.hypot(lr_u, v_u), lr_u=lr_u, word='、'.join(words))


def _spot_seg_start(S, viewer, target, tick, look_s):
    """以 tick 结尾的“viewer 看见 target”连续段（中断 <= spot_gap_ticks）的起点，往回最多 look_s 秒；tick 前后没看见返回 None"""
    gap = int(S.th['spot_gap_ticks'])
    cur = C.spots(S.ctx, viewer, target, tick - gap, tick)
    if cur is None:
        return None
    lo_all = tick - S.ticks(look_s)
    start = cur
    while True:
        lo = max(start - gap, lo_all)
        if lo >= start:
            break
        p = C.spots(S.ctx, viewer, target, lo, start - 1)
        if p is None:
            break
        start = p
    return start


def _enemy_aim_ticks(S, enemy, t0, t1):
    """enemy 在 [t0, t1] 内朝用户开的枪（准星离用户身体 < C-SEEN d 的门槛）的 tick 列表"""
    key = enemy
    if key not in S.aims:
        f = C.fires(S.ctx, sid=enemy)
        S.aims[key] = (f['tick'].to_numpy(np.int64), f['user_pitch'].to_numpy(float), f['user_yaw'].to_numpy(float))
    tk, pit, yaw = S.aims[key]
    i0, i1 = np.searchsorted(tk, [int(t0), int(t1) + 1])
    out = []
    for j in range(int(i0), int(i1)):
        tick = int(tk[j])
        rnd = C.round_of(S.ctx, tick)
        if rnd is None or not C.alive(S.ctx, S.u, tick, rnd) or not C.is_enemy(S.ctx, enemy, S.u, rnd):
            continue
        src = C.eye(S.ctx, enemy, tick)
        be = C.body_err(S.ctx, src, pit[j], yaw[j], S.u, tick)
        dist = C.dist3(src, C.eye(S.ctx, S.u, tick))
        if _f(be) and be < C.aim_limit(S.ctx, dist, S.cm['seen_d_min_deg'], S.cm['seen_d_half_bodies']):
            out.append(tick)
    return out


def _vel(S, sid, tick):
    """帧差速度向量（3D，单位/秒；窗口 move_window_ticks）；瞬移返回 None"""
    w = int(S.cm['move_window_ticks'])
    p1, p0 = C.pos(S.ctx, sid, tick), C.pos(S.ctx, sid, tick - w)
    v = (p1 - p0) / (w / S.tr)
    if not np.all(np.isfinite(v)) or float(np.linalg.norm(v[:2])) > S.cm['move_teleport']:
        return None
    return v


def _lateral_speed(S, target, tick, src):
    """他相对用户视线的横向速度（3D，扣掉沿视线方向的分量）"""
    v = _vel(S, target, tick)
    if v is None:
        return 0.0
    los = C.eye(S.ctx, target, tick) - np.asarray(src, float)
    n = float(np.linalg.norm(los))
    if n < 1:
        return 0.0
    los /= n
    return float(np.linalg.norm(v - np.dot(v, los) * los))


def _end_word(info, hit_first=False):
    """一串的结果。hit_first=True：前面刚写了“他先打中了你”，没分出结果时写清是谁没倒（“他先打中你，没打死”主语不清）"""
    if info['killed']:
        return '最后打死了他'
    if info['died_to_target']:
        return '最后被他打死'
    if info['died']:
        # 写清是谁打死的（“随后你也倒了”读起来像又死了一次，a32 R13）
        return f"随后你被 {info['died_by']} 打死" if info.get('died_by') else '随后你也倒了'
    if info.get('nade_end'):
        # 复核修正：这次对枪没分出结果，几秒后被同一个人的雷 / 火收掉（a32 R2：对手1（第2场） 的雷），不写“你没倒”
        return f"没打死他，随后被他的{info['nade_end']}收掉"
    return '但你没倒，也没打死他' if hit_first else '没打死他'


def _lost_parts(info):
    """第 6 条：你先打中他、最后被他打死时，双方打中的部位（伤害明显偏低补“隔着东西打的”）：
    “先打中他两枪在胸口，他蹲着两枪都打在你头上”；不是这种情况返回 None"""
    D = info.get('D')
    if D is None or info['first_hit'] != '我' or not info['died_to_target'] or info['killed']:
        return None
    mine = DU.my_parts_phrase(D, lead='')
    his = DU.his_parts_phrase(D)
    if not mine or not his:
        return None
    return f"先打中他{mine}，{his}"


def _one_shot(info):
    """他打中你的第一下就是致命的那一下（AWP 一枪）：不写“他先打中了你，最后被他打死”（e6b R7）"""
    return info['first_hit'] == '敌' and info.get('one_shot') and info['died_to_target'] and not info['killed']


def _res_words(info):
    """“他先打中了你，……”+ 结果"""
    if _one_shot(info):
        return '被他一枪打死'
    hf = info['first_hit'] == '敌'
    return '，'.join((['他先打中了你'] if hf else []) + [_end_word(info, hf)])


# ================================================================ 每一串的事实
def _burst_info(S, b):
    """对着看得见的人的一串：出现时刻、谁先打中、结果、隔烟/被闪等（SHOT-01/02/03/05 共用）"""
    ctx, th = S.ctx, S.th
    shots = b['shots']
    s0, sl = shots[0], shots[-1]
    tgt, rnd = b['target'], b['round']
    t_first = s0['tick']
    info = dict(b=b, shots=shots, s0=s0, tgt=tgt, rnd=rnd, enemy=C.name_of(ctx, tgt),
                my_place_raw=s0['place'], my_place=C.place_cn(s0['place']), enemy_place=_place(ctx, tgt, t_first),
                dist=s0['dist'], dist_m=S.meters(s0['dist']) if _f(s0['dist']) else float('nan'),
                weapon=s0['weapon'], wk=s0['wk'], wcls=b['wcls'], side=C.user_side(ctx, rnd))
    info['smoke'] = C.smoke_between(ctx, s0['src'], C.eye(ctx, tgt, t_first), t_first) is not None
    info['flashed'] = C.blind_left(ctx, S.u, t_first) > th['flash_left_s']
    info['hits'] = [bool(x['hit']) for x in shots]
    info['hitgroups'] = [x['hitgroup'] for x in shots if x['hit']]
    info['k_still'] = next((k for k, x in enumerate(shots) if x['still']), None)
    # ---- 对枪动作信号（duel.py）：拉枪方式、看得见你的敌人、抢枪、部位、挨了第一下有没有退出视线……
    ud0 = _user_death(S, rnd)
    near_death = ud0 if (ud0 is not None and ud0[2] == tgt and t_first <= ud0[1]
                         <= sl['tick'] + S.ticks(th['duel_death_window_s'])) else None
    D = DU.signals(S, rnd, tgt, shots=shots, death=near_death)
    info['D'] = D
    info['two_seeing'] = len(D.get('seeing') or []) >= int(th['duel_two_seeing_n'])
    info['guard'] = bool(D.get('fired_before') or D.get('transfer_from'))
    # ---- 他“出现”的时刻（SHOT-02/05）
    look = S.ticks(th['appear_look_s'])
    cands = []
    seg = _spot_seg_start(S, S.u, tgt, t_first, th['appear_look_s'])
    if seg is not None:
        cands.append((seg, 'spotted'))
    aims = _enemy_aim_ticks(S, tgt, t_first - look, t_first + int(th['enemy_aim_fwd_ticks']))
    if aims:
        st = aims[-1]
        for x in reversed(aims):
            if st - x > S.ticks(th['enemy_aim_gap_s']):
                break
            st = x
        cands.append((st, '他开枪'))
    ph = _pair_hurts(S, S.u, tgt, t_first - look, t_first)
    if len(ph):
        cands.append((int(ph['tick'].min()), '打中'))
    info['seg_start'] = seg
    if D['peek'] == 'me_peek' and D.get('my_see') is not None:
        # 你拉出去打架着的人：出现时刻用你自己看见他的那一刻（第 5 条：8d R17 78.9 是你大拉出去，
        # 真正看见时准星就在他身上，不是“他一出来准星差一大截”；spotted 每 1/8 秒才更新，可以略晚于第一枪）
        fs, src = int(D['my_see']), 'spotted'
    elif cands and min(cands)[0] <= t_first:
        fs, src = min(cands)
    else:                               # 三者都没有或都晚于第一枪：第一枪前 0.3 秒内准星离他最远的那一帧（= 开枪前要拉多远）
        best = None
        for tk in range(t_first - S.ticks(th['fallback_s']), t_first + 1, int(th['fallback_step_ticks'])):
            e = C.aim_err(C.eye(ctx, S.u, tk), *C.view(ctx, S.u, tk), C.eye(ctx, tgt, tk))
            if _f(e[0]) and (best is None or e[0] > best[1]):
                best = (tk, e[0])
        fs, src = (best[0] if best else t_first), '开枪前拉枪'
    info['appear_tick'], info['appear_src'] = int(fs), src
    info['appear_before_s'] = (t_first - fs) / S.tr
    e = C.aim_err(C.eye(ctx, S.u, fs), *C.view(ctx, S.u, fs), C.eye(ctx, tgt, fs))
    info['pre_err'] = e[0]
    info['pre_body'] = e[0] / C.half_body_deg(ctx, e[3]) if _f(e[0]) else float('nan')
    info['facing'] = C.facing_deg(ctx, S.u, fs, C.eye(ctx, tgt, fs))
    info['seen_place'] = _place(ctx, tgt, fs)
    info['my_spd_appear'] = C.move_speed(ctx, S.u, fs)
    info['en_spd_appear'] = C.move_speed(ctx, tgt, fs)
    # ---- 谁先打中、结果
    t_lo = min(fs, t_first) - S.ticks(th['pair_hit_back_s'])
    t_hi = sl['tick'] + S.ticks(th['pair_hit_fwd_s'])
    pair = _pair_hurts(S, S.u, tgt, t_lo, t_hi)
    info['first_hit'] = None if not len(pair) else ('我' if pair.iloc[0]['att'] == S.u else '敌')
    eh = pair[pair['att'] == tgt]
    info['enemy_first_hit_tick'] = int(eh['tick'].min()) if len(eh) else None
    info['enemy_hit'] = bool(len(eh))
    info['one_shot'] = bool(len(eh) == 1 and int(eh.iloc[0]['health']) <= 0)
    kd = _deaths_in(S, t_first, sl['tick'] + S.ticks(th['conseq_s']))
    kk = kd[(kd['att'] == S.u) & (kd['vic'] == tgt)]
    # 完整性复核：和 duel.py 同一个口径——只算用枪打死、而且在你倒下之前（bd R25 46.3：你死后你的雷才炸死 对手5（第4场））
    kk = kk[[C.is_gun(w) for w in kk['wk']]] if len(kk) else kk
    _kdu = kd[kd['vic'] == S.u]
    if len(_kdu) and len(kk):
        kk = kk[kk['tick'] <= int(_kdu['tick'].iloc[0])]
    info['killed'] = bool(len(kk))
    info['kill_tick'] = int(kk['tick'].iloc[0]) if len(kk) else None
    info['died'] = bool((kd['vic'] == S.u).any())
    kdu = kd[kd['vic'] == S.u]
    info['died_by'] = C.name_of(ctx, kdu['att'].iloc[0]) if len(kdu) and kdu['att'].iloc[0] in C.enemies(ctx, rnd) else None
    info['died_to_target'] = bool(((kd['vic'] == S.u) & (kd['att'] == tgt)).any())
    kn_ = C.deaths(ctx, rnd)
    kn_ = kn_[(kn_['vic'] == S.u) & (kn_['att'] == tgt) & (kn_['tick'] > sl['tick'])
              & (kn_['tick'] <= sl['tick'] + S.ticks(th['nade_link_s']))]
    info['nade_end'] = (NADE_END_CN.get(C.wkey(kn_['wk'].iloc[0])) if len(kn_) and not info['died_to_target'] and not info['died']
                        else None)
    # 和用户阵亡挂钩：没打死他，并且最后一枪后 conseq_s 秒内死了、或 death_link_s 秒内被这个人打死
    ud = _user_death(S, rnd)
    link = ud is not None and not info['killed'] and t_first <= ud[1] and (
        ud[1] <= sl['tick'] + S.ticks(th['conseq_s'])
        or (ud[2] == tgt and ud[1] <= sl['tick'] + S.ticks(th['death_link_s'])))
    info['death'] = ud if link else None
    # 这一串挨着你的死（1 秒内）：你的位置用阵亡那一刻的报点，和时间线、主要原因同一个来源（128 R23：
    # 开枪那一帧在 Jungle、0.1 秒后死在 CT 出生点，时刻写 Jungle、主要原因写 CT 出生点）
    if link and ud[1] - t_first <= S.ticks(th['death_place_s']):
        dpl = C.place_at(ctx, S.u, ud[1])
        if dpl and C.place_cn(dpl, default=''):
            info['my_place_raw'], info['my_place'] = dpl, C.place_cn(dpl)
    rk = _deaths_in(S, t_first - S.ticks(th['just_killed_s']), t_first - 1)
    mates = set(C.mates(ctx, rnd))
    info['just_killed_mate'] = bool(((rk['att'] == tgt) & rk['vic'].isin(mates)).any())
    info['seen_code'] = next((x['seen'] for x in shots if x['target'] == tgt and x['seen']), None)
    return info


def _base_facts(S, info):
    s0 = info['s0']
    f = dict(enemy=info['enemy'], my_place=info['my_place'], enemy_place=info['enemy_place'], weapon=info['weapon'],
             n=len(info['shots']), hits=sum(info['hits']), moves=[x['move'] for x in info['shots']],
             first_hit=info['first_hit'], killed=info['killed'], died=info['died'],
             died_to_target=info['died_to_target'], seen=info['seen_code'],
             dist_m=round(info['dist_m'], 1) if _f(info['dist_m']) else None, burst_tick=int(s0['tick']))
    if info['kill_tick'] is not None:
        f['kill_tick'] = info['kill_tick']
        f['kill_t'] = C.sec(C.t_of(S.ctx, info['kill_tick'], info['rnd']))
    if info['death'] is not None:
        f['death_t'] = C.sec(info['death'][0])
        f['killer'] = C.name_of(S.ctx, info['death'][2]) if info['death'][2] else None
    return f


# ================================================================ SHOT-01 / 02 / 03 / 05（按串）
def _shot01(S, info):
    th = S.th
    b = info['b']
    if not b['engage_first'] or info['wcls'] not in th['s01_classes']:
        return None
    if info['smoke'] or info['flashed'] or info['shots'][0]['still'] or info['hits'][0]:
        return None
    k = 0
    for x in info['shots']:
        if not x['still'] and not x['hit']:
            k += 1
        else:
            break
    mv = info['shots'][0]['move']
    mv_cn = '跑着' if mv == '跑着' else '还没停稳'
    conseq = info['killed'] or info['died'] or info['died_to_target'] or info['enemy_hit']
    if info['killed']:
        # 打死了他：没停稳只算小问题（审核问题 Q13 默认：人多时跑出去补人、换掉了他，记小问题）
        display, blame = ('note' if info['first_hit'] != '敌' else 'moment'), '小问题'
    else:
        display, blame = ('moment' if conseq else 'summary'), '你的责任'
    near = _f(info['dist_m']) and info['dist_m'] < th['duel_min_dist_m']
    if near or info['two_seeing']:
        # 第 13 条：10 米内贴脸边走边开第一枪是正常打法，只进全场汇总；第 10 条：两个人同时看得见你时不判急停
        display = 'summary'
    D = info['D']
    ep = info['enemy_place']
    if D['peek'] == 'me_peek':
        # 第 5 条：你拉出去打架着的人，第一枪还在走（8d R17 78.9）
        head = (f"第 {C.fmt_sec(b['t'])} 秒在{info['my_place']}{DU.peek_phrase(D, info['my_place'])}，"
                f"{info['weapon']}第一枪{'还在跑' if mv == '跑着' else '还在走'}，没中"
                + (f"（前{C.num_cn(k)}枪都是边走边开，都空了）" if k >= 2 else ''))
    else:
        where = f"打同在{ep}的" if ep == info['my_place'] else f"对{ep}的"
        head = (f"第 {C.fmt_sec(b['t'])} 秒在{info['my_place']}{where} {info['enemy']}，{info['weapon']}第一枪是{mv_cn}开的，没中"
                + (f"（前{C.num_cn(k)}枪都是边走边开，都空了）" if k >= 2 else ''))
    fh = {'我': '之后还是你先打中他', '敌': '他先打中了你', None: '双方都没打中'}[info['first_hit']]
    ew = _end_word(info, info['first_hit'] == '敌')
    tail = fh if (info['first_hit'] is None and ew == '没打死他') else f"{fh}，{ew}"
    lp = _lost_parts(info)
    if lp:
        tail = '之后' + lp                  # 第 6 条：先打中他两枪在胸口，他蹲着两枪都打在你头上
    if _one_shot(info):
        tail = '被他一枪打死'
    facts = _base_facts(S, info)
    facts.update(move=mv_cn, k_miss=k, display=display, peek=D['peek'], near=bool(near),
                 two_seeing=bool(info['two_seeing']))
    ev = f"第一枪开的时候人还{'在跑' if mv == '跑着' else '没停稳'}，这一枪没打中；{_seen_ev(info)}"
    return dict(rule='SHOT-01', head=head, tail=tail, facts=facts, blame=blame, display=display,
                clause=f"第一枪是{mv_cn}开的，没中", evidence=ev,
                alt='还可以先急停（松开方向键或点一下反方向）再开第一枪；你架枪时第一枪都是停稳的，拉枪、进点、补人时也可以先停住再开。')


def _long_spray(S, info):
    """第 12 条：long_spray_m 米外一口气（间隔 <= duel_spray_gap_s）>= long_spray_n 发、只中 <= long_spray_hits 发"""
    th = S.th
    D = info.get('D') or {}
    return (info['wcls'] in ('步枪', '冲锋枪', '机枪') and _f(info['dist_m']) and info['dist_m'] > th['long_spray_m']
            and D.get('first_breath_n', 0) >= int(th['long_spray_n'])
            and D.get('first_breath_hits', 99) <= int(th['long_spray_hits']))


def _shot03_ok(S, info):
    th = S.th
    need = th['spray_min'].get(info['wcls'])
    if not need or not info['b']['seen'] or info['smoke'] or info['flashed']:
        return False
    n = len(info['shots'])
    if _long_spray(S, info):
        return True                     # 一口气扫到底几乎没中（a32 R7 42.0：28 米一口气十九发只中一发）
    if n < need or any(info['hits']):
        return False
    if sum(1 for x in info['shots'] if x['still']) * 2 <= n:
        return False
    if info['wcls'] == '冲锋枪' and not (_f(info['dist_m']) and info['dist_m'] >= th['smg_min_m']):
        return False
    return True


def _pre_kb(info):
    """SHOT-02 补哪句位置提示：只按阵营和地点配（M6）"""
    if info['side'] != 'T':
        return None
    me, sp = info['my_place'], info['seen_place']
    if me in ('中路', '中远') and sp == 'VIP':
        return 'aim_vip'
    if me == '中路' and sp == 'B 小':
        return 'aim_bshort'
    if me == 'A 二楼' and sp == 'Jungle':
        return 'aim_a_exit'
    if me == 'A 二楼' and sp in ('A 二楼', '跳台'):
        return 'aim_a_apps'
    return None


def _side_on(S, info):
    """他先打中你、那一刻你侧着身（准星离他很远），而且你刚扔完道具：{t_util, util, n, dmg} 或 None"""
    ctx, th = S.ctx, S.th
    eft = info['enemy_first_hit_tick']
    if info['first_hit'] != '敌' or eft is None or eft > info['s0']['tick']:
        return None
    if C.facing_deg(ctx, S.u, eft, C.eye(ctx, info['tgt'], eft)) < th['s02_side_deg']:
        return None
    h = C.hurts(ctx, info['rnd'])
    pre = h[(h['att'] == info['tgt']) & (h['vic'] == S.u) & (h['tick'] >= eft) & (h['tick'] < info['s0']['tick'])]
    if int(pre['dmg'].sum()) < th['s02_side_dmg']:
        return None
    f = C.fires(ctx, info['rnd'], sid=S.u, guns_only=False)
    f = f[(~f['gun']) & (f['tick'] <= eft) & (f['tick'] >= eft - S.ticks(th['s02_side_util_s']))]
    f = f[[C.weapon_cn(w, default='') in ('烟', '闪', '火', '雷') for w in f['wk']]] if len(f) else f
    if not len(f):
        return None
    last = f.iloc[-1]
    return dict(t_util=C.t_of(ctx, int(last['tick']), info['rnd']), util=C.weapon_cn(last['wk']), n=len(pre),
                dmg=int(pre['dmg'].sum()))


def _shot02(S, info, is03):
    th = S.th
    b = info['b']
    if not b['engage_first'] or info['wcls'] not in th['s02_classes'] or (is03 and not _long_spray(S, info)):
        return None
    if info['smoke'] or info['flashed']:
        return None
    # 第 11 条：前 recover_s 秒内开过枪、或者接在上一个击杀后面续扫时，准星被后坐带着，不算“差一大截”
    lateral = (info['appear_before_s'] <= th['appear_max_s'] and _f(info['pre_err'])
               and info['pre_err'] >= th['pre_err_deg'] and info['pre_body'] >= th['pre_half_bodies']
               and info['facing'] <= th['facing_deg'] and not info['guard'])
    D = info['D']
    rushed = DU._rushed(th, D)
    ks = info['k_still']
    off = None
    low = False
    if info['wcls'] in th['s02_low_classes'] and ks is not None and ks <= 1:
        off = _off_head(S, info['shots'][ks], th['s02_words'])
        heads = [g for g in info['hitgroups'][:int(th['head_check_n'])] if g in ('head', 'neck')]
        low = (off is not None and bool(off['word']) and off['off_u'] > th['off_head_u']
               and off['lr_u'] <= th['on_body_lr_u'] and not heads and info['dist_m'] > th['min_dist_m']
               and not info['guard'])         # 第 11 条：续扫 / 刚开过枪，记录的准星在压枪
        if ks == 1 and not info['died_to_target']:
            low = False                 # 第二枪才停稳：只有被这个人打死才单独说头线
        if info['hits'][ks] and not info['died_to_target']:
            low = False                 # 停稳那枪打中了、也没输掉这一枪，不算问题
    if not (lateral or low or rushed):
        return None
    bad = (not info['hits'][0]) or info['first_hit'] == '敌' or info['died_to_target']
    if not bad or info['killed']:
        return None
    conseq = info['died'] or info['died_to_target'] or info['enemy_hit']
    display = 'moment' if conseq else 'summary'
    if info['two_seeing']:
        display = 'summary'             # 第 10 条：两个人同时看得见你，输了归站位，不出准星类问题
    if not (lateral or low):
        # 第 4 条：第一枪停住了，但准星还在快速拉、没到他身上就开了，没中（bd R1 44.9）→ 小问题
        t = C.fmt_sec(b['t'])
        head = f"第 {t} 秒在{info['my_place']}{DU.peek_phrase(D, info['my_place'], neutral_both=True)}，停住了，但准星还没拉到他身上就开了，没中"          # 完整性复核二：句首已点名，后半句写“他”（bd R1）
        tail = DU._tail(D, DU.his_parts_phrase(D))
        tail = tail[1:] if tail.startswith('，') else tail
        facts = _base_facts(S, info)
        facts.update(variant='抢枪', lateral=False, low=False, word=None, display=display, peek=D['peek'],
                     rushed=True, appear_src=info['appear_src'], two_seeing=bool(info['two_seeing']))
        ev = f"开第一枪时人已经停稳，但准星还在往他身上拉、离他身体还有一段；{_seen_ev(info)}"
        return dict(rule='SHOT-02', head=head, tail=tail or _end_word(info), facts=facts, blame='小问题', display=display,
                    clause=f"停住了，但准星还没拉到 {info['enemy']} 身上就开了", evidence=ev,
                    alt='还可以停住以后等准星到他身上再开第一枪。', kb=[], variant='抢枪', first=None)
    hgcn = th['hitgroup_cn']
    t = C.fmt_sec(b['t'])
    sp = info['seen_place']
    sp_ok = sp != '某处'
    toward = sp if sp_ok and sp != info['my_place'] else '他那边'      # 同一个报点里写“往他那边走”
    my_still = info['my_spd_appear']
    my_moving = my_still is not None and my_still > C.still_limit(S.ctx, info['wk'], info['shots'][0]['scoped'])
    en_still = info['en_spd_appear'] is not None and info['en_spd_appear'] <= th['enemy_still_speed']
    walk = lateral and my_moving and en_still and _walked_toward(S, info)
    low_part = f"，准星也{off['word']}" if low else ''
    res = _res_words(info)
    rush_part = '，没等准星到他身上就开了' if rushed else ''      # 手册 1.3：8d R4 19.0 并上抢枪
    # 完整性复核二：紧跟在“准星离他还差一大截”后面时不再重复“准星”（bd R16“准星离他还差一大截，准星也低到了腰”）
    low_gap = (f"，也{off['word']}" if not rushed else low_part) if low else ''
    if lateral and walk:
        variant = '你走出去碰到他'
        head = (f"第 {t} 秒你在{info['my_place']}往{toward}走，碰到早就站在那里的 {info['enemy']}，"
                f"那一刻准星离他还差一大截{low_gap}")
        clause = f"碰到他那一刻准星离他还差一大截{low_gap}"
        tail = res
    elif lateral and D['peek'] in ('me_peek', 'both_move'):
        # 第 5 条：句式按实际谁动谁架写；你在动（拉出去 / 双方都在动）时不写“他一出来”
        variant = '你拉他架' if D['peek'] == 'me_peek' else '都在动'
        first = '第一枪没中' if not info['hits'][0] else f"第一枪只打在{hgcn.get(info['hitgroups'][0], '身上')}"
        head = (f"第 {t} 秒在{info['my_place']}{DU.peek_phrase(D, info['my_place'])}，碰上时准星离他还差一大截"
                + rush_part + low_gap)
        clause = '碰上时准星离他还差一大截' + rush_part + low_gap
        lp = _lost_parts(info)
        tail = first + '，' + (lp if lp else _res_words(info))
    elif lateral:
        variant = '他一出来'
        first = '第一枪没中' if not info['hits'][0] else f"第一枪只打在{hgcn.get(info['hitgroups'][0], '身上')}"
        # 他出来的报点就是你站的报点时，“你在 A 楼梯，你本来就看着 A 楼梯那个方向”是废话，不写
        look = f"你本来就看着{sp}那个方向，" if sp_ok and sp != info['my_place'] else ''
        head = (f"第 {t} 秒在{info['my_place']}，{look}"
                f"{info['enemy']} 一出来准星离他还差一大截，要拉过去才开枪"
                + ('，开枪时人也还没停稳' if ks != 0 else '') + rush_part + low_part)
        clause = "他一出来准星离他还差一大截，要拉过去才开枪" + rush_part + low_part      # 没停稳已由 SHOT-01 说了
        lp = _lost_parts(info)
        tail = first + '，' + (lp if lp else _res_words(info))
    else:
        variant = '头线'
        ep_s = ('同在' if info['enemy_place'] == info['my_place'] else '') + info['enemy_place']
        head = f"第 {t} 秒在{info['my_place']}对{ep_s}的 {info['enemy']}，准星对着人，但{off['word']}"
        clause = f"停稳那一枪准星对着人，但{off['word']}"
        tail = _lost_parts(info) or _end_word(info)
    kb = _pre_kb(info)
    alt = '还可以站定时把准星提前放在他最可能出来的口子、和头一样高，等他走进准星再开'
    if kb:
        alt += '；' + KB_TIP[kb]
    alt += '。'
    facts = _base_facts(S, info)
    facts.update(variant=variant, lateral=bool(lateral), low=bool(low), word=off['word'] if low else None,
                 seen_place=sp if sp_ok else None, display=display, appear_src=info['appear_src'], peek=D['peek'],
                 rushed=bool(rushed), two_seeing=bool(info['two_seeing']))
    ev = ('他出现那一刻你朝着他那边，但准星离他还差一大截' if lateral else '停稳那一枪准星对着他身体，没在头线上') \
        + f"；{_seen_ev(info)}"
    side = _side_on(S, info)
    if side is not None:
        # 你扔道具时侧着身、背对他，他先打中你一大截血，你转过来才开枪：这次输的是“侧着身扔道具”，不是准星高低
        # （bd R9：33.5 秒在 Jungle 往外扔烟，A 点的 对手3（第4场） 33.6 / 33.7 秒先打中两枪 46 血，你还的两枪打在胸口、腿上）
        variant = '侧身挨枪'
        head = (f"第 {C.fmt_sec(side['t_util'])} 秒在{info['my_place']}扔{side['util']}时侧身对着{info['enemy_place']}，"
                f"{info['enemy']} 先打中你{C.num_cn(side['n'])}枪；你转过来才还枪" + (f"，准星也{off['word']}" if low else ''))
        clause = f"扔{side['util']}时侧身对着{info['enemy_place']}，{info['enemy']} 先打中你{C.num_cn(side['n'])}枪"
        tail = _end_word(info)
        alt = f"还可以扔{side['util']}前先退到{info['enemy_place']}打不到的地方，扔完再出来架。"
        kb = None
        facts.update(variant=variant, side_on=True, util=side['util'], t_util=C.sec(side['t_util']),
                     pre_dmg=side['dmg'], pre_hits=side['n'])
        ev = f"他打中你的时候你正侧着身、准星没对着他，这两下在你开第一枪之前；{_seen_ev(info)}"
    return dict(rule='SHOT-02', head=head, tail=tail, facts=facts, blame='你的责任', display=display, clause=clause,
                evidence=ev, alt=alt, kb=[], variant=variant,
                first=first if variant in ('他一出来', '你拉他架', '都在动') else None)


def _shot03(S, info, s01):
    th = S.th
    b = info['b']
    n = len(info['shots'])
    cls = info['wcls']
    ks = info['k_still'] if info['k_still'] is not None else 0
    off = _off_head(S, info['shots'][ks], th['s03_words'])
    word = off['word'] if off is not None and off['off_u'] > th['off_head_u'] and off['word'] else ''
    gaps = [(info['shots'][k + 1]['tick'] - info['shots'][k]['tick']) / S.tr for k in range(n - 1)]
    # 第 11 条：停稳的那枪是连发第三发以后（一直连着按）时，记录的准星在压枪，不写“比头低”
    if ks >= int(th['spray_k_guard']) and all(g <= th['duel_spray_gap_s'] for g in gaps[:ks]):
        word = ''
    far = cls == '手枪' and info['dist_m'] > th['far_pistol_m']
    # 第 25 条：手枪前两枪间隔 <= fast_tap_s（沙鹰 deagle_fast_s）才算点太快
    tap_lim = th['deagle_fast_s'] if info['wk'] == 'deagle' else th['fast_tap_s']
    fast = cls == '手枪' and gaps and gaps[0] <= tap_lim and info['dist_m'] > th['fast_tap_m']
    long_spray = _long_spray(S, info)
    rushed = DU._rushed(th, info['D'])
    released = bool(gaps) and max(gaps) > th['duel_breath_gap_s']
    dist_w = '远处' if info['dist_m'] > th['dist_far_m'] else ('中距离' if info['dist_m'] > th['dist_mid_m'] else '近处')
    t = C.fmt_sec(b['t'])
    n_still = sum(1 for x in info['shots'] if x['move'] == '停稳')
    nh = sum(info['hits'])
    allw = f"{_cn(n)}枪全空" if not nh else f"{_cn(n)}枪只中{_cn(nh)}枪"
    if s01:
        rest = n - max(ks, 1)
        body = (f"{info['weapon']}第一枪是{s01['facts']['move']}开的，停下来后又开了{_cn(rest)}枪，"
                f"{allw}")
    else:
        body = f"{info['weapon']}连开{allw}"
    if rushed and not s01:
        # 手册 1.3 / 第 12 条例句（8d R22 55.9）；偏在身侧照写（第 25 条例句 bd R13 39.9）
        body += '，停下来第一枪准星还没对上就开了' + ('，偏在身侧' if '偏在身侧' in word else '')
    # 停稳的不止一枪时不写“停稳的那枪”（e6b R8：三枪里两枪停稳；8d R13/R14/R18）；准星高低只按停稳后第一枪算
    if word and not rushed:
        body += f"，停稳的那枪准星{word}" if n_still <= 1 else f"，停稳后第一枪准星{word}"
    if fast and not far:
        mid = '，前两枪点得太快，上一枪的晃动还没回稳就开了下一枪'
    elif cls == '手枪' and gaps and max(gaps) >= th['s03_pause_s']:
        mid = '，中间停下来重新对准过，后面几枪还是没中'       # R1 31.1：第二、三枪之间停了快一秒
    elif cls == '手枪':
        mid = ''                                        # 第 25 条：0.2–0.3 秒一枪是正常节奏，不写“没停下来重新对准”
    elif long_spray:
        mid = '，一口气扫到底，没有松开重新对准'
    elif released:
        mid = '，后面松开重新打过，还是没中'          # 第 12 条：中间松开过的不写“一直按着没松开”
    else:
        mid = ''
    text = (f"第 {t} 秒在{info['my_place']}对{dist_w}{info['enemy_place']}的 {info['enemy']}，{body}{mid}"
            + ('；他刚打死你队友' if info['just_killed_mate'] else '')
            + ('；远处拿手枪本来就难打中' if far else ''))
    blame = '合理代价' if far else '你的责任'
    if cls == '冲锋枪':
        conseq = info['killed'] or info['died'] or info['died_to_target'] or info['enemy_hit']
        display = 'moment' if conseq else 'summary'
    else:
        display = 'moment'
    if info['two_seeing']:
        display = 'summary'             # 第 10 条：两个人同时看得见你时不写准星 / 连发类问题（bd R13 39.9）
    paused = cls == '手枪' and bool(gaps) and max(gaps) >= th['s03_pause_s']
    if paused:
        # 中间已经停下来重新对准过：不再写“一枪不中先停半拍”（和句子本身矛盾，8d R1 31.1）
        alt = ('还可以前两枪点慢一点，' if fast else '还可以') + '停下来重新对准以后，等准星完全回稳再点。'
    elif cls == '手枪' and (fast or far):
        alt = ('还可以前两枪点慢一点，' if fast and far else '还可以') + '一枪不中先停半拍，等准星回稳、重新对准再点。'
    elif cls == '手枪':
        alt = '还可以每一枪都等准星回到他身上再点。'      # 节奏正常、准星偏了（bd R13 39.9）
    elif long_spray:
        alt = '还可以两三发没中就松开，重新对准再打。'
    elif rushed:
        alt = '还可以停住以后等准星到他身上再开第一枪。'
    else:
        alt = '还可以前两三发没中就松开重新对准。'      # 第 12 条：去掉“远处改成两三发一组点射”
    facts = _base_facts(S, info)
    facts.update(dist_word=dist_w, word=word or None, fast=bool(fast), far=bool(far), long_spray=bool(long_spray),
                 released=bool(released), rushed=bool(rushed), hits_n=int(nh),
                 just_killed_mate=info['just_killed_mate'], display=display, n_cn=C.num_cn(n), paused=bool(paused),
                 max_gap_s=round(max(gaps), 2) if gaps else None, two_seeing=bool(info['two_seeing']))
    still_w = ('每一枪都是停稳开的' if n_still == n else '大多数枪是停稳开的' if n_still * 2 > n
               else f'停稳开的有{C.num_cn(n_still)}枪')
    ev = f"这一串对着同一个人，{still_w}，{'一枪都没打中' if not nh else '几乎都没打中'}；{_seen_ev(info)}"
    return dict(rule='SHOT-03', text=text, facts=facts, blame=blame, display=display,
                clause=f"连开{allw}" + mid, evidence=ev, alt=alt)


def _shot05(S, info):
    th = S.th
    if info['wcls'] not in th['s05_classes'] or not info['b']['seen'] or info['smoke'] or info['flashed']:
        return None
    eft = info['enemy_first_hit_tick']
    if info['first_hit'] != '敌' or eft is None or eft > info['s0']['tick']:
        return None
    seg = info['seg_start']
    if seg is not None and (info['s0']['tick'] - seg) / S.tr >= th['seen_before_s']:
        return None                     # 你早就看见他了，不是“拉出去撞上架枪的人”
    my_v = C.move_speed(S.ctx, S.u, eft)
    en_v = C.move_speed(S.ctx, info['tgt'], eft)
    if my_v is None or en_v is None:
        return None
    if my_v <= C.still_limit(S.ctx, info['wk'], False) or en_v > th['peek_enemy_still']:
        return None
    t = C.fmt_sec(info['b']['t'])
    killed_note = info['killed']
    ep = info['enemy_place']
    D = info['D']
    where = f"，和同在{ep}的" if ep == info['my_place'] else f"，和{ep}的"       # 不写“在 X 打 X 的”
    pre = '，他一直瞄着你出来的位置' if _prefired(S, D) else ''          # 第 35 条
    end = _lost_parts(info) or _end_word(info)
    late = '你停稳再开枪时已经晚了一步，' if info['shots'][0]['still'] else ''
    if late and killed_note:
        late = '你停稳再开枪时已经晚了一步，不过'      # 完整性复核二：“晚了一步，最后打死了他”读着像前后矛盾（bd R6）
    sniper = info['wcls'] == '狙'
    if D['peek'] == 'me_peek':
        # 第 5 条：句式按谁动谁架写——你拉出去、他架着
        lead = f"第 {t} 秒在{info['my_place']}{DU.peek_phrase(D, info['my_place'])}"
        if sniper:
            lead = f"第 {t} 秒在{info['my_place']}，{info['weapon']}跑着拉出去打{'同' if ep == info['my_place'] else ''}在{ep}架着的 {info['enemy']}"
        text = (lead + f"{pre}，他打中你那一刻你人还在动、没停住，他先打中了你；"
                + late + end)
    else:
        # 只写看得到的事实：他打中你那一刻你人还在动、他已经停住（多数是原地晃身出枪，不一定是“拉出去”，R14 86.4）
        text = (f"第 {t} 秒在{info['my_place']}{where} {info['enemy']} 对枪{pre}，他打中你那一刻你人还在动、没停住，"
                f"他已经停住先打中了你；" + late + end)
    kb = []
    if sniper:
        alt = f"还可以别拿着 {info['weapon']} 跑着拉出去"         # 审核问题 Q9 默认：AWP 跑着拉只写这一条
    else:
        alt = '还可以出去前先停在掩体边，让他先暴露，出去后马上停住再开枪；知道那边有人架着就先扔闪'
        if info['side'] == 'T' and info['my_place'] == 'A 二楼' and info['enemy_place'] == 'Jungle':
            alt += '；' + KB_TIP['aim_a_exit']
            kb = []
    alt += '。'
    display = 'note' if killed_note else 'moment'
    near = _f(info['dist_m']) and info['dist_m'] < th['duel_min_dist_m']
    if info['two_seeing'] or near:
        display = 'summary'             # 第 10 条：两个人同时看得见你，归站位；第 13 条：10 米内贴脸不算没停住
    facts = _base_facts(S, info)
    facts.update(display=display, opened_still=bool(info['shots'][0]['still']), peek=D['peek'], prefired=bool(pre),
                 two_seeing=bool(info['two_seeing']), near=bool(near))
    ev = '他第一次打中你的时候你还在移动、他站着没动，这一下在你开第一枪之前'
    return dict(rule='SHOT-05', text=text, facts=facts, blame='小问题', display=display,
                clause='他打中你那一刻你人还在动、没停住', evidence=ev, alt=alt, kb=kb)


def _prefired(S, D):
    """第 35 条：你拉出去被他打中，他看见你那一刻准星就在你身上，而且你刚在同一个位置露过面 = 他一直瞄着你出来的位置"""
    th = S.th
    return bool(D and D.get('reexposed') and _f(D.get('his_err_see')) and D['his_err_see'] <= th['duel_prefire_deg']
                and D.get('peek') in ('me_peek', 'both_move'))


def _shot05_noshot(S):
    """SHOT-05 没开枪的一支：拉出去还在动就被站着的人先打中、一枪没开就被他打死（bd R1 45.0：CT 出生点拉出去被
    对手1（第4场） USP 一枪爆头）。按 C-FIGHT 分串的 SHOT-05 要你开过枪，这种死原来一条命中都没有，页面上这次死就没人写"""
    ctx, th = S.ctx, S.th
    out = []
    for ri in C.rounds(ctx):
        rnd = ri['n']
        ud = _user_death(S, rnd)
        if ud is None or ud[2] is None or not C.is_enemy(ctx, S.u, ud[2], rnd):
            continue
        t_d, dtk, K = ud
        h = C.hurts(ctx, rnd)
        hk = h[(h['att'] == K) & (h['vic'] == S.u) & (h['dmg'] > 0) & (h['tick'] <= dtk)
               & (h['tick'] >= dtk - S.ticks(th['s05_noshot_window_s']))]
        if not len(hk):
            continue
        eft = int(hk['tick'].iloc[0])
        f = C.fires(ctx, rnd, sid=S.u)
        if len(f[(f['tick'] >= eft - S.ticks(th['s05_noshot_window_s'])) & (f['tick'] <= dtk)]):
            continue                                    # 开过枪：按分串的 SHOT-05 / 其他开枪规则算
        if C.blind_left(ctx, S.u, eft) > th['flash_left_s'] or C.smoked(ctx, S.u, K, eft):
            continue
        wk = C.wkey(C.weapon_at(ctx, S.u, eft))
        if not wk or C.weapon_class(wk) not in th['s05_classes']:
            continue
        seg = _spot_seg_start(S, S.u, K, eft, 10.0)
        if seg is not None and (eft - seg) / S.tr >= th['seen_before_s']:
            continue                                    # 早就看见他了，不是拉出去撞上
        # 一枪打死时打中那一帧就是阵亡帧，位置更新不可靠：取那一下前 1..duel_hit_spd_ticks 帧的均值（同 duel.signals）
        vs = [C.move_speed(ctx, S.u, eft - k) for k in range(1, int(th['duel_hit_spd_ticks']) + 1)]
        vs = [v for v in vs if _f(v)]
        my_v, en_v = (float(np.mean(vs)) if vs else C.move_speed(ctx, S.u, eft)), C.move_speed(ctx, K, eft)
        if my_v is None or en_v is None or my_v <= C.still_limit(ctx, wk, False) or en_v > th['peek_enemy_still']:
            continue
        kn, mp, ep = C.name_of(ctx, K), _place(ctx, S.u, eft), _place(ctx, K, eft)
        one = int(hk['tick'].iloc[0]) == dtk or len(hk) == 1
        where = f"，和同在{ep}的" if ep == mp else f"，和{ep}的"
        t = C.t_of(ctx, eft, rnd)
        D = DU.signals(S, rnd, K, shots=None, anchor=eft, death=ud)
        if len(D.get('seeing') or []) >= int(th['duel_two_seeing_n']):
            continue                                    # 第 10 条：两个人同时看得见你，归站位（SHOT-06 的判定写）
        pre = '，他一直瞄着你出来的位置' if _prefired(S, D) else ''
        lead = (f"第 {C.fmt_sec(t)} 秒在{mp}{DU.peek_phrase(D, mp)}" if D['peek'] == 'me_peek'
                else f"第 {C.fmt_sec(t)} 秒在{mp}{where} {kn} 碰上")
        text = (lead + f"{pre}，他打中你那一刻你人还在动、没停住，他已经停住先打中了你，"
                + ('你一枪没开就被他一枪打死' if one else '你一枪没开就被他打死'))
        if C.is_sniper(wk):
            alt = f"还可以别拿着 {C.weapon_cn(wk)} 跑着拉出去。"     # 审核问题 Q9 默认
        else:
            alt = '还可以出去前先停在掩体边，让他先暴露，出去后马上停住再开枪；知道那边有人架着就先扔闪。'
        facts = dict(enemy=kn, my_place=mp, enemy_place=ep, weapon=C.weapon_cn(wk), my_place_raw=C.place_at(ctx, S.u, eft),
                     died=True, died_to_target=True, killed=False, shots=0, display='moment', opened_still=False,
                     clause='他打中你那一刻你人还在动、没停住，你一枪没开', death_t=C.sec(t_d), hit_t=C.sec(t),
                     peek=D['peek'], prefired=bool(pre))
        ev = '他第一次打中你的时候你还在移动、他站着没动，你到倒下都没开枪'
        out.append(_hit(S, 'SHOT-05', '问题', rnd, t, eft, '小问题', facts, text, alt, [], ev, death=ud))
    return out


def _burst_hits(S):
    """SHOT-01/02/03/05：按串判，同一串命中多条时合成一句（主句取单独成时刻的、编号最靠前的）"""
    out = []
    for b in C.bursts(S.ctx):
        if not b['target'] or not b['seen'] or not b['wcls']:
            continue
        info = _burst_info(S, b)
        s01 = _shot01(S, info)
        is03 = _shot03_ok(S, info)
        s02 = _shot02(S, info, is03)
        s03 = _shot03(S, info, s01) if is03 else None
        s05 = _shot05(S, info)
        found = [x for x in (s01, s02, s03, s05) if x]
        if not found:
            continue
        # 每条自己的整句
        def _tsep(x):
            # SHOT-02“要拉过去才开枪”后面的“第一枪没中”是同一枪的结果，用逗号接上；分号隔开读着像另起一件事（8d R3 44.4）
            return '，' if x['rule'] == 'SHOT-02' and str(x.get('tail') or '').startswith('第一枪') else '；'
        for x in found:
            if 'text' not in x:
                x['text'] = f"{x['head']}{_tsep(x)}{x['tail']}"
        # 合成一句：主句 = 单独成时刻的里编号最靠前的；没有就取 note；其余并进主句
        order = {'moment': 0, 'note': 1, 'summary': 2}
        prim = sorted(found, key=lambda x: (order[x['display']], x['rule']))[0]
        others = [x for x in found if x is not prim]
        if others and prim['display'] != 'summary':
            if prim['rule'] == 'SHOT-03':
                pass                                    # SHOT-01 已写进 SHOT-03 的句子（“第一枪是…开的，停下连开”）
            elif 'head' in prim:
                cl = []
                walk = any(x.get('variant') == '你走出去碰到他' for x in found)
                s05 = False
                ls = False
                for x in others:
                    if x['rule'] == 'SHOT-02' and x.get('variant') == '头线' and _lost_parts(info):
                        continue                        # 第 6 条：已经按部位写了（他打在胸口 / 你打在头上），不再写“比头低”
                    if x['rule'] in ('SHOT-01', 'SHOT-05') and x['facts'].get('near'):
                        continue                        # 第 13 条：10 米内边走边开第一枪不写
                    if x['rule'] == 'SHOT-05' and walk:
                        continue                        # “走出去碰到早就站着的他”已经说了同一件事
                    if x['rule'] == 'SHOT-03':
                        ls = ls or bool(x['facts'].get('long_spray'))      # 第 12 条（a32 R7 42.0），见下
                        continue
                    if x['rule'] == 'SHOT-05' and prim['rule'] == 'SHOT-02' and prim.get('first')                             and info['first_hit'] == '敌' and not _one_shot(info):
                        s05 = True                      # 和“他先打中了你”并成一句，见下
                        continue
                    cl.append(x['clause'])
                if s05:
                    # “他打中你那一刻你人还在动；第一枪没中，他先打中了你”说了两遍他先打中你（e6b R3 50.8）：
                    # 先写你的第一枪，再一句写他先打中你、那一刻你还在动，最后写结果
                    prim['text'] = (prim['head'] + ''.join(f"；{c}" for c in cl) + f"，{prim['first']}；"
                                    + f"他先打中了你，那一刻你人还在动、没停住，{_end_word(info, True)}")
                else:
                    tail = prim['tail']
                    if any(x['rule'] == 'SHOT-05' for x in others) and not walk and '他先打中了你，' in tail                             and '他打中你那一刻你人还在动、没停住' in cl:
                        # 同上：SHOT-05 的半句和结尾“他先打中了你”并成一句（51920faa R2、c7e R2）
                        cl = [c for c in cl if c != '他打中你那一刻你人还在动、没停住']
                        tail = tail.replace('他先打中了你，', '他先打中了你，那一刻你人还在动、没停住，', 1)
                    prim['text'] = prim['head'] + ''.join(f"；{c}" for c in cl) + (_tsep(prim) if not cl else '；') + tail
                if ls:
                    # 一口气扫到底：接在“第一枪没中”后面（第一枪之后的事），找不到就接在主句后面
                    lsc = '之后一口气扫到底，没有松开重新对准'
                    f0 = prim.get('first') or ''
                    if f0 and f0 in prim['text']:
                        prim['text'] = prim['text'].replace(f0, f"{f0}，{lsc}", 1)
                    elif '，没中' in prim['text']:
                        prim['text'] = prim['text'].replace('，没中', f"，没中，{lsc}", 1)
                    else:
                        prim['text'] = prim['head'] + f"；{lsc}" + prim['text'][len(prim['head']):]
            prim['facts']['also'] = [x['rule'] for x in others]
            for x in others:
                x['facts']['merged_into'] = prim['rule']
                x['facts']['display'] = 'merged'
                x['display'] = 'merged'
        mk = f"shot:{b['round']}:{b['tick']}"
        for x in found:
            out.append(_hit(S, x['rule'], '问题', b['round'], b['t'], b['tick'], x['blame'],
                            dict(x['facts'], clause=x['clause']), x['text'], x['alt'], x.get('kb'), x['evidence'],
                            death=info['death'], merge_key=mk))
    return out


# ================================================================ SHOT-04 狙空枪
def _exchanging(S, tgt, tick):
    """开这一枪前后 s04_exchange_s 秒内，他真的在和你交火：他打中过你，或者他用枪开过火、当时看得见你（8d R12：
    对手2 这段只扔了火和烟，一枪没开，不能写“正和你交火”）"""
    ctx, w = S.ctx, S.ticks(S.th['s04_exchange_s'])
    h = C.hurts(ctx)
    if len(h[(h['att'] == tgt) & (h['vic'] == S.u) & (h['tick'] >= tick - w) & (h['tick'] <= tick + w) & (h['dmg'] > 0)]):
        return True
    f = C.fires(ctx, sid=tgt)
    f = f[(f['tick'] >= tick - w) & (f['tick'] <= tick + w)]
    return any(C.spots(ctx, tgt, S.u, int(x.tick) - 8, int(x.tick) + 8) is not None for x in f.itertuples())


def _punished(S, r):
    """只空了一枪、停稳开镜：狙空后还站在原地（或切了刀），s04_punish_s 秒内就被他打中——不是“正常失误”，
    是空了以后没收（8d R8：VIP 狙空 对手1 后原地切刀，不到半秒挨了两发 AK）。返回 (说法, 挨枪 tick) 或 None。
    同时还有别人打中你的不算这里（POS-01 的站位句式管）"""
    ctx, th = S.ctx, S.th
    tk, tgt = r['tick'], r['tgt']
    h = C.hurts(ctx)
    w = h[(h['vic'] == S.u) & (h['tick'] > tk) & (h['tick'] <= tk + S.ticks(th['s04_punish_s'])) & (h['dmg'] > 0)]
    if not len(w) or w.iloc[0]['att'] != tgt:
        return None
    if len(h[(h['vic'] == S.u) & (h['att'] == tgt) & (h['tick'] < tk) & (h['tick'] >= tk - S.ticks(1.0)) & (h['dmg'] > 0)]):
        return None                     # 开这一枪前他已经打中过你：是对枪输了（SHOT-05 管，bd R18），不是空了以后没收
    other = h[(h['vic'] == S.u) & (h['att'] != tgt) & (h['tick'] >= tk - S.ticks(1.0)) & (h['tick'] <= int(w['tick'].max()))
              & (h['dmg'] > 0)]
    if len(other):
        return None
    th_tk = int(w.iloc[0]['tick'])
    if C.dist3(C.pos(ctx, S.u, tk), C.pos(ctx, S.u, th_tk)) > th['s04_punish_move']:
        return None
    v = C.move_speed(ctx, S.u, th_tk)
    if v is None or v > C.still_limit(ctx, 'awp', False):
        return None                     # 挨枪那一刻你在动（bd R18 SHOT-05：他打中你那一刻你人还在动），不是“还站在原地”
    knife = C.wkey(C.weapon_at(ctx, S.u, th_tk)) == 'knife'
    return ('切了刀还站在原地' if knife else '还站在原地'), th_tk


def _shot04(S):
    ctx, th = S.ctx, S.th
    shots = [s for s in C.shots(ctx) if s['wk'] in th['s04_weapons']]
    miss = []
    prev_tgt = {}                                       # 回合 -> (tick, 目标)：本规则口径下认出的上一枪
    for s in shots:
        if s['hit']:
            prev_tgt[s['round']] = (s['tick'], s['target'])
            continue
        tgt, code = s['target'], s['seen']
        if tgt and not code:                            # 规则口径：全服务器没有 spotted 时离身体不到 3° 也算，不要雷达
            code = C.seen(ctx, S.u, tgt, s['tick'], shot=s, f_min_deg=th['f_min_deg'], f_need_radar=False)
        if not code and s['round'] in prev_tgt:         # 3 秒内连着打同一个人的按 8°
            pt, pg = prev_tgt[s['round']]
            c = s['cands'].get(pg)
            if pg and s['tick'] - pt <= S.ticks(S.cm['chain_sniper_s']) and c and _f(c[0]) \
                    and c[0] <= S.cm['chain_deg_sniper']:
                tgt, code = pg, 'chain'
        ident = bool(tgt and code)
        rec = dict(s=s, tick=s['tick'], t=s['t'], round=s['round'], ident=ident, tgt=tgt if ident else None, reasons=[])
        if ident:
            prev_tgt[s['round']] = (s['tick'], tgt)
            if C.smoke_between(ctx, s['src'], C.eye(ctx, tgt, s['tick']), s['tick']) is not None:
                continue
            if C.blind_left(ctx, S.u, s['tick']) > th['flash_left_s']:
                continue
            c = s['cands'].get(tgt, (float('nan'), float('nan')))
            dist = c[1]
            dm = S.meters(dist) if _f(dist) else 0.0
            rs = []
            if not s['scoped'] and dm > th['noscope_ok_m']:
                rs.append('没开镜')
            if not s['still']:
                rs.append('人还在走')
            in_view = C.spots(ctx, S.u, tgt, s['tick'] - int(S.cm['seen_a_back_ticks']),
                              s['tick'] + int(S.cm['seen_a_fwd_ticks'])) is not None
            lat = _lateral_speed(S, tgt, s['tick'], s['src']) if in_view else 0.0
            if lat > th['runner_lat']:
                rs.append('他横着跑')
            # AWP 开镜一枪就能打死：Mirage 上常规的狙击距离不算“太远”（8d VIP 看中远、128 B 小看 B 点），只用 far_m_awp
            far_lim = th['far_m_awp'] if s['wk'] == 'awp' else th['far_m']
            if dm > far_lim:
                kd = C.deaths(ctx, s['round'])
                later = kd[(kd['att'] == S.u) & (kd['vic'] == tgt) & (kd['tick'] > s['tick'])
                           & (kd['tick'] <= s['tick'] + S.ticks(th['group_s']))]
                if not len(later):
                    rs.append('距离很远')      # 同一个人随后在同样远的地方被你狙死了，就不是距离的问题（128 R4）
            eh = C.aim_err(s['src'], s['pitch'], s['yaw'], C.eye(ctx, tgt, s['tick']))[0]
            if s['scoped'] and s['still'] and _f(eh) and _f(dist) and \
                    eh > C.aim_limit(ctx, dist, th['aim_bad_min_deg'], th['aim_bad_half_bodies']):
                rs.append('开镜了但准星差一大截')
            rec['reasons'] = rs
            rec['in_view'] = in_view
            rec['dist_m'] = round(dm, 1)
            rec['place'] = _place(ctx, tgt, s['tick'])
            rec['code'] = code
        else:
            if C.blind_left(ctx, S.u, s['tick']) > th['flash_left_s']:
                continue
            rs = []
            if not s['scoped']:
                rs.append('没开镜')
            if not s['still']:
                rs.append('人还在走')
            rec['reasons'] = rs
        miss.append(rec)
    # 同一回合 15 秒内的空枪合成一条
    groups = []
    for r in miss:
        g = groups[-1] if groups else None
        if g and g[-1]['round'] == r['round'] and r['tick'] - g[-1]['tick'] <= S.ticks(th['group_s']):
            g.append(r)
        else:
            groups.append([r])
    out = []
    for g in groups:
        idn = [r for r in g if r['ident']]
        if not idn:
            continue
        pun = None
        if not any(r['reasons'] for r in idn) and len(g) < 2:
            pun = _punished(S, idn[0])
            if pun is None:
                continue                # 停稳开镜、目标不跑、不远、只空一枪，之后也没站着挨枪：正常失误
        rnd, g0 = g[0]['round'], g[0]['s']
        # 按人合并：对{place}的 {enemy}[{k}枪]（原因）
        per = []
        for r in idn:
            e = next((p for p in per if p['tgt'] == r['tgt']), None)
            if e is None:
                e = dict(tgt=r['tgt'], place=r['place'], k=0, reasons=[])
                per.append(e)
            e['k'] += 1
            for x in r['reasons']:
                if x not in e['reasons']:
                    e['reasons'].append(x)
        parts = []
        for e in per:
            rs = '、'.join(e['reasons']) if e['reasons'] else '停稳开镜了但准星没在人身上'
            if len(g) == 1:
                # 只有一枪：“AWP 对中远的 对手1 开了一枪没中”（8d R8：“这一枪没中：对…（距离很远）”读着不通）
                parts.append(f"对{e['place']}的 {C.name_of(ctx, e['tgt'])}开了一枪没中（{rs}）")
            else:
                parts.append(f"对{e['place']}的 {C.name_of(ctx, e['tgt'])}" + (f"{C.num_cn(e['k'])}枪" if e['k'] >= 2 else '')
                             + f"（{rs}）")
        un = [r for r in g if not r['ident']]
        tail = ''
        if un:
            tail = f"；另外{C.num_cn(len(un))}枪没对着具体的人"
            ur = [r for r in un if r['reasons']]
            if ur:
                rs = []
                for r in ur:
                    for x in r['reasons']:
                        if x not in rs:
                            rs.append(x)
                tail += f"，其中{C.num_cn(len(ur))}枪{'、'.join(rs)}"
        places = {r['s']['place'] for r in g}
        same = len(g) >= 2 and len(places) == 1 and max(
            C.dist2(r['s']['src'], g0['src']) for r in g) <= th['same_spot_u']
        gun = '鸟狙' if g0['wk'] == 'ssg08' else 'AWP'
        lead = (f"{gun}这一段开了{C.num_cn(len(g))}枪都没中：" if len(g) >= 2 else f"{gun}")
        text = (f"第 {C.fmt_sec(g0['t'])} 秒{'起' if len(g) >= 2 else ''}在{C.place_cn(g0['place'])}，{lead}"
                + '；'.join(parts) + tail + ('，都是在同一个位置连着开的' if same else ''))
        if pun is not None:
            text += f"，狙空后你{pun[0]}，马上就被他打中"
        # 你的部分句子太长时用的短句（8d R9：三枪各自的原因列了一长串，把同一回合打得好的那一面挤掉了）
        short = (f"第 {C.fmt_sec(g0['t'])} 秒起在{C.place_cn(g0['place'])}，{gun}连开{C.num_cn(len(g))}枪都没中" if len(g) >= 2 else
                 f"第 {C.fmt_sec(g0['t'])} 秒在{C.place_cn(g0['place'])}，{gun}一枪没中" + (f"，狙空后{pun[0]}" if pun else ''))
        all_rs = [x for r in idn for x in r['reasons']]
        # “还可以”只给一个思路，按主要原因选（8d R9 / R12：一次塞了三条）
        if '没开镜' in all_rs or '人还在走' in all_rs:
            alt = '停稳开镜再打'
        elif '他横着跑' in all_rs:
            alt = '准星先放在他要经过的口子，等人走进来再开'
        elif '距离很远' in all_rs and gun != 'AWP':
            alt = '太远的枪先不开'
        else:
            alt = '一枪不中先收回掩体，换个角度再出'
        alt = '还可以' + alt + '。'
        ud = _user_death(S, rnd)
        death = ud if ud is not None and g0['tick'] <= ud[1] <= g[-1]['tick'] + S.ticks(th['death_link_s']) else None
        facts = dict(weapon=gun, my_place=C.place_cn(g0['place']), n=len(g), n_cn=C.num_cn(len(g)),
                     shots=[dict(t=C.sec(r['t']), enemy=C.name_of(ctx, r['tgt']) if r['tgt'] else None,
                                 reasons=r['reasons'], scoped=bool(r['s']['scoped'])) for r in g],
                     enemies=[C.name_of(ctx, e['tgt']) for e in per], unidentified=len(un), same_spot=bool(same),
                     display='moment', short=short)
        if pun is not None:
            facts.update(punished=pun[0], hurt_t=C.sec(C.t_of(ctx, pun[1], rnd)))
        if death is not None:
            facts['death_t'] = C.sec(death[0])
        # 证据按这一枪实际的情况写（看得见 / 正在交火），不写“记录”“或”这种判法说明（128 R4、8d R8/R9/R12）
        if len(g) >= 2 and len(idn) == 1:
            # 只有一枪是对着人开的：不写“那几枪 / 都”（8d R12 对手2）
            r0 = idn[0]
            st0 = ('他当时在你视野里' if r0.get('in_view') else
                   '他当时正和你交火' if _exchanging(S, r0['tgt'], r0['tick']) else '准星就在他附近')
            ev = f"这几枪都没打中；对着 {C.name_of(ctx, r0['tgt'])} 开的那一枪，" + st0
        elif len(g) >= 2:
            # 按人写各自的情况，不写“有的……有的……”（8d R9）
            # 每个人的情况：在你视野里 > 真的在和你交火（他打中过你 / 看得见你时开过枪）> 只是准星在他附近
            st_of = {}
            for r in idn:
                n_ = C.name_of(ctx, r['tgt'])
                st = 2 if r.get('in_view') else (1 if _exchanging(S, r['tgt'], r['tick']) else 0)
                st_of[n_] = max(st_of.get(n_, 0), st)
            order_n = [C.name_of(ctx, e['tgt']) for e in per if C.name_of(ctx, e['tgt']) in st_of]
            order_n = list(dict.fromkeys(order_n + sorted(st_of)))
            seen_n = [n_ for n_ in order_n if st_of[n_] == 2]
            fight_n = [n_ for n_ in order_n if st_of[n_] == 1]
            aim_n = [n_ for n_ in order_n if st_of[n_] == 0]
            bits = []
            if seen_n:
                bits.append(f"{'、'.join(seen_n)} {'都' if len(seen_n) > 1 else ''}在你视野里")
            if fight_n:
                bits.append(f"{'、'.join(fight_n)} {'都在' if len(fight_n) > 1 else '正'}和你交火")
            if aim_n:
                bits.append(f"打 {'、'.join(aim_n)} 的那{'几' if len(aim_n) > 1 else '一'}枪准星就在{'他们' if len(aim_n) > 1 else '他'}附近")
            how = '，'.join(bits)
            ev = f"这几枪都没打中；对着人开的那几枪，{how}"
        else:
            r0 = idn[0]
            sc = '开镜' if r0['s']['scoped'] else '没开镜'
            ev = (f"这一枪{sc}打 {C.name_of(ctx, r0['tgt'])}，没打中，" +
                  ('当时他就在你视野里' if r0.get('in_view') else
                   '当时你们正在交火' if _exchanging(S, r0['tgt'], r0['tick']) else '准星就在他附近'))
            if pun is not None:
                ev += "；开枪后马上就被他打中，这时你还在原地"
        out.append(_hit(S, 'SHOT-04', '问题', rnd, g0['t'], g0['tick'], '你的责任', facts, text, alt, [], ev,
                        death=death, merge_key=f"snipe:{rnd}:{g0['tick']}"))
    return out


# ================================================================ 好的操作：SHOT-G1 / G2 / G3（按 user_fights 赢下的交火）
def _fights(S):
    """timeline.json 里用户赢下的交火：[(回合, fight dict, 敌人 sid, 击杀 tick)]"""
    out = []
    tl = S.ctx.json('timeline')
    for R in sorted(tl['rounds'], key=lambda r: r['round']):
        for f in R.get('user_fights', []):
            if f.get('result') != '赢' or f.get('end_tick') is None:
                continue
            e = str(f.get('enemy_id') or C.sid_of(S.ctx, f.get('enemy')))
            out.append((int(R['round']), f, e, int(f['end_tick']), R))
    return out


def _fight_window(S, f, e, kt):
    """[min(开打, 这一对 3 秒内第一次互相打中) - 0.5 秒, 击杀]"""
    th = S.th
    h = _pair_hurts(S, S.u, e, kt - S.ticks(th['mutual_hit_s']), kt)
    st = min([int(f['start_tick'])] + [int(x) for x in h['tick']])
    return st - S.ticks(th['pre_s']), kt


def _fight_info(S, rnd, f, e, kt):
    ctx = S.ctx
    w0, w1 = _fight_window(S, f, e, kt)
    sh = [s for s in C.shots(ctx) if w0 <= s['tick'] <= w1 and s['target'] == e]     # 只算对着这个人的枪
    if not sh:
        return None
    hit_i = next((i for i, s in enumerate(sh) if e in s['hit_vics']), None)
    till = sh[:hit_i + 1] if hit_i is not None else sh
    ph = _pair_hurts(S, S.u, e, w0, w1)
    first_hit = None if not len(ph) else ('我' if ph.iloc[0]['att'] == S.u else '敌')
    took = int(ph[ph['att'] == e]['dmg'].sum()) if len(ph) else 0
    d = C.deaths(ctx, rnd)
    krow = d[(d['tick'] == kt) & (d['att'] == S.u) & (d['vic'] == e)]
    thru = bool(krow['thrusmoke'].iloc[0]) if len(krow) and 'thrusmoke' in krow else False
    kdist = C.dist3(C.eye(ctx, S.u, kt), C.eye(ctx, e, kt))
    return dict(w0=w0, w1=w1, shots=sh, hit_i=hit_i, till=till, first_hit=first_hit, took=took, thru=thru,
                kill_dist_m=S.meters(kdist), enemy=C.name_of(ctx, e),
                my_place=C.place_cn(sh[0]['place']), enemy_place=_place(ctx, e, sh[0]['tick']))


def _after_kill_death(S, rnd, kt):
    """击杀后 die_after_s 秒内用户也阵亡：(秒差, 凶手名) 或 None"""
    ud = _user_death(S, rnd)
    if ud is None or not (kt <= ud[1] <= kt + S.ticks(S.th['die_after_s'])):
        return None
    return (ud[1] - kt) / S.tr, C.name_of(S.ctx, ud[2]) if ud[2] else None


def _econ_tag(S, rnd):
    b = C.buy(S.ctx, rnd, 'them')
    return b if b in S.th['econ_tag_buys'] else None


def _good_g1_g2_g3(S):
    ctx, th = S.ctx, S.th
    out = []
    econ_said = set()                                   # G2 经济标签一回合只说一次
    for rnd, f, e, kt, R in _fights(S):
        fi = _fight_info(S, rnd, f, e, kt)
        if fi is None:
            continue
        sh, till = fi['shots'], fi['till']
        s0 = sh[0]
        side = C.user_side(ctx, rnd)
        # 第 35 条：你架着、他看见你那一刻准星离你很远 = 站的位置他没预瞄到
        Dg = DU.signals(S, rnd, e, shots=till)
        off_angle = bool(Dg.get('me_hold') and _f(Dg.get('his_err_see')) and Dg['his_err_see'] >= th['duel_offangle_deg'])
        oa = '，站的位置他没预瞄到' if off_angle else ''
        mk = f"kill:{rnd}:{kt}"
        kill_t = C.t_of(ctx, kt, rnd)
        base = dict(enemy=fi['enemy'], my_place=fi['my_place'], enemy_place=fi['enemy_place'], kill_tick=kt,
                    kill_t=C.sec(kill_t), first_hit=fi['first_hit'], weapon=s0['weapon'], display='moment')
        # ---------------- SHOT-G1 停稳先手拿下（非狙）
        if not s0['sniper'] and fi['hit_i'] is not None:
            spds = [s['spd'] for s in till if _f(s['spd'])]
            lim0 = C.still_limit(ctx, s0['wk'], s0['scoped'])
            all_still = s0['still'] and till[-1]['still'] and spds and float(np.median(spds)) <= lim0
            if all_still:
                k1 = fi['hit_i'] + 1
                t = C.fmt_sec(s0['t'])
                # “先被打中后站住反杀”：他打中你和你开枪得是同一段连续的对枪；中间隔了好几秒（你先退出视线、再回来打）
                # 就不是站住反杀（128 R24：87 秒被打中退走，90 秒才回来打死他）
                ph = _pair_hurts(S, S.u, e, fi['w0'], fi['w1'])
                eh = ph[ph['att'] == e]
                counter = (fi['first_hit'] == '敌' and len(eh)
                           and s0['tick'] - int(eh['tick'].min()) <= S.ticks(th['counter_gap_s']))
                if counter:
                    var = '反杀'
                    txt = f"第 {t} 秒被{fi['enemy_place']}的 {fi['enemy']} 先打中，你没乱动，站住把准星修上去反杀"
                elif k1 >= th['late_k']:
                    var = f'第{k1}枪才中'
                    txt = (f"第 {t} 秒在{fi['my_place']}对{fi['enemy_place']}的 {fi['enemy']}，停稳再开枪，"
                           f"第{ORD.get(k1, str(k1))}枪才打中，拿下击杀")
                elif k1 == 1:
                    var = '首枪中'
                    txt = (f"第 {t} 秒在{fi['my_place']}，{s0['weapon']}停稳后第一枪就打中，先手打死"
                           f"{fi['enemy_place']}的 {fi['enemy']}")
                else:
                    var = '首枪空了不乱动'
                    txt = (f"第 {t} 秒在{fi['my_place']}，第一枪没中但人没乱动，停着修准星后打死"
                           f"{fi['enemy_place']}的 {fi['enemy']}")
                # 附加：对方跑着撞进准星 / 脚步声 / 隔烟
                en_spd, my_spd = f.get('enemy_speed') or 0, f.get('my_speed') or 0
                runs = en_spd >= th['enemy_run'] and my_spd <= th['me_slow']
                in_view = C.spots(ctx, S.u, e, s0['tick'] - S.ticks(th['pre_s']), s0['tick']) is not None
                extra = ''
                if fi['thru']:
                    extra = ('；他是跑着过来的，' if en_spd >= th['enemy_run'] else '；') + \
                            ('你隔着烟先打中' if fi['first_hit'] == '我' else '隔着烟拿下')
                elif runs and in_view:
                    extra = '；对方是跑着出来、撞进你准星的'
                elif en_spd >= th['enemy_run'] and not in_view:
                    extra = '；他是跑着过来的，你停着等他出来'      # 原来“他跑过来有脚步声”没说明白在说什么（128 R11/R24）
                # “下包后站定守包”只在包是你下的、下包后到这一杀你基本没挪窝时写；别人下的包或你走开了写“包下了之后”
                plant = R.get('plant')
                after_plant = side == 'T' and bool(plant) and plant.get('tick') is not None and kt > int(plant['tick'])
                hold = False
                if after_plant:
                    by_me = bool(plant.get('by_user')) or C.sid_of(ctx, plant.get('planter')) == S.u
                    moved = C.path_len(ctx, S.u, int(plant['tick']), kt)
                    hold = by_me and moved <= th['hold_plant_move']
                    rest = extra[1:] if extra.startswith('；') else extra
                    if hold:
                        extra = '；你下包后站定守包' + ('，' + rest if rest else '')
                    else:
                        extra = '；包下了之后，' + rest if rest else '；当时包已经下了'
                tag = _econ_tag(S, rnd)
                if tag:
                    extra += f"（对面是 {tag}）" if tag == 'eco' else f"（对面是{tag}）"
                akd = _after_kill_death(S, rnd, kt)
                if akd and akd[1]:
                    # 复核修正（第 11 条）：打死他后没松开、一口气扫到下一个人身上（SHOT-06 续扫）时，照实写续扫
                    dx = DU.death_duel(S, rnd, _user_death(S, rnd))
                    if dx is not None and dx[0].get('transfer_from') == fi['enemy']:
                        extra += f"；打死他后没松开，一口气扫到 {akd[1]} 身上，最后被他打死"
                    else:
                        extra += f"；之后你也被 {akd[1]} 打死"
                # 为什么好：只说这一枪本身好在哪（按分支）；“还可以”只放和这一枪有关的改进，放 alt
                if fi['first_hit'] == '敌' and not counter:
                    var = '首枪中' if k1 == 1 else '首枪空了不乱动'
                    txt = ((f"第 {t} 秒在{fi['my_place']}，{s0['weapon']}停稳后第一枪就打中，打死"
                            f"{fi['enemy_place']}的 {fi['enemy']}") if k1 == 1 else
                           (f"第 {t} 秒在{fi['my_place']}，第一枪没中但人没乱动，停着修准星后打死"
                            f"{fi['enemy_place']}的 {fi['enemy']}"))
                # 句子说的是“打死”：秒数写击杀那一刻，和主要原因、时间线同一个秒（128 R9：23.6 / 23.9）
                if var != '反杀':
                    txt = txt.replace(f"第 {t} 秒", f"第 {C.fmt_sec(kill_t)} 秒", 1)
                    g1_t, g1_tick = kill_t, kt
                else:
                    g1_t, g1_tick = s0['t'], s0['tick']
                why_good = {'反杀': '被先打中后没乱跑，站住把准星修上去，反过来把他打死',
                            '首枪中': '停稳后第一枪就打中，他没来得及还手',
                            '首枪空了不乱动': '第一枪没中也没乱跑，停着修准星把人拿下'}.get(
                    var, '一直停稳修准星、没乱动，最后把人拿下')
                # 并进同一次击杀的别的夸奖时只用这半句（主句已经写了打死谁）
                manner = {'反杀': '被先打中后站住把准星修上去',
                          '首枪中': '停稳后第一枪就打中',
                          '首枪空了不乱动': '第一枪没中但人没乱动，停着修准星'}.get(
                    var, f"停稳再开枪，第{ORD.get(k1, str(k1))}枪才打中")
                alt = ''
                kb = []
                if k1 >= th['late_k'] and fi['first_hit'] != '敌':
                    alt = '还可以出去前就把准星放在他所在位置的头线'
                    if side == 'T' and fi['my_place'] in ('中路', '中远') and fi['enemy_place'] == 'VIP':
                        alt += f"，比如{KB_TIP['aim_vip']}"
                    alt += '。'
                facts = dict(base, variant=var, k_hit=k1, econ_tag=tag, thru_smoke=fi['thru'], hold_plant=bool(hold),
                             after_plant=bool(after_plant), why_good=why_good, manner=manner,
                             enemy_runs=bool(runs and in_view), off_angle=off_angle)
                if off_angle:
                    # 句尾的经济标签、“几秒后你也被打死”之前插入（第 35 条从句）
                    txt = txt + oa
                if akd:
                    facts.update(died_after_s=C.sec(akd[0]), killer=akd[1])
                ev = '第一枪和打中的那一枪都是停稳开的，到打中为止没有乱动'
                out.append(_hit(S, 'SHOT-G1', '好', rnd, g1_t, g1_tick, '—', facts, txt + extra, alt, kb, ev,
                                merge_key=mk))
        # ---------------- SHOT-G2 AWP / 鸟狙停稳开镜先手
        if s0['sniper'] or (fi['hit_i'] is not None and sh[fi['hit_i']]['sniper']):
            if fi['hit_i'] is not None:
                ks = sh[fi['hit_i']]
                if ks['sniper'] and ks['scoped'] and ks['still']:
                    gun = '鸟狙' if ks['wk'] == 'ssg08' else 'AWP'
                    t = C.fmt_sec(ks['t'])
                    behind = fi['kill_dist_m'] >= th['behind_m'] and fi['took'] == 0
                    if fi['first_hit'] == '敌':
                        var = '先挨枪反杀'
                        txt = (f"第 {t} 秒在{fi['my_place']}先挨了{fi['enemy_place']}的 {fi['enemy']} 一枪，"
                               f"你停住开镜，一枪把他打死")
                    elif fi['hit_i'] > 0:
                        var = '空枪后没走开'
                        # 不夸“空了没走开”（和缺点“狙空枪：一枪不中先收回掩体”并排读着矛盾，8d R9），只写第二枪带走
                        txt = f"第 {t} 秒在{fi['my_place']}，{gun}第一枪空了，{fi['enemy']} 再露头时停稳开镜一枪带走"
                    else:
                        var = '停稳开镜先手'
                        txt = (f"第 {t} 秒在{fi['my_place']}，{gun}停稳开镜，先手打死"
                               f"{_place(ctx, e, ks['tick'])}的 {fi['enemy']}"
                               + ('，你站得远、一滴血没掉' if behind else ''))
                    tag = _econ_tag(S, rnd)
                    if tag and behind and rnd not in econ_said:
                        econ_said.add(rnd)
                        txt += ('；对面是 eco，你站远用狙架，他们的手枪够不着你' if tag == 'eco'
                                else f"；对面是{tag}，你没贴上去对枪，站远架住")
                    alt, kb = '', []
                    us_after = C.alive_counts(ctx, kt + 1, lag_s=0)
                    if us_after and us_after[1] > 0:
                        alt = '还可以拿到人后挪个位置，第二时间再回来从斜边架。'
                        if side == 'CT' and ks['place'] in ('Palace', 'PalaceInterior'):
                            kb = []
                    why_good = {'先挨枪反杀': '挨了一枪没乱跑，停住开镜一枪把他带走',
                                '空枪后没走开': '第二枪停稳开镜打中，没给他第二次机会'}.get(
                        var, '停稳开镜再开枪，先手拿下')
                    manner = {'先挨枪反杀': '先挨了一枪后停住开镜反杀',
                              '空枪后没走开': f'{gun}第二枪停稳开镜打中'}.get(var, f'{gun}停稳开镜先手')
                    if off_angle:
                        txt = txt + oa
                    facts = dict(base, variant=var, weapon=gun, behind=bool(behind), econ_tag=tag, off_angle=off_angle,
                                 kill_dist_m=round(fi['kill_dist_m'], 1), took=fi['took'], shots_before=fi['hit_i'],
                                 alt_if_no_posg2=bool(alt), why_good=why_good, manner=manner)
                    ev = '打死他的那一枪开了镜、人是停稳的'
                    out.append(_hit(S, 'SHOT-G2', '好', rnd, ks['t'], ks['tick'], '—', facts, txt, alt, kb, ev,
                                    merge_key=mk))
        # ---------------- SHOT-G3 打没看你的人
        ft = s0['tick']
        if C.spots(ctx, S.u, e, ft - S.ticks(th['g3_spot_back_s']), ft + int(th['g3_spot_fwd_ticks'])) is not None:
            off = C.facing_deg(ctx, e, ft, C.eye(ctx, S.u, ft))
            ew = C.weapon_at(ctx, e, ft)
            wk = C.wkey(ew) or ''
            util = next((k for k in UTIL_KEYS if wk.startswith(k)), None)
            facing = '背对着你' if off >= th['back_deg'] else ('侧着身、没在看你这边' if off >= th['side_deg'] else None)
            if facing or util:
                d = C.deaths(ctx, rnd)
                st = d[(d['att'] == e) & (d['tick'] < ft) & (d['tick'] >= ft - S.ticks(th['streak_s']))]
                streak = int(len(st))
                who = (f"刚连杀 {streak} 人的 " if streak >= th['streak_n'] else '') + fi['enemy'] + ' '
                txt = f"第 {C.fmt_sec(s0['t'])} 秒，{who}"
                if facing:
                    txt += facing
                if util:
                    txt += ('，' if facing else '') + f"手里{'还' if facing else ''}拿着{C.weapon_cn(util)}"
                manner = txt[len(f"第 {C.fmt_sec(s0['t'])} 秒，"):] + '，没有和他正面对枪'
                txt += f"，你从{fi['my_place']}先开枪拿下，没有和他正面对枪"
                alt, kb = '', []
                if side == 'T' and s0['place'] in A_AREA:
                    alt = '还可以等对面的狙往别处开枪那一下再拉。'
                    kb = []
                facts = dict(base, facing=facing, util=C.weapon_cn(util) if util else None, streak=streak,
                             manner=manner, why_good='挑他没看你的那一刻先开枪，不用和他正面对枪')
                ev = '你开第一枪时他在你的视野里，他的视线没对着你' + ('，手里拿的不是枪' if util else '')
                out.append(_hit(S, 'SHOT-G3', '好', rnd, s0['t'], s0['tick'], '—', facts, txt, alt, kb, ev,
                                merge_key=mk))
    return out


# ================================================================ SHOT-G4 人数劣势里拿下击杀
def _good_g4(S):
    ctx, th = S.ctx, S.th
    out = []
    tl = ctx.json('timeline')
    rmeta = {int(r['round']): r for r in tl['rounds']}
    d = C.deaths(ctx)
    d = d[(d['att'] == S.u) & (d['round'] > 0)]
    for row in d.itertuples():
        rnd, kt, v = int(row.round), int(row.tick), row.vic
        if not C.is_enemy(ctx, S.u, v, rnd):
            continue
        cnt = C.alive_counts(ctx, kt)
        if cnt is None:
            continue
        us, them = cnt
        if not (us < them or (us == 1 and them >= 1)):
            continue
        R = rmeta.get(rnd, {})
        kill_t = C.t_of(ctx, kt, rnd)
        vname = C.name_of(ctx, v)
        f = next((x for x in R.get('user_fights', []) if str(x.get('enemy_id')) == v and x.get('end_tick') is not None
                  and abs(int(x['end_tick']) - kt) <= S.tr), None)
        dd = C.deaths(ctx, rnd)
        st = dd[(dd['att'] == v) & (dd['tick'] < kt) & (dd['tick'] >= kt - S.ticks(th['streak_s']))]
        streak = int(len(st))
        vw = C.wkey(C.weapon_at(ctx, v, kt - 8))
        awp = vw == 'awp' or bool((dd[(dd['att'] == v) & (dd['tick'] < kt)]['wk'] == 'awp').any())
        ud = _user_death(S, rnd)
        safe = (ud is None or ud[1] > kt + S.ticks(th['safe_after_s'])) and \
            C.round_info(ctx, rnd)['end'] > kt + S.ticks(th['safe_after_s'])
        active = f is not None and (f.get('my_speed') or 0) > th['active_speed'] and \
            f.get('engage') in th['active_engage'] and safe
        place = _place(ctx, v, kt)
        who = (f"刚连杀 {streak} 人的" if streak >= th['streak_n'] else '') + (' AWP' if awp else '') + ' ' + vname
        anti_eco = C.buy(ctx, rnd, 'them') == 'eco' and C.buy(ctx, rnd, 'us') not in ('eco', None)
        t = C.fmt_sec(kill_t)
        won = R.get('result') == '赢'
        if us == 1 and them == 1:
            var = '1v1 收尾'
            txt = f"第 {t} 秒 1v1 打死最后一个人 {vname}" + ('，残局赢下' if won else '')
        elif active:
            var = '主动'
            txt = f"第 {t} 秒 {C.alive_str(us, them)} 时没躲，主动去找{place}的{who}，把他打死，扳成 {C.alive_str(us, them - 1)}"
        else:
            var = '中性'
            txt = f"第 {t} 秒 {C.alive_str(us, them)} 时打死{place}的{who}，扳成 {C.alive_str(us, them - 1)}"
        if anti_eco:
            txt += '（对面是 eco，装备上你占优）'
        side = C.user_side(ctx, rnd)
        plant = R.get('plant')
        planted = plant is not None and plant.get('tick') is not None and kt > int(plant['tick'])
        alt, kb = '', []
        kplace = C.place_at(ctx, S.u, kt)
        # “还可以用火和烟拖时间”只在你身上真有这些道具、又不是夸你主动去找人的时候配（635 R4：eco 只有沙鹰）
        held = set(UT._nades_at(ctx, S.u, kt))
        # 拿下之后几秒内你自己就扔了的道具不再写进“还可以”：那就是你做了的事（8d R9：37.7 秒拿人，40.6 秒就扔了火）
        thrown_after = {x['n'] for x in UT._user_util(ctx, rnd)
                        if kt < x['tick'] <= kt + int(S.th['g4_util_after_s'] * S.tr)}
        held -= thrown_after
        # 这一杀本身是夸奖（人少时拿下一个）：“还可以”只写拿下之后的下一步，不写“先拖时间别打”——
        # 那等于说这一杀不该打，和“为什么好”读着矛盾（8d R3 38.0、R9 37.7）
        # 审核单第 26 条：拖时间要写具体报点，没有报点不写——往他来的那个报点扔（完整性复核：原来没写往哪扔）
        if var != '主动' and us >= 2 and side == 'CT' and not planted and held & {'火', '烟'} and place and place != '某处':
            alt = (f'还可以拿下他之后先往{place}扔' + '、'.join(n for n in ('烟', '火') if n in held)
                   + '拖住剩下的人，等队友赶过来再一起打。')
            kb = []
        elif var != '主动' and us >= 2 and side == 'T' and planted and held & {'火', '闪'}:
            alt = '还可以拿下他之后先把' + '和'.join(n for n in ('火', '闪') if n in held) + '用掉，再和队友一起拉。'
            kb = []
        why_good = {'1v1 收尾': '残局一对一把最后一个人拿下',
                    '主动': '人少时没有等着被找，主动拿下一个，把人数扳回来'}.get(var, '人少时拿下一个，把人数扳回来')
        facts = dict(victim=vname, place=place, us=us, them=them, us_after=us, them_after=them - 1,
                     alive=C.alive_str(us, them), alive_after=C.alive_str(us, them - 1), variant=var, enemy_awp=awp,
                     streak=streak, anti_eco=anti_eco, kill_tick=kt, display='moment', held=sorted(held),
                     why_good=why_good, manner='' if var == '1v1 收尾' else f'扳成 {C.alive_str(us, them - 1)}',
                     text_plain=(f"第 {t} 秒 {C.alive_str(us, them)} 时打死{place}的{who}，扳成 {C.alive_str(us, them - 1)}"
                                 + ('（对面是 eco，装备上你占优）' if anti_eco else '')))
        ev = '击杀前我方活着的人比对面少' if us < them else '击杀前我方只剩你'
        out.append(_hit(S, 'SHOT-G4', '好', rnd, kill_t, kt, '—', facts, txt, alt, kb, ev, merge_key=f"kill:{rnd}:{kt}"))
    return out


# ================================================================ 每一次阵亡的对枪动作判定（拍板 7、8；审核单第 2 条）
NR_CLAUSE = '挨了第一下以后没退出他的视线'


def _duel_hits(S, hits):
    """用户每一次被枪打死：按手册 1.13 判定表给一个对枪动作结论（duel.verdict）。
    - 结论挂到这次死已有的开枪类命中上（facts.duel，同一个 dict）；
    - 另外每次死一定出一条 SHOT-06（facts.duel_carrier=True，display='summary'：合并层接上之前不成时刻），
      text 就是判定整句；facts.duel.covered_by = 已经把这次死写成时刻的开枪类规则（没有为 None）。
    - 第 18 条：判定里“挨了第一下以后没退出他的视线”并进已有命中的句子。"""
    ctx, th = S.ctx, S.th
    out = []
    for ri in C.rounds(ctx):
        rnd = ri['n']
        ud = _user_death(S, rnd)
        if ud is None:
            continue
        x = DU.death_duel(S, rnd, ud)
        if x is None:
            continue
        D, b = x
        V = DU.verdict(S, D)
        V['text'] = C.tidy(V['text'], S.names)
        V['alt'] = C.tidy(V['alt'], S.names)
        dsec = C.sec(ud[0])
        kname = C.name_of(ctx, ud[2])
        linked = [h for h in hits if h['round'] == rnd and h['rule'] in SHOT_PROB and h.get('death_t') == dsec]
        # 写成时刻的、对着凶手的那一条（51920faa R22：38.1 秒打 对手2（第5场） 的那条不是这次对枪）
        shown = [h for h in linked if h['facts'].get('display') in ('moment', 'note')
                 and (h['facts'].get('enemy') == kname or kname in (h['facts'].get('enemies') or []))]
        shown.sort(key=lambda h: (h['facts'].get('display') != 'moment', h['rule']))
        V['covered_by'] = shown[0]['rule'] if shown else None
        if shown and V['ok'] is True and blame_mine(shown[0]['blame']):
            # 已经有一条开枪类问题把这次死写成时刻（SHOT-02 头线“准星比头低”、SHOT-05“没停住”等）：
            # 判定跟着那一条走，不和它并排写“动作对”（用户规则：不并排写方向相反的结论）
            h0 = shown[0]
            V.update(ok=False, kind='问题', blame=h0['blame'], key='as_' + h0['rule'], step=STEP_OF.get(h0['rule']),
                     text=h0['text'], clause=h0['facts'].get('clause') or '', alt=h0['alt'])
        V['death_t'] = dsec
        V['killer'] = kname
        V['burst_tick'] = int(b['tick']) if b is not None else None
        for h in linked:
            h['facts']['duel'] = V
        # 第 18 条：句子里还没写“没退出视线”的，补进去（只补写成时刻的那一条）
        if V['no_retreat'] and shown:
            h = shown[0]
            cl = NR_CLAUSE + ('，继续站着打' if D.get('kept_firing') else '')
            if NR_CLAUSE not in h['text']:
                tx = h['text']
                # “你一枪没开就被他打死”里不能直接插（d5a R17 拼成“一枪没开就挨了第一下……”）
                cl_also = cl.replace(NR_CLAUSE, NR_CLAUSE.replace('以后', '以后也'), 1)
                tx2 = re.sub(r'一枪没开就被他(一枪)?打死', f'一枪没开，{cl_also}，最后被他打死', tx, count=1)
                if tx2 != tx:
                    tx = tx2
                else:
                    for key in ('最后被他打死', '被他打死'):
                        if key in tx:
                            tx = tx.replace(key, f"{cl}，{key}", 1)
                            break
                    else:
                        tx = f"{tx}；{cl}"
                h['text'] = C.tidy(tx, S.names)
                h['facts']['no_retreat'] = True
        if str(V.get('key')).startswith('as_'):
            V['text'] = shown[0]['text']
        kind = '问题' if V['ok'] is False else '判定'
        blame = V['blame'] if V['ok'] is False else '—'
        facts = dict(duel=V, duel_carrier=True, display='summary', enemy=V['enemy'], killer=V['killer'], weapon=D.get('weapon'),
                     my_place=V['my_place'], enemy_place=V['enemy_place'], death_t=dsec, verdict=V['key'],
                     ok=V['ok'], step=V['step'], clause=V['clause'])
        ev = {True: '这次对枪的动作（急停、准星位置、开枪时机）都没有问题', False: '这次对枪的动作有一步没做好',
              None: '这次死不按对枪动作判'}[V['ok']]
        # 完整性复核：判定自己成时刻时证据只写“有一步没做好”太空（bd R29），按错在哪一步写
        if V['ok'] is False:
            ev = DUEL_EV.get(str(V.get('key')), ev)
        tick = b['tick'] if b is not None else D['contact']
        out.append(_hit(S, 'SHOT-06', kind, rnd, D['t'] if b is not None else D['t_contact'], tick, blame, facts,
                        V['text'], V['alt'], [], ev, death=ud, merge_key=f"duel:{rnd}:{ud[1]}"))
    return out


NADE_END_CN = {'hegrenade': '雷', 'inferno': '火', 'molotov': '火', 'incgrenade': '火'}
# SHOT-06 动作有错时的证据（按判定表的 key；不写数字）
DUEL_EV = {'peek_into_holder': '他打中你那一刻你人还在动，他架着没动',
           'aim_off': '他出现那一刻你的准星离他还差一大截',
           'no_stop': '第一枪开的时候人还没停稳，这一枪没打中',
           'rush': '开第一枪时人停住了，但准星还在往他身上拉',
           'long_spray': '这一串一口气扫到底，几乎没中',
           'fast_tap': '前两枪隔得太近，上一枪的晃动还没回稳',
           'no_retreat': '挨了第一下以后你一直在他的视野里',
           'awp': 'AWP 第一枪没开镜或人还在走',
           'awp_run': 'AWP 跑着拉出去，停下开镜前他已经打中了你'}
SHOT_PROB = ('SHOT-01', 'SHOT-02', 'SHOT-03', 'SHOT-04', 'SHOT-05')
STEP_OF = {'SHOT-05': 1, 'SHOT-01': 2, 'SHOT-02': 3, 'SHOT-03': 7, 'SHOT-04': 10}     # 判定表的行（duel.verdict）


def blame_mine(b):
    return b in ('你的责任', '小问题', '你的责任 + 队友或团队')


# ================================================================ 入口
def run(ctx):
    """开枪与准星类全部命中，按 (回合, tick, 规则) 排好"""
    S = _S(ctx)
    hits = []
    hits += _burst_hits(S)
    hits += _shot04(S)
    hits += _shot05_noshot(S)
    hits += _duel_hits(S, hits)
    hits += _good_g1_g2_g3(S)
    hits += _good_g4(S)
    hits.sort(key=lambda h: (h['round'], h['tick'] if h['tick'] is not None else -1, h['rule'], h['text']))
    return hits
