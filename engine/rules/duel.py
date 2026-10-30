# -*- coding: utf-8 -*-
"""对枪动作分析（手册 1.13 判定表；用户拍板 7、8：“枪法失误就是枪法失误，要分析动作是不是对枪的正确动作”）。

只给 shot.py 用（shot.py 是唯一 import 它的模块）。两层：

  signals(S, rnd, tgt, shots=None, anchor=None, death=None) -> dict
      一次对枪的逐 tick 信号（3D + spotted）：接触时刻、拉枪方式、看得见你的敌人、第一枪急停 / 准星转速 / 离身体、
      看见那一刻准星离头、连发分口气、双方命中部位和伤害（隔着东西）、挨了第一下有没有退出视线、往下压过了、
      对面看见你那一刻准星离你多远（站位有没有被预瞄）。
      shots = 用户这一串（common.shots 的 dict 列表）；没开枪就死时 shots=None、anchor = 他第一次打中你的 tick。

  verdict(S, D) -> dict
      按判定表顺序取第一条：
        0 被闪 / 隔烟（不判）、开打时两个以上敌人看得见你（归站位，不判动作）
        1 没开枪就死（拉出去撞上架着的人 / 准星没放好 / 他先开的枪 / 没看见他）
        2 第一枪没停稳、没中、10 米外
        3 准星没放在他出来的位置（前 0.4 秒开过枪、续扫的不判）
        4 停住了但准星还没拉到他身上就开了（抢枪）
        5 先打中但打在身上，他打的是头
        7 20 米外一口气扫到底几乎没中
        8 手枪点得太快
        9 挨了第一下以后没退出他的视线
        10 AWP 没开镜 / 没停稳
        11 动作对，是枪法
      另外的从句：往下压过了（6）、他一直瞄着你出来的位置（第 35 条）、部位 / 隔着东西（第 6 条）。

句子不写数字（秒数除外）、不写速度、角度、伤害；阈值只读 ctx.th['shot']（duel_* 键）。

shot.py 输出（给合并层）：
  SHOT-06  用户每一次被枪打死一条（凶手是敌人、凶器是枪）。kind = 问题（ok False）/ 判定（ok True / None），
           blame = 判定的 blame（ok 不是 False 时为 —），death_t / death_of 填好，merge_key = duel:<回合>:<阵亡 tick>，
           facts.display = 'summary'（现在的合并层不会把它写成时刻），facts.duel_carrier = True。
  facts.duel（SHOT-06 和这次死已有的 SHOT-01..05 命中共用同一个 dict）：
    step / key     判定表的行和名字：flashed smoke two_seeing | peek_into_holder unseen aim_off beaten |
                   no_stop aim_off transfer rush body_vs_head long_spray fast_tap no_retreat awp awp_run moving_hit ok |
                   as_SHOT-0x（这次死已有一条开枪类问题写成时刻，判定跟着它走）
    ok             True = 动作对（是枪法）；False = 动作有错；None = 不按动作判（被闪、隔烟、两个人看得见你、没看见他、续扫）
    kind / blame   同 SHOT-06
    text / clause  带秒数的整句 / 不带秒数的半句；alt = “还可以……”（可能为空）
    clauses        附带从句（“他一直瞄着你出来的位置”“挨了第一下以后没退出他的视线……”），已经写进 text
    covered_by     已经把这次死写成时刻（display moment / note）、对着凶手的那条开枪类规则；没有为 None
    parts          {mine: [你打中他的部位], his: [他打中你的部位], mine_low: [隔着东西]}
    seeing         开打时看得见你的敌人名字（two_seeing 时 >= 2 个）
    peek           me_peek（你拉他架）/ he_peek（他拉你架）/ both_move / both_hold
    no_retreat / overpull / prefired / killed / enemy / killer / my_place / enemy_place / t / death_t / burst_tick
"""
import math

import numpy as np

from engine import common as C

ORD = {1: '一', 2: '二', 3: '三'}
HAND_CN = {'smokegrenade': '烟', 'flashbang': '闪', 'hegrenade': '雷', 'molotov': '火', 'incgrenade': '火',
           'decoy': '诱饵弹', 'knife': '刀', 'c4': '包', 'taser': '电击枪'}


def _f(x):
    return x is not None and isinstance(x, (int, float, np.floating, np.integer)) and math.isfinite(float(x))


def _tk(S, s):
    return int(round(float(s) * S.tr))


# ================================================================ 帧上的小工具
def seg_start(S, viewer, target, tick, look_s, fwd_ticks=0):
    """viewer 看见 target 的 spotted 连续段（中断 <= duel_spot_gap_ticks 仍算连续），段尾落在 [tick - tail, tick + fwd] 内；
    返回段起点 tick（往回最多 look_s 秒），没有返回 None。两人都活着的帧才算"""
    ctx, th = S.ctx, S.th
    d = C._data(ctx)
    viewer, target = str(viewer), str(target)
    if viewer not in d.col or target not in d.col:
        return None
    gap, tail = int(th['duel_spot_gap_ticks']), int(th['duel_spot_tail_ticks'])
    rnd = C.round_of(ctx, tick)
    hi = int(tick) + int(fwd_ticks)
    for s in (viewer, target):
        dt = C.death_tick(ctx, s, rnd) if rnd else None
        if dt is not None:
            hi = min(hi, dt - 1)
    lo = int(tick) - _tk(S, look_s)
    i0, i1 = d.frange(lo, hi)
    if i1 <= i0:
        return None
    idx = np.flatnonzero((d.spot[i0:i1, d.col[target]] & d.bit[viewer]) != 0)
    if not len(idx):
        return None
    k = len(idx) - 1
    if int(d.ticks[i0 + idx[k]]) < int(tick) - tail:
        return None
    while k > 0 and idx[k] - idx[k - 1] <= gap:
        k -= 1
    return int(d.ticks[i0 + idx[k]])


def _speed_max(S, sid, t0, t1, step=4):
    """[t0, t1] 内帧差速度的最大值（瞬移帧跳过），没有返回 None"""
    vs = [C.move_speed(S.ctx, sid, tk) for tk in range(int(t0), int(t1) + 1, step)]
    vs = [v for v in vs if _f(v)]
    return max(vs) if vs else None


def _duck(S, sid, tick):
    d = C._data(S.ctx)
    return float(d.duck[d.fi(tick), d.col[str(sid)]])


def _aimvel(S, sid, tick):
    """开枪那一帧准星转速（度/秒）：这一帧和前 duel_aimvel_ticks 帧的视线夹角"""
    w = int(S.th['duel_aimvel_ticks'])
    a = C.view_vec(*C.view(S.ctx, sid, tick))
    b = C.view_vec(*C.view(S.ctx, sid, tick - w))
    ang = math.degrees(math.acos(float(np.clip(np.dot(a, b), -1.0, 1.0))))
    return ang / (w / S.tr)


def _seeing(S, me, rnd, tick, ref=None):
    """开打时看得见 me 的活着的敌人（按 steamid 排）：接触 tick 前后 duel_seeing_ticks 内 spotted 了 me 的，
    并上你第一枪（没开枪时 = 他第一次打中你）前 duel_seeing_pre_s 秒到那一刻 spotted 了你的、接触到那一刻打中过你的
    （手册 POS-A：从第一枪前 0.25 秒看到第一枪；bd R24 66.5 对手1（第4场） 在接触后、第一枪前打中你）"""
    ctx, th = S.ctx, S.th
    w = int(th['duel_seeing_ticks'])
    ref = int(tick) if ref is None else int(ref)
    lo2 = ref - _tk(S, th['duel_seeing_pre_s'])
    h = C.hurts(ctx, rnd)
    hv = h[(h['vic'] == me) & (h['dmg'] > 0) & (h['tick'] >= min(int(tick), lo2)) & (h['tick'] <= ref)]
    hit_by = set(hv['att'].astype(str))
    out = []
    for e in C.enemies(ctx, rnd, me):
        at_contact = C.alive(ctx, e, int(tick), rnd) and C.spots(ctx, e, me, tick - w, tick + w) is not None
        # 第一枪前那一段：要到那一刻还活着（8d R13 47.4：刚被你打死的 对手5 不算“开打时也打得到你”）
        at_ref = C.alive(ctx, e, ref, rnd) and (C.spots(ctx, e, me, lo2, ref) is not None or str(e) in hit_by)
        if at_contact or at_ref:
            out.append(e)
    return out


def _dmg_median(S):
    """本场 (武器, 部位) 打有甲的人、没打死的那一下的伤害中位数（样本 >= duel_lowdmg_min_n）：隔着东西的参照"""
    m = getattr(S, '_duel_dmg_med', None)
    if m is not None:
        return m
    h = C.hurts(S.ctx)
    h = h[(h['dmg'] > 0) & (h['health'] > 0) & (h['dmg_armor'] > 0)]
    m = {}
    for (wk, hg), g in h.groupby(['wk', 'hitgroup'], sort=True):
        if len(g) >= int(S.th['duel_lowdmg_min_n']):
            m[(wk, hg)] = float(np.median(g['dmg'].to_numpy(float)))
    S._duel_dmg_med = m
    return m


def _low_dmg(S, wk, hg, dmg, health_after, armored):
    """这一下伤害明显低于本场同枪同部位的中位数 = 隔着东西打的（打死的那一下伤害被血量截断，不算）"""
    if health_after <= 0 or not armored:
        return False
    med = _dmg_median(S).get((wk, hg))
    return med is not None and dmg < med * float(S.th['duel_lowdmg_frac'])


def _broke_los(S, me, en, t0, t1):
    """[t0, t1] 内有没有一段 duel_los_ticks 帧两人谁都看不见谁（退出视线）：返回第一帧 tick 或 None"""
    ctx = S.ctx
    d = C._data(ctx)
    w = int(S.th['duel_los_ticks'])
    cm, ce = d.col[str(me)], d.col[str(en)]
    bm, be = d.bit[str(me)], d.bit[str(en)]
    i0, i1 = d.frange(t0, t1)
    for i in range(i0, i1, 2):
        j = min(i + w, len(d.ticks))
        if not (d.falive[i:j, cm].all() and d.falive[i:j, ce].all()):
            return None
        if not ((d.spot[i:j, cm] & be) != 0).any() and not ((d.spot[i:j, ce] & bm) != 0).any():
            return int(d.ticks[i])
    return None


# ================================================================ 一次对枪的信号
def signals(S, rnd, tgt, shots=None, anchor=None, death=None):
    ctx, th = S.ctx, S.th
    me = S.u
    tgt = str(tgt)
    shots = list(shots or [])
    if shots:
        # 这一串前面紧挨着、没认出目标的枪（他刚露头时准星还离得远，C-TARGET 认不出）也是这次对枪的第一枪（a32 R5 24.9）
        mine = [x for x in C.shots(ctx, me) if x['round'] == rnd and x['tick'] < shots[0]['tick']]
        dk = C.deaths(ctx, rnd)
        kt = dk[dk['att'] == me]['tick'].to_numpy()
        for x in reversed(mine):
            if ((kt >= x['tick'] - _tk(S, th['duel_pre_untargeted_s'])) & (kt <= shots[0]['tick'])).any():
                break                       # 刚打死一个人后接着扫的枪是续扫，不往前接（bd R10 11.7）
            if x['target'] is None and shots[0]['tick'] - x['tick'] <= _tk(S, th['duel_pre_untargeted_s']):
                shots.insert(0, x)
            else:
                break
    noshot = not shots
    ref = shots[0]['tick'] if shots else int(anchor)
    look = float(th['duel_look_s'])
    D = dict(rnd=int(rnd), tgt=tgt, enemy=C.name_of(ctx, tgt), noshot=noshot, ref=int(ref),
             graze_hb=float(th['duel_graze_hb']), min_dist_m=float(th['duel_min_dist_m']))
    # ---- 接触：我看见他 / 他看见我 / 第一次互相打中，取最早
    fwd = int(th['duel_seeing_ticks'])
    my_see = seg_start(S, me, tgt, ref, look, fwd)
    he_see = seg_start(S, tgt, me, ref, look, fwd)
    h = C.hurts(ctx, rnd)
    end = (shots[-1]['tick'] + _tk(S, th['duel_end_s'])) if shots else int(anchor)
    if death is not None:
        end = max(end, int(death[1]))
    pair = h[(h['tick'] >= ref - _tk(S, look)) & (h['tick'] <= end)
             & (((h['att'] == me) & (h['vic'] == tgt)) | ((h['att'] == tgt) & (h['vic'] == me)))]
    cands = [x for x in (my_see, he_see) if x is not None]
    if len(pair):
        cands.append(int(pair['tick'].min()))
    contact = min(cands) if cands else ref
    contact = min(contact, ref)
    D.update(contact=int(contact), my_see=my_see, he_see=he_see, end=int(end))
    D['t_contact'] = C.t_of(ctx, contact, rnd)
    D['t'] = C.t_of(ctx, ref, rnd)
    D['my_place'] = C.place_cn_at(ctx, me, ref)
    D['enemy_place'] = C.place_cn_at(ctx, tgt, ref)
    # ---- 拉枪方式：接触前 duel_peek_s 秒双方速度（帧差）和各自的停稳线比
    wk_me = shots[0]['wk'] if shots else C.wkey(C.weapon_at(ctx, me, ref))
    wk_en = C.wkey(C.weapon_at(ctx, tgt, contact))
    D['wk'] = wk_me
    # 没开枪就死、但他打中你前 duel_pre_untargeted_s 秒到阵亡之间朝别处开过枪（128 R13：边跑边打另一个人）
    D['fired_other'] = False
    if not shots:
        f0 = C.fires(ctx, rnd, sid=me)
        hi0 = int(death[1]) if death is not None else int(anchor)
        D['fired_other'] = bool(len(f0[(f0['tick'] >= int(anchor) - _tk(S, th['duel_pre_untargeted_s'])) & (f0['tick'] <= hi0)]))
    # 他打中你之前手里拿的不是枪（烟 / 火 / 刀……）：没开枪就死时不按急停 / 准星判（a32 R6 拿着烟被 对手2（第2场） 打死）
    D['in_hand'] = None
    if not shots:
        hk = [C.wkey(C.weapon_at(ctx, me, int(anchor) - k)) for k in range(1, int(th['duel_hand_ticks']) + 1)]
        hk = [x for x in hk if x]
        if hk and not any(C.is_gun(x) for x in hk):
            f = C.fires(ctx, rnd, sid=me)
            qs = f[(f['tick'] < int(anchor)) & (f['tick'] >= int(anchor) - _tk(S, th['duel_quickswitch_s']))]
            if not (len(qs) and any(C.is_sniper(w) for w in qs['wk'])):      # AWP 开完枪切刀再切回来不算
                D['in_hand'] = hk[0]
    D['wcls'] = C.weapon_class(wk_me) if wk_me else None
    D['weapon'] = C.weapon_cn(wk_me) if wk_me else None
    D['sniper'] = bool(wk_me and C.is_sniper(wk_me))
    t0 = contact - _tk(S, th['duel_peek_s'])
    v_me = _speed_max(S, me, t0, contact)
    v_en = _speed_max(S, tgt, t0, contact)
    lim_me = C.still_limit(ctx, wk_me) if wk_me and C.is_gun(wk_me) else float(th['duel_hold_default'])
    lim_en = C.still_limit(ctx, wk_en) if wk_en and C.is_gun(wk_en) else float(th['duel_hold_default'])
    me_hold = v_me is not None and v_me <= lim_me
    en_hold = v_en is not None and v_en <= lim_en
    peek = ('both_hold' if me_hold and en_hold else 'me_peek' if en_hold else 'he_peek' if me_hold else 'both_move')
    D.update(peek=peek, me_hold=bool(me_hold), en_hold=bool(en_hold),
             he_duck=_duck(S, tgt, contact) > float(th['duel_duck'])
             or (len(pair[pair['att'] == tgt]) > 0 and _duck(S, tgt, int(pair[pair['att'] == tgt]['tick'].min())) > float(th['duel_duck'])))
    # ---- 局面：被闪、隔烟、看得见你的敌人
    D['flashed'] = C.blind_left(ctx, me, ref) > float(th['flash_left_s'])
    D['smoke'] = C.smoked(ctx, me, tgt, contact) is not None and C.smoked(ctx, me, tgt, ref) is not None
    see = _seeing(S, me, rnd, contact, ref)
    if tgt not in see and (he_see is not None or len(pair[pair['att'] == tgt])):
        see.append(tgt)
    D['seeing'] = sorted(set(see))
    D['seeing_names'] = [C.name_of(ctx, x) for x in sorted(set(see), key=lambda s: (s != tgt, s))]
    # ---- 看见那一刻准星离头（半身宽倍数）、朝向
    sref = my_see if my_see is not None else contact
    eyem = C.eye(ctx, me, sref)
    e = C.aim_err(eyem, *C.view(ctx, me, sref), C.eye(ctx, tgt, sref))
    hb = C.half_body_deg(ctx, e[3]) if _f(e[3]) else float('nan')
    D['err_see_hb'] = e[0] / hb if _f(e[0]) and _f(hb) and hb > 0 else float('nan')
    D['facing_see'] = C.facing_deg(ctx, me, sref, C.eye(ctx, tgt, sref))
    D['dist_m'] = float(e[3]) * float(th['unit_m']) if _f(e[3]) else float('nan')
    # ---- 对面看见我那一刻准星离我多远（度）：站位有没有被预瞄（第 35 条）
    if he_see is not None:
        ee = C.aim_err(C.eye(ctx, tgt, he_see), *C.view(ctx, tgt, he_see), C.eye(ctx, me, he_see))
        D['his_err_see'] = ee[0] if _f(ee[0]) else float('nan')
    else:
        D['his_err_see'] = float('nan')
    # 他看见我之前 duel_reexpose_s 秒内，我在同一个位置被他看见过（中间断开过）= 我从同一个地方又出来
    D['reexposed'] = False
    if he_see is not None:
        lo = he_see - _tk(S, th['duel_reexpose_s'])
        hi = he_see - _tk(S, th['duel_reexpose_gap_s'])
        prev = C.spots(ctx, tgt, me, lo, hi) if hi > lo else None
        if prev is not None and C.dist2(C.pos(ctx, me, prev), C.pos(ctx, me, he_see)) <= float(th['duel_reexpose_u']):
            D['reexposed'] = True
    # ---- 我的枪
    D['shots_n'] = len(shots)
    if shots:
        s0 = shots[0]
        D['first_still'] = bool(s0['still'])
        D['first_move'] = s0['move']
        D['first_hit'] = bool(s0['hit'])
        D['first_scoped'] = bool(s0['scoped'])
        D['aimvel'] = _aimvel(S, me, s0['tick'])
        D['body_hb'] = s0['err_body'] / s0['hb'] if _f(s0['err_body']) and _f(s0['hb']) and s0['hb'] > 0 else float('nan')
        D['head_hb'] = s0['err_head'][0] / s0['hb'] if _f(s0['err_head'][0]) and _f(s0['hb']) and s0['hb'] > 0 else float('nan')
        D['shot_dist_m'] = float(s0['dist']) * float(th['unit_m']) if _f(s0['dist']) else D['dist_m']
        gaps = [(shots[k + 1]['tick'] - shots[k]['tick']) / S.tr for k in range(len(shots) - 1)]
        D['gaps'] = gaps
        sg = float(th['duel_spray_gap_s'])
        nb = 1
        while nb <= len(gaps) and gaps[nb - 1] <= sg:
            nb += 1
        D['first_breath_n'] = nb
        D['first_breath_hits'] = sum(1 for x in shots[:nb] if x['hit'])
        D['released'] = any(g > float(th['duel_breath_gap_s']) for g in gaps)
        D['max_gap'] = max(gaps) if gaps else 0.0
        # 前 recover_s 秒内开过枪 / 接在上一个击杀后面续扫（准星被后坐带着，不拿来算“准星离他差多少”）
        f = C.fires(ctx, rnd, sid=me)
        prev = f[(f['tick'] < s0['tick']) & (f['tick'] >= s0['tick'] - _tk(S, th['recover_s']))]
        D['fired_before'] = bool(len(prev))
        dd = C.deaths(ctx, rnd)
        # 续扫：从这一串第一枪往回，间隔 <= duel_spray_gap_s 一直连着的枪里打死过别人
        ft = f['tick'].to_numpy()
        ft = ft[ft < s0['tick']]
        cs = s0['tick']
        for x in ft[::-1]:
            if cs - int(x) <= _tk(S, th['duel_spray_gap_s']):
                cs = int(x)
            else:
                break
        kb = dd[(dd['att'] == me) & (dd['tick'] <= s0['tick']) & (dd['tick'] >= cs) & (dd['vic'] != tgt)] if cs < s0['tick'] \
            else dd.iloc[0:0]
        D['transfer_from'] = C.name_of(ctx, kb['vic'].iloc[-1]) if len(kb) else None
        # 每一枪在连发里是第几发（间隔 <= duel_spray_gap_s 算连着）
        ks, k = [], 0
        for i, x in enumerate(shots):
            k = 0 if i == 0 or gaps[i - 1] > sg else k + 1
            ks.append(k)
        D['spray_k'] = ks
    # ---- 双方命中
    hcn = th['hitgroup_cn']
    mine = pair[pair['att'] == me]
    his = pair[pair['att'] == tgt]
    D['my_hits'] = [dict(part=hcn.get(r.hitgroup, '身上'), hg=r.hitgroup, low=_low_dmg(S, r.wk, r.hitgroup, r.dmg, r.health, r.dmg_armor > 0 or r.armor > 0),
                         tick=int(r.tick)) for r in mine.itertuples() if r.dmg > 0]
    D['his_hits'] = [dict(part=hcn.get(r.hitgroup, '身上'), hg=r.hitgroup, tick=int(r.tick), lethal=int(r.health) <= 0)
                     for r in his.itertuples() if r.dmg > 0]
    fm = int(mine['tick'].min()) if len(mine) else None
    fh = int(his['tick'].min()) if len(his) else None
    D['moving_at_his_hit'] = False
    D['en_still_at_his_hit'] = False
    if fh is not None:
        # 他打中你那一刻你的速度：取那一下之前 1–duel_hit_spd_ticks 帧（一枪打死时打中那一帧就是阵亡帧，位置更新不可靠：
        # dc2a R19 最后 0.1 秒已经停住、bd R19 AWP 到最后一帧前都在走）
        vs = [C.move_speed(ctx, me, fh - k) for k in range(1, int(th['duel_hit_spd_ticks']) + 1)]
        vs = [x for x in vs if _f(x)]
        v = float(np.mean(vs)) if vs else C.move_speed(ctx, me, fh)
        D['speed_at_his_hit'] = float(v) if _f(v) else None
        D['moving_at_his_hit'] = bool(_f(v) and v > (C.still_limit(ctx, wk_me, True) if D['sniper'] else lim_me))
        ve = C.move_speed(ctx, tgt, fh)
        D['en_still_at_his_hit'] = bool(_f(ve) and ve <= float(th['peek_enemy_still']))
    D['who_first'] = None if fm is None and fh is None else ('me' if fh is None or (fm is not None and fm <= fh) else 'him')
    fe = C.fires(ctx, rnd, sid=tgt)
    fe = fe[(fe['tick'] >= contact - _tk(S, th['duel_his_fire_back_s'])) & (fe['tick'] <= end)]
    D['his_first_fire'] = int(fe['tick'].min()) if len(fe) else None
    D['shot_first'] = bool(shots) and (D['his_first_fire'] is None or shots[0]['tick'] <= D['his_first_fire'])
    # 他打死我的那一枪是他这次对枪的第几枪
    D['his_k_fatal'] = None
    if death is not None:
        D['his_k_fatal'] = int((fe['tick'] <= int(death[1])).sum()) or None
    # ---- 结果
    dd = C.deaths(ctx, rnd)
    kk = dd[(dd['att'] == me) & (dd['vic'] == tgt) & (dd['tick'] >= ref) & (dd['tick'] <= end + _tk(S, th['duel_end_s']))]
    # 只算用枪打死、而且在你倒下之前（bd R25：你死后你扔的雷才炸死 对手5（第4场），不算“最后打死了他”）
    kk = kk[[C.is_gun(w) for w in kk['wk']]] if len(kk) else kk
    if death is not None and len(kk):
        kk = kk[kk['tick'] <= int(death[1])]
    D['killed'] = bool(len(kk))
    D['died'] = death is not None
    D['died_to_tgt'] = death is not None and str(death[2]) == tgt
    D['death_tick'] = int(death[1]) if death is not None else None
    # ---- 挨了第一下以后（第 18 条）：没被这一下打死、retreat_s 秒内没打死他、也没退出他的视线，最后被他打死
    D['no_retreat'] = False
    D['kept_firing'] = False
    if fh is not None and D['died_to_tgt'] and D['who_first'] == 'him':
        win = _tk(S, th['retreat_s'])
        if int(death[1]) - fh > win:
            k_dead = dd[(dd['vic'] == tgt) & (dd['tick'] >= fh) & (dd['tick'] <= fh + win)]
            if not len(k_dead) and _broke_los(S, me, tgt, fh + int(th['duel_los_skip_ticks']), fh + win) is None:
                D['no_retreat'] = True
                D['kept_firing'] = any(fh <= x['tick'] <= fh + win for x in shots)
    # ---- 往下压过了：连发第 3 发起空了、记录到的准星比“这一发打中胸口”的中位数还低 overpull_deg 以上，前面打中过
    D['overpull'] = False
    if shots and D['wcls'] == '步枪':
        tab = th['duel_recoil_chest_ud']
        hit_before = False
        for x, k in zip(shots, D['spray_k']):
            if x['hit']:
                hit_before = True
                continue
            if k >= int(th['spray_k_guard']) and hit_before and _f(x['err_head'][2]):
                ref_ud = tab[min(k, len(tab) - 1)]
                if x['err_head'][2] < ref_ud - float(th['duel_overpull_deg']):
                    D['overpull'] = True
                    break
    return D


# ================================================================ 句子小件
def _num(n):
    return C.num_cn(n)


def _parts_join(parts):
    seen = []
    for p in parts:
        if p not in seen:
            seen.append(p)
    return '和'.join(seen) if len(seen) <= 2 else '、'.join(seen)


def my_parts_phrase(D, lead='打中的'):
    """“打中的两枪在胸口和肚子、隔着东西打的”；没打中返回 ''"""
    hs = D.get('my_hits') or []
    if not hs:
        return ''
    n = len(hs)
    lows = sum(1 for x in hs if x['low'])
    s = f"{lead}{('那一枪' if lead else '一枪') if n == 1 else _num(n) + '枪'}在{_parts_join([x['part'] for x in hs])}"
    if lows * 2 >= n and lows:
        s += '、隔着东西打的'
    return s


def his_parts_phrase(D):
    """“他蹲着两枪都打在你头上” / “他第二枪打在你头上” / “他打中你三枪，最后一枪在头上”"""
    hs = D.get('his_hits') or []
    if not hs:
        return ''
    duck = '蹲着' if D.get('he_duck') else ''
    n = len(hs)
    parts = [x['part'] for x in hs]
    if n == 1:
        k = D.get('his_k_fatal')
        if hs[0]['lethal'] and k and 2 <= k <= 3:
            return f"他{duck}第{ORD[k]}枪打在你{parts[0]}上" if parts[0] == '头' else f"他{duck}第{ORD[k]}枪打在你{parts[0]}"
        return f"他{duck}一枪打在你{parts[0]}上" if parts[0] == '头' else f"他{duck}一枪打在你{parts[0]}"
    if len(set(parts)) == 1:
        return f"他{duck}{_num(n)}枪都打在你{parts[0]}" + ('上' if parts[0] == '头' else '')
    last = hs[-1]['part']
    return f"他{duck}打中你{_num(n)}枪，最后一枪在{last}" + ('上' if last == '头' else '')


def peek_phrase(D, me_place=None, neutral_both=False):
    """拉枪方式（第 5 条）：你拉他架 / 他拉你架 / 都在动；“在{我的位置}” 后面直接接"""
    en, ep = D['enemy'], D['enemy_place']
    same = me_place is not None and ep == me_place
    ep_s = f"同在{ep}" if same else ep
    if D['peek'] == 'me_peek':
        # 第 5 条例句：“拉出去打蹲着架 A 二楼的 对手1”（同一个报点也写出来）
        if D.get('he_duck'):
            return f"拉出去打蹲着架{ep}的 {en}"
        # 完整性复核二：“拉出去打架着超市的 X”里“打架”连读，改成“拉出去打在超市架着的 X”（同一个报点写“同在”）
        return f"拉出去打{'同' if same else ''}在{ep}架着的 {en}"
    if D['peek'] == 'he_peek':
        return f"对{ep_s}拉出来的 {en}"
    if D['peek'] == 'both_move' and not neutral_both:
        return f"，你和{ep_s}的 {en} 都在动"          # 完整性复核：“在 CT 出生点和 A 点的 X 都在动”读着像两个地方
    return f"对{ep_s}的 {en}"


# ================================================================ 判定表
def verdict(S, D):
    """按手册 1.13 判定表取第一条。返回 dict：
       step（表里的行号）、key、ok（True = 动作对 / False = 动作有错 / None = 不判动作）、kind、blame、
       clause（不带秒数的半句，给别的句子并）、text（带秒数的整句）、alt、clauses（附带从句列表）、parts（双方部位）"""
    th = S.th
    t = C.fmt_sec(D['t'])
    mp = D['my_place']
    en = D['enemy']
    pk = peek_phrase(D, mp)
    head = f"第 {t} 秒在{mp}{pk}"
    head_n = f"第 {t} 秒在{mp}{peek_phrase(D, mp, neutral_both=True)}"      # 后面接“停住 / 停稳”时不写“都在动”
    his = his_parts_phrase(D)
    extra = []
    if D.get('reexposed') and _f(D.get('his_err_see')) and D['his_err_see'] <= float(th['duel_prefire_deg']) \
            and D['peek'] in ('me_peek', 'both_move'):
        extra.append('他一直瞄着你出来的位置')
    V = dict(step=None, key=None, ok=None, kind='判定', blame='—', clause='', text='', alt='', clauses=[],
             peek=D['peek'], enemy=en, my_place=mp, enemy_place=D['enemy_place'], t=C.sec(D['t']),
             parts=dict(mine=[x['part'] for x in D.get('my_hits') or []], his=[x['part'] for x in D.get('his_hits') or []],
                        mine_low=[bool(x['low']) for x in D.get('my_hits') or []]),
             seeing=D.get('seeing_names') or [], no_retreat=bool(D.get('no_retreat')), overpull=bool(D.get('overpull')),
             prefired=bool(extra), killed=bool(D.get('killed')), moving=bool(D.get('moving_at_his_hit')),
             transfer_from=D.get('transfer_from'))

    def done(step, key, ok, clause, core=None, tail='', kind=None, blame=None, alt='', nr=True):
        """core：主句（不带结尾）；从句（预瞄、没退出视线）插在主句和结尾之间"""
        V.update(step=step, key=key, ok=ok, clause=clause, alt=alt)
        V['kind'] = kind or ('问题' if ok is False else '判定')
        V['blame'] = blame or ('—' if ok is not False else '小问题')
        if ok is False and D.get('killed'):
            V['blame'] = '小问题'           # 换掉了他（审核问题 Q13 默认：人多时出去补人、换掉了他记小问题）
        cl = list(extra)
        if nr and D.get('no_retreat') and key != 'no_retreat':
            cl.append('挨了第一下以后没退出他的视线' + ('，继续站着打' if D.get('kept_firing') else ''))
        V['clauses'] = cl
        body = core if core is not None else f"{head}，{clause}"
        V['text'] = body + ''.join(f"，{c}" for c in cl if c not in body) + tail
        return V

    # ---- 0 被闪 / 隔烟：不判
    if D['flashed']:
        return done(0, 'flashed', None, '开枪时被闪着', nr=False)
    if D['smoke']:
        return done(0, 'smoke', None, '隔着烟交火', nr=False)
    # ---- 0 两个以上敌人同时看得见你：归站位（第 10 条）；正在打别人时被他打死也是
    if D.get('busy_with') or len(D.get('seeing') or []) >= int(th['duel_two_seeing_n']):
        names = list(D['seeing_names'])
        if D.get('busy_with'):
            names = [D['busy_with']] + [n for n in names if n != D['busy_with']]
            if en not in names:
                names.append(en)
        who = ' 和 '.join(names[:2]) if len(names) == 2 else '、'.join(names)
        cl = f"开打的位置 {who} 都打得到"
        core = (f"第 {t} 秒在{mp}正和 {D['busy_with']} 交火时被 {en} 打死，{cl}" if D.get('busy_with')
                else f"第 {t} 秒在{mp}和 {en} 交火，{cl}")
        V['seeing'] = names
        return done(0, 'two_seeing', None, cl, core=core, alt='还可以先用烟或闪挡掉一边，一次只和一个人对枪。', nr=False)
    # ---- 1 没开枪就死
    if D['noshot'] and D.get('in_hand'):
        tl = f"，{his}" if his else ''
        hw = HAND_CN.get(D['in_hand'], '道具')
        ep_s = ('同在' if D['enemy_place'] == mp else '') + D['enemy_place']
        cl = f"他打中你时你手里拿着{hw}，没法还枪"
        return done(1, 'in_hand', None, cl, core=f"第 {C.fmt_sec(D['t_contact'])} 秒在{mp}，和{ep_s}的 {en} 碰上，{cl}",
                    tail=tl, nr=False)
    if D['noshot']:
        tl = f"，{his}" if his else ''
        if D.get('moving_at_his_hit') and (D['peek'] == 'me_peek' or D.get('en_still_at_his_hit')):
            nz = '枪口还没对着他' if D.get('fired_other') else '一枪没开'     # 复核修正：朝别处开过枪的不写“一枪没开”
            return done(1, 'peek_into_holder', False, f'他打中你那一刻你还没停住，{nz}',
                        core=(head if D['peek'] == 'me_peek' else
                              f"第 {t} 秒在{mp}，和{('同在' if D['enemy_place'] == mp else '') + D['enemy_place']}的 {en} 碰上")
                        + f"，他打中你那一刻你还没停住，{nz}就被打死", tail=tl,
                        alt='还可以出去前先停在掩体边，让他先暴露，出去后马上停住再开枪。')
        if D['my_see'] is None:
            return done(1, 'unseen', None, '还没看见他就被打中',
                        core=f"第 {C.fmt_sec(D['t_contact'])} 秒在{mp}还没看见{'同在' if D['enemy_place'] == mp else ''}"
                             f"{D['enemy_place']}的 {en} 就被他打中", tail=tl)
        if _f(D['err_see_hb']) and D['err_see_hb'] > float(th['duel_aim_off_hb']) and D['facing_see'] <= float(th['facing_deg']):
            cl1 = ('拉出去时准星没放在他架的位置，没来得及开枪' if D['peek'] == 'me_peek'
                   else f"{en} 出来时准星没放在他出来的位置，没来得及开枪")
            return done(1, 'aim_off', False, cl1, tail=tl, blame='你的责任',
                        alt='还可以站定时把准星提前放在他最可能出来的口子、和头一样高。')
        tl = ('，' + his[1:]) if his.startswith('他') else tl
        return done(1, 'beaten', True, '准星已经在他身上，他先开枪', tail=tl)
    far = _f(D.get('shot_dist_m')) and D['shot_dist_m'] >= float(th['duel_min_dist_m'])
    tail = _tail(D, his)
    # ---- 2 第一枪没停稳、没中、10 米外
    if not D['first_still'] and not D['first_hit'] and far and not D['sniper']:
        run = D['first_move'] == '跑着'
        cl = (f"第一枪是{'跑着' if run else '还没停稳'}开的，没中" if D['peek'] != 'me_peek'
              else f"第一枪{'还在跑' if run else '还在走'}，没中")
        return done(2, 'no_stop', False, cl, tail=tail, blame='你的责任', alt='还可以先急停再开第一枪。')
    # ---- 3 准星没放在他出来的位置（续扫 / 刚开过枪的不判）
    guard = D.get('fired_before') or D.get('transfer_from')
    lost_first = D['who_first'] == 'him' or not D['first_hit']
    if not guard and not D['sniper'] and _f(D['err_see_hb']) and D['err_see_hb'] > float(th['duel_aim_off_hb']) \
            and D['facing_see'] <= float(th['facing_deg']) and lost_first:
        cl = '碰上时准星离他还差一大截' if D['peek'] in ('me_peek', 'both_move') else '他一出来准星离他还差一大截，要拉过去才开枪'
        if _rushed(th, D):
            cl += '，没等准星到他身上就开了'          # 手册 1.3：8d R4 19.0 第 3 条为主、第 4 条作从句
        return done(3, 'aim_off', False, cl, tail=tail, blame='你的责任',
                    alt='还可以站定时把准星提前放在他最可能出来的口子、和头一样高。')
    if D.get('transfer_from'):
        cl = f"打死 {D['transfer_from']} 后没松开，一口气扫到 {en} 身上"
        p = my_parts_phrase(D)
        return done(3, 'transfer', None, cl, core=f"第 {t} 秒在{mp}{cl}" + (f"，{p}" if p else ''),
                    tail=_tail(D, his, skip_mine=True))
    # ---- 4 停住了，但准星还没拉到他身上就开了（抢枪）
    if _rushed(th, D):
        cl = f"停住了，但准星还没拉到 {en} 身上就开了，没中"
        return done(4, 'rush', False, cl, core=f"{head_n}，{cl}", tail=tail, alt='还可以停住以后等准星到他身上再开第一枪。')
    # ---- 5 先打中但打在身上，他打的是头
    mh = D.get('my_hits') or []
    hh = D.get('his_hits') or []
    if D['who_first'] == 'me' and D['died_to_tgt'] and not D['killed'] and mh \
            and not any(x['hg'] in ('head', 'neck') for x in mh) and hh and hh[-1]['hg'] == 'head':
        mp_ = my_parts_phrase(D, lead='')
        cl = f"先打中他{mp_}" if mp_ else '先打中他'
        g = _graze(D)
        core = (f"{head_n}，{_act(D)}、{cl}，动作对，是枪法" + (f"；{g}" if g else '')
                + ('，后面往下压过了' if D.get('overpull') else ''))
        return done(5, 'body_vs_head', True, cl, core=core, tail=f"，{his}")
    # ---- 7 20 米外一口气扫到底、几乎没中
    if D['wcls'] in ('步枪', '冲锋枪', '机枪') and _f(D.get('shot_dist_m')) and D['shot_dist_m'] > float(th['long_spray_m']) \
            and D['first_breath_n'] >= int(th['long_spray_n']) and D['first_breath_hits'] <= int(th['long_spray_hits']):
        return done(7, 'long_spray', False, '一口气扫到底，没有松开重新对准', tail=tail, blame='你的责任',
                    alt='还可以两三发没中就松开，重新对准再打。')
    # ---- 8 手枪点得太快
    if D['wcls'] == '手枪' and D.get('gaps') and not mh and _f(D.get('shot_dist_m')) \
            and D['shot_dist_m'] > float(th['fast_tap_m']) \
            and D['gaps'][0] <= (float(th['deagle_fast_s']) if D['wk'] == 'deagle' else float(th['fast_tap_s'])):
        return done(8, 'fast_tap', False, '前两枪点得太快，上一枪的晃动还没回稳就开了下一枪', tail=tail, blame='你的责任',
                    alt='还可以前两枪点慢一点，等准星回稳再点。')
    # ---- 9 挨了第一下以后没退出他的视线
    if D.get('no_retreat'):
        cl = '挨了第一下以后没退出他的视线' + ('，继续站着打' if D.get('kept_firing') else '')
        p = my_parts_phrase(D)
        core = f"{head_n}，{_act(D)}" + ('，第一枪没中' if not D['first_hit'] else '') + f"；{cl}" + (f"，{p}" if p else '')
        return done(9, 'no_retreat', False, cl, core=core, tail=_tail(D, his, skip_mine=True),
                    alt='还可以挨了一下又打不死他时先退出他的视线，再换个角度打。')
    # ---- 10 AWP 没开镜 / 没停稳 / 跑着拉
    if D['sniper']:
        gun = '鸟狙' if D['wk'] == 'ssg08' else 'AWP'
        if not D['first_scoped'] or not D['first_still']:
            cl = f"{gun}第一枪" + ('没开镜' if not D['first_scoped'] else '人还在走') + ('，没中' if not D['first_hit'] else '')
            return done(10, 'awp', False, cl, tail=tail, blame='你的责任', alt='还可以停稳开镜再打。')
        if D['who_first'] == 'him' and D.get('moving_at_his_hit') and D['peek'] in ('me_peek', 'both_move'):
            # 审核问题 Q9 默认：AWP 跑着拉出去、停下开镜前他已经先打中了你 → 只写“AWP 别跑着拉”
            cl = f"{gun}跑着拉出去，还没停住他就先打中了你"
            ep_s = ('同在' if D['enemy_place'] == mp else '') + D['enemy_place']
            return done(10, 'awp_run', False, cl, core=f"第 {t} 秒在{mp}对{ep_s}的 {en}，{cl}", tail=tail,
                        blame='小问题', alt=f'还可以别拿着 {gun} 跑着拉出去。')
    # ---- 第一枪边走边开但打中了（10 米外）：不说动作对，只写事实
    bits = []
    g = _graze(D)
    if g:
        bits.append(g)
    p = my_parts_phrase(D)
    if p:
        bits.append(p)
    if D.get('overpull'):
        bits.append('后面往下压过了')
    if not D['first_still'] and far:
        cl = '第一枪是边走边开的，打中了' if D['first_hit'] else '第一枪是边走边开的'
        return done(11, 'moving_hit', None, cl, core=f"{head}，{cl}" + ''.join(f"，{b}" for b in bits if b != g),
                    tail=_tail(D, his, skip_mine=True))
    # ---- 11 动作对，是枪法
    cl = '动作对，是枪法'
    first = _act(D) + ('、先打中他' + (_num(len(mh)) + '枪' if len(mh) > 1 else '') if D['who_first'] == 'me' else '')
    core = f"{head_n}，{first}，{cl}" + (('；' + '，'.join(bits)) if bits else '')
    if his and D['died_to_tgt'] and not D['killed']:
        tl = f"，{his}" if bits else f"；{his}"
    else:
        tl = tail
    return done(11, 'ok', True, cl, core=core, tail=tl)


def _act(D):
    """动作本身：架着停稳先开枪 / 停稳开枪 / 拉出去停稳再开枪"""
    if not D.get('first_still'):
        near = _f(D.get('shot_dist_m')) and D['shot_dist_m'] < D.get('min_dist_m', 10.0)
        return '贴脸边走边开' if near else '边走边开'
    if D['peek'] == 'me_peek':
        return '停稳再开枪'               # 句首已经写了“拉出去打……”（复核修正：不写两遍“拉出去”）
    if D['me_hold'] and D.get('shot_first'):
        return '架着停稳先开枪'
    if D.get('shot_first'):
        return '停稳先开枪'
    return '停稳开枪'


def _graze(D):
    """第一枪没中、但离头不到 duel_graze_hb 个半身宽：“第一枪擦着头过去”"""
    if D.get('noshot') or D.get('first_hit'):
        return ''
    hb = D.get('head_hb')
    return '第一枪擦着头过去' if _f(hb) and hb <= D.get('graze_hb', 1.0) else ''


# ================================================================ 每一次阵亡
def death_duel(S, rnd, death):
    """用户这次阵亡的对枪：(信号 D, 用户这一串 shots) 或 None（凶手不是敌人 / 不是枪打死的）。
    死前 duel_death_window_s 秒内对凶手开过枪：取最后一串（common.bursts 里目标是他的串）；没有就按“没开枪就死”，
    anchor = 他第一次打中你的那一下（同一窗口内）"""
    ctx, th = S.ctx, S.th
    t_d, dtk, K = death
    if K is None or not C.is_enemy(ctx, S.u, K, rnd):
        return None
    d = C.deaths(ctx, rnd)
    row = d[(d['vic'] == S.u) & (d['tick'] == dtk)]
    if not len(row) or not C.is_gun(row['wk'].iloc[0]):
        return None
    win = _tk(S, th['duel_death_window_s'])
    bs = [b for b in C.bursts(ctx) if b['round'] == rnd and b['target'] == K and b['tick'] <= dtk
          and b['shots'][-1]['tick'] >= dtk - win]
    if bs:
        b = bs[-1]
        return signals(S, rnd, K, shots=b['shots'], death=death), b
    # 正在打另一个人时被他打死（交叉火力）
    busy = [b for b in C.bursts(ctx) if b['round'] == rnd and b['target'] and b['target'] != K and b['seen']
            and b['tick'] <= dtk and b['shots'][-1]['tick'] >= dtk - _tk(S, th['duel_busy_s'])]
    h = C.hurts(ctx, rnd)
    hk = h[(h['att'] == K) & (h['vic'] == S.u) & (h['dmg'] > 0) & (h['tick'] <= dtk) & (h['tick'] >= dtk - win)]
    anchor = int(hk['tick'].iloc[0]) if len(hk) else int(dtk)
    # 没认出目标的枪（准星离他还有几度，C-TARGET 认不出）也是开了枪：他第一次打中你前 duel_pre_untargeted_s 秒到你倒下，
    # 朝他方向（离他 <= duel_untargeted_deg）开的枪当这一串（bd R29 74.94 离 对手3（第4场） 4 度那一枪，不能写“一枪没开”）
    lo = anchor - _tk(S, th['duel_pre_untargeted_s'])
    loose = []
    for x in C.shots(ctx, S.u):
        if x['round'] != rnd or x['tick'] < lo or x['tick'] > dtk or x['target'] not in (None, K):
            continue
        tk = int(x['tick'])
        e = C.aim_err(x['src'], x['pitch'], x['yaw'], C.eye(ctx, K, tk))
        if not _f(e[0]) or e[0] > float(th['duel_untargeted_deg']):
            continue
        if x['target'] is None:
            x = dict(x, err_body=C.body_err(ctx, x['src'], x['pitch'], x['yaw'], K, tk), dist=e[3],
                     err_head=e[:3], hb=C.half_body_deg(ctx, e[3]) if _f(e[3]) else float('nan'))
        loose.append(x)
    if loose:
        b = dict(round=rnd, target=K, tick=int(loose[0]['tick']), t=loose[0]['t'], shots=loose, seen=None, loose=True)
        return signals(S, rnd, K, shots=loose, death=death), b
    D = signals(S, rnd, K, shots=None, anchor=anchor, death=death)
    if busy:
        D['busy_with'] = C.name_of(ctx, busy[-1]['target'])
    return D, None


def _tail(D, his, skip_first=False, skip_mine=False):
    """问题句的结尾：你打中他的部位（第 6 条）+ 他怎么打中你的 / 结果"""
    if D.get('killed'):
        return '，最后打死了他' + ('，你也倒了' if D.get('died') else '')
    if not D.get('died_to_tgt'):
        return ''
    mine = my_parts_phrase(D, lead='')
    pre = ''
    if mine and not skip_first and not skip_mine:
        pre = ('，之后先打中他' if D.get('who_first') == 'me' and not D.get('first_hit') else
               '，先打中他' if D.get('who_first') == 'me' else '，打中他') + mine
    if his:
        return f"{pre}，{his}"
    return pre + '，最后被他打死'


def _rushed(th, D):
    """第 4 条：第一枪停稳了、开枪那一下准星还在快速拉、离他身体超过 rush_body_hb 个半身宽、没中"""
    return (not D.get('noshot') and D.get('first_still') and not D.get('sniper') and not D.get('first_hit')
            and not (_f(D.get('shot_dist_m')) and D['shot_dist_m'] < float(th['duel_rush_min_m']))
            and _f(D.get('aimvel')) and D['aimvel'] > float(th['rush_aimvel'])
            and _f(D.get('body_hb')) and D['body_hb'] > float(th['rush_body_hb']))
