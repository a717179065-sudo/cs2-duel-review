# -*- coding: utf-8 -*-
"""规则模块：经济与买法（ECO-01..09）、捡枪升级（ECO-G1）、装备差距下输掉对枪（COST-01）。

依据：rules_v1.json 对应条目 + decisions_v1.md #19（T/CT 手枪局输了、第二回合钱够强起却 eco → 写成“还有这种打法”，
选择权在用户；T 方没下包也提）+ 用户写作规则（买法没问题的不提；和强者不同只当“另一种思路”，不说买法对/错）
+ 审核单（手册 v1，2026-10-30）：#22 你自己没买（ECO-04 (c)、COST-01 不出）、#23 实测连败计数 + 对面赛点、
#32 eco 阵亡只留一条还可以（COST-01 facts.alt_yields_to，merge 落地）、#33 跨回合链 chain_saved / chain_rebuy、
#36 ECO-04 句尾按上回合结果推、Q21 对面半起不算反 eco（thresholds pistols_only_is_eco）。
原型：pro/analysis/match3/rules_proto/经济与买法.py（ECO-01..09）、好的操作.py GOOD-12（ECO-G1）、
开枪与准星.py COST-01。

数据：ctx.json('economy') / ctx.json('timeline')、ctx.cache('ev_item_pickup')；C-* 口径一律走 engine.common。
买枪时刻（ECO-02 退钱、ECO-04 买冲锋枪）facts 层没有缓存，这里用 demoparser2 直接读 ctx.dem 的
item_purchase 和买枪阶段的 balance（约 1 秒，只在需要时读；读不到就退回买枪结束时刻）。
阈值：ctx.th['eco']（thresholds/eco.json）+ ctx.th['common']（C-ECO 全起线 / 强起线 / 起狙线）。

输出：run(ctx) -> [Hit]（CONTRACT）。facts['slot'] 标明这条命中该进 review 的哪一栏：
  moment / economy.idea / main_cause / summary.momentum / summary.headline / summary.team
"""
from collections import Counter

from engine import common as C

RULES = ('ECO-01', 'ECO-02', 'ECO-03', 'ECO-04', 'ECO-05', 'ECO-06', 'ECO-07', 'ECO-08', 'ECO-09', 'ECO-G1', 'COST-01')
TEAM = '全队'                                   # 回合层面、全队买法的主角
FORCE_GUNS = {'T': 'Galil、吹风机', 'CT': 'MP9、法玛斯'}   # 强起常见的枪（句子用）
ARMOR_CN = {'Kevlar Vest': '甲', 'Kevlar & Helmet': '头盔甲'}


# ================================================================ 基础数据
def _th(ctx):
    return ctx.th['eco']


def _econ(ctx):
    """economy.json 的回合 {回合号: 回合}"""
    return {int(r['round']): r for r in ctx.json('economy')['rounds']}


def _tl(ctx):
    """timeline.json 的回合 {回合号: 回合}"""
    return {int(r['round']): r for r in ctx.json('timeline')['rounds']}


def _half(ctx, r):
    """第几个半场（0 上半场、1 下半场、2 起是加时的每个半场）"""
    hr, ot = _th(ctx)['half_rounds'], _th(ctx)['ot_half_rounds']
    if r <= 2 * hr:
        return (r - 1) // hr
    return 2 + (r - 2 * hr - 1) // ot


def _half_end(ctx, r):
    """这个半场按赛制的最后一回合"""
    hr, ot = _th(ctx)['half_rounds'], _th(ctx)['ot_half_rounds']
    h = _half(ctx, r)
    return hr * (h + 1) if h < 2 else 2 * hr + ot * (h - 1)


def _half_rounds(ctx, E, r):
    """数据里和 r 同一半场的回合（排好序）"""
    return sorted(x for x in E if _half(ctx, x) == _half(ctx, r))


def _last_of_half(ctx, E, r):
    """半场最后一回合，或者整场最后一回合"""
    return r == _half_end(ctx, r) or r == max(E)


def _pistol(ctx, r):
    """常规半场第一回合 = 手枪局（加时没有手枪局）"""
    hr = _th(ctx)['half_rounds']
    return r in (1, hr + 1)


def _second(ctx, r):
    """手枪局之后的第二回合"""
    return r - 1 >= 1 and _pistol(ctx, r - 1)


def _same_half(ctx, a, b):
    return _half(ctx, a) == _half(ctx, b)


def _won(E, r):
    return E[r]['result'] == '赢'


def _score(E, r):
    a, b = E[r]['score_before'].split(':')
    return int(a), int(b)


def _score_after(E, r):
    a, b = _score(E, r)
    return (a + 1, b) if _won(E, r) else (a, b + 1)


def _side(E, r):
    return C.side_name(E[r]['side'])


def _lb(ctx, k):
    """连败计数 k（输完之后）的败方奖励"""
    k = max(1, min(5, int(k)))
    return int(_th(ctx)['loss_bonus'][str(k)])


def _lb_max(ctx):
    """败方奖励最大一档（$3400）"""
    return max(int(v) for v in _th(ctx)['loss_bonus'].values())


def _lc(E, r, who='us'):
    """回合开始时的连败计数（economy.json loss_counter，demo 实测：每半场从 1 起，输 +1、赢 −2）"""
    return int((E[r].get('loss_counter') or {}).get(who) or 0)


def _loss_reward(ctx, E, r, who='us'):
    """这回合输了拿的败方奖励（按回合开始的实测计数 +1 查表；不含 T 下包的 +$600）"""
    return _lb(ctx, _lc(E, r, who) + 1)


def _match_point(ctx, E, r):
    """赛点：返回 'us' / 'them' / 'both' / None。常规先到 half_rounds + 1 胜，加时第 k 轮先到
    half_rounds + ot_half_rounds×(k+1) + 1 胜（和 post._exempt、compose.match_point 同一口径）"""
    hr, ot = int(_th(ctx)['half_rounds']), int(_th(ctx)['ot_half_rounds'])
    a, b = _score(E, r)
    if r <= 2 * hr:
        target = hr + 1
    else:
        target = hr + ot * ((r - 2 * hr - 1) // (2 * ot) + 1) + 1
    mine, theirs = a == target - 1, b == target - 1
    return 'both' if mine and theirs else ('us' if mine else ('them' if theirs else None))


def _them_mp(ctx, E, r):
    """对面赛点（含双方赛点）：这回合输了就没有下回合"""
    return _match_point(ctx, E, r) in ('them', 'both')


def _user_name(ctx):
    return C.name_of(ctx, C.user_sid(ctx))


def _who(ctx, name):
    """句子里的人名：用户写“你”"""
    return '你' if name == _user_name(ctx) else name


def _bought(p):
    """这名玩家这回合没退的购买（去掉“(退)”）"""
    return [x for x in p.get('purchases') or [] if not x.endswith('(退)')]


def _refunded(p):
    """这名玩家这回合买了又退的东西（按购买顺序去重）"""
    out = []
    for x in p.get('purchases') or []:
        if x.endswith('(退)'):
            it = x[:-3]
            if it not in out:
                out.append(it)
    return out


def _tier(w):
    return C.gun_tier(w) if w else -1


def _join(items):
    """两个用“和”，多个用顿号"""
    items = list(items)
    return '和'.join(items) if len(items) == 2 else '、'.join(items)


def _gun_count_text(names):
    """['AK-47', 'AK-47', 'AWP'] → '2 把 AK、1 把 AWP'（按数量、再按中文名排）"""
    c = Counter(C.weapon_cn(n) for n in names if n)
    return '、'.join(f'{k} 把 {g}' for g, k in sorted(c.items(), key=lambda x: (-x[1], x[0])))


# ================================================================ demo 里的购买 / 退钱时刻（facts 层没有缓存，见 needs_from_others）
def _parser(ctx):
    key = '_eco_parser'
    if key not in ctx._cache:
        try:
            from demoparser2 import DemoParser
            ctx._cache[key] = DemoParser(str(ctx.dem))
        except Exception:                       # demo 不在 / 解析库不可用：调用方退回买枪结束时刻
            ctx._cache[key] = None
    return ctx._cache[key]


def _purchases(ctx):
    """item_purchase（tick, steamid 字符串, item_name, was_sold），读不到返回 None"""
    key = '_eco_purchase'
    if key not in ctx._cache:
        df = None
        p = _parser(ctx)
        if p is not None:
            try:
                df = p.parse_event('item_purchase')[['tick', 'steamid', 'item_name', 'was_sold']].copy()
                df['steamid'] = df['steamid'].astype(str)
                df = df.sort_values(['tick', 'steamid', 'item_name'], kind='mergesort').reset_index(drop=True)
            except Exception:
                df = None
        ctx._cache[key] = df
    return ctx._cache[key]


def _refund_ticks(ctx, r, sids):
    """买枪阶段（冻结开始到买枪结束）这些人 balance 上涨的时刻 = 退钱：[(sid, tick, 退回金额)]，按 tick 排"""
    key = f'_eco_refund_{r}'
    if key not in ctx._cache:
        out = None
        p = _parser(ctx)
        kt = _tl(ctx)[r]['key_ticks']
        if p is not None and kt.get('buy_end'):
            try:
                df = p.parse_ticks(['balance'], ticks=list(range(int(kt['freeze_start']) + 2, int(kt['buy_end']) + 1)))
                df['steamid'] = df['steamid'].astype(str)
                df = df.sort_values(['steamid', 'tick'], kind='mergesort')
                df['d'] = df.groupby('steamid')['balance'].diff()
                out = [(str(x.steamid), int(x.tick), int(x.d)) for x in df[df['d'] > 0].itertuples()]
            except Exception:
                out = None
        ctx._cache[key] = out
    out = ctx._cache[key]
    if out is None:
        return None
    return sorted([x for x in out if x[0] in sids], key=lambda x: (x[1], x[0]))


def _buy_tick(ctx, r, sid, item):
    """这回合 sid 买（没退）item 的第一个 tick；读不到返回 None"""
    df = _purchases(ctx)
    if df is None:
        return None
    kt = _tl(ctx)[r]['key_ticks']
    lo, hi = int(kt['freeze_start']), int(kt['round_start']) + 25 * int(ctx.th['common']['tickrate'])
    x = df[(df.steamid == str(sid)) & (df.item_name == item) & (~df.was_sold.astype(bool)) & (df.tick >= lo) & (df.tick <= hi)]
    return int(x.tick.iloc[0]) if len(x) else None


def _first_buy_tick(ctx, r, sid, items):
    """sid 这回合买 items 里任一样的第一个 tick（含退掉的），排序用"""
    df = _purchases(ctx)
    if df is None:
        return None
    kt = _tl(ctx)[r]['key_ticks']
    x = df[(df.steamid == str(sid)) & df.item_name.isin(list(items)) & (df.tick >= int(kt['freeze_start']))
           & (df.tick <= int(kt['round_start']) + 25 * int(ctx.th['common']['tickrate']))]
    return int(x.tick.iloc[0]) if len(x) else None


# ================================================================ Hit


def _tidy(ctx, s):
    """句子排版：common.tidy（人名整体保护）"""
    return C.tidy(s, C.player_names(ctx))


def _hit(ctx, rule, kind, rnd, scope, subject, blame, facts, text, alt='', evidence='', t=0.0, tick=None,
         death_t=None, death_of=None, merge_key=None):
    return dict(rule=rule, kind=kind, round=int(rnd), t=C.sec(t) if t is not None else 0.0, tick=tick, scope=scope,
                subject=subject, is_user=subject == _user_name(ctx), blame=blame, death_t=death_t, death_of=death_of,
                merge_key=merge_key, facts=facts, text=_tidy(ctx, text), alt=_tidy(ctx, alt), kb=[], evidence=_tidy(ctx, evidence))


def _round_facts(E, r, slot):
    """回合层面的公共 facts"""
    u, m = E[r]['us'], E[r]['them']
    return dict(slot=slot, side=_side(E, r), score_before=E[r]['score_before'], result=E[r]['result'],
                buy=u['buy'], them_buy=m['buy'], avg_start=int(u['avg_start']))


# ================================================================ ECO-01 手枪局输后第二回合
def eco01(ctx, E, H, dbg):
    th_c = ctx.th['common']
    for r in sorted(E):
        if not _second(ctx, r) or r - 1 not in E or not _same_half(ctx, r - 1, r) or _won(E, r - 1):
            continue
        s, u = _side(E, r), E[r]['us']
        cf, b = C.can_force(ctx, r), u['buy']
        planted = s == 'T' and bool(E[r - 1]['plant'])        # 我方 T 手枪局下过包
        pre = '手枪局下过包输了' if planted else '手枪局输了'
        n_ok = sum(1 for p in u['players'] if p['start'] >= C.force_line(ctx, s))
        f = _round_facts(E, r, 'economy.idea')
        f.update(can_force=cf, planted_pistol=planted, n_can_force=n_ok, force_avg=int(th_c['force_avg']))
        if cf and b in ('强起', '半起', '全起'):
            dbg.append(f'ECO-01 R{r} (A) 钱够强起、起了 {b}：买法没问题，不出（用户规则：买法没问题的不提）')
            continue
        if ((cf and b == 'eco') or (not cf and b in ('强起', '半起'))) and _won(E, r):
            dbg.append(f'ECO-01 R{r} 买法 {b} 赢了：不出（和赢下来的回合放一起读着矛盾）')
            continue
        if cf and b == 'eco':                                   # (B) 决定 #19：T/CT 都提，写成“还有这种打法”
            text = f'{pre}，这回合全队钱够强起，最后选了 eco'
            alt = (f'还有这种打法：全队强起（{FORCE_GUNS[s]}加头盔甲）搏一把，赢了下回合就是打对面 eco，'
                   f'对面还要再穷一回合；eco 攒钱也是常见选择，两种都可以')
            ev = f'开局人均 {C.money(u["avg_start"])}，{n_ok} 人的钱够强起'
            H.append(_hit(ctx, 'ECO-01', '思路', r, 'round', TEAM, '队友或团队', f, text, alt, ev))
        elif not cf and b in ('强起', '半起'):                   # (C)
            # 审核单 #23：对面赛点没有下回合，直接不出“还可以先 eco”（a32 R14：1:12 全队强起）
            if _them_mp(ctx, E, r):
                dbg.append(f'ECO-01 R{r} (C) 对面赛点，这回合输了就没有下回合：不出“先 eco”')
                continue
            # 用户规则：手枪局输后强起是常规，大家都够强起线、也真的强起了的，不提别的买法
            if b == '强起' and n_ok >= int(th_c['force_n']):
                dbg.append(f'ECO-01 R{r} (C) 人均差一点但 {n_ok} 人够强起线、全队强起：常规买法，不出')
                continue
            # 只有 eco 了下回合真能全起（这回合不花钱 + 再输一回合的败方奖励 >= 全起线）才提 eco，并写出理由
            # 审核单 #23 / 手册 ECON-A：档位按回合开始的实测连败计数（economy.json loss_counter）+1 查，
            # 不按“本半场连输几回合”（手枪局输了第二回合开局计数已经是 2，再输拿 $2400，56542d6d R2）
            k = _lc(E, r)
            bonus = _loss_reward(ctx, E, r)
            proj = int(u['avg_start']) + bonus
            if proj < C.full_buy_line(ctx, s):
                dbg.append(f'ECO-01 R{r} (C) eco 了下回合人均也只有 {proj}，不到全起线：不出')
                continue
            f.update(next_avg_if_eco=proj, loss_bonus_next=bonus, full_buy_line=C.full_buy_line(ctx, s), loss_counter=k)
            text = (f'{pre}，这回合全队钱不够强起，起了半套' if b == '半起'
                    else f'{pre}，这回合钱不太够，还是起了')
            alt = (f'还可以先 eco：这回合不花钱，就算输了下回合人均也有 {C.money(proj)}，'
                   f'够全队起长枪加甲一起打')
            ev = (f'开局人均 {C.money(u["avg_start"])}，不到 {C.money(th_c["force_avg"])}' if u['avg_start'] < th_c['force_avg']
                  else f'开局人均 {C.money(u["avg_start"])}，只有 {n_ok} 人的钱够强起')
            H.append(_hit(ctx, 'ECO-01', '思路', r, 'round', TEAM, '队友或团队', f, text, alt, ev))
        else:
            dbg.append(f'ECO-01 R{r} 买法 {b}、can_force={cf}：不出')


# ================================================================ ECO-02 该强起却 eco（含买了又退）
def _item_cn(it):
    if it in ARMOR_CN:
        return ARMOR_CN[it]
    return C.weapon_cn(it, default=None) if C.is_gun(it) else None


def _item_order(it):
    """句子里物品的顺序：长枪 / 冲锋枪 → 手枪 → 甲"""
    if it in ARMOR_CN:
        return 2
    return 0 if _tier(it) >= 1 else 1


def eco02(ctx, E, H, dbg):
    th = _th(ctx)
    hit_rounds = set()
    for r in sorted(E):
        if _pistol(ctx, r) or _second(ctx, r) or _last_of_half(ctx, E, r):
            continue
        pr = r - 1
        if pr in E and _same_half(ctx, pr, r) and E[pr]['us']['buy'] in ('强起', '半起') and not _won(E, pr):
            continue                                            # 强起输了的下一回合交给 ECO-03
        s, u = _side(E, r), E[r]['us']
        full = C.full_buy_line(ctx, s)
        if u['buy'] != 'eco' or u['avg_start'] >= full or not C.can_force(ctx, r):
            continue
        k = _lc(E, r)
        proj = int(u['avg_start'] - th['eco_spend'] + _lb(ctx, k + 1))     # eco 输了下回合人均（推算）
        mp = _them_mp(ctx, E, r)                # 审核单 #23：对面赛点照出，理由换成“没有下回合”
        if not (mp or k <= th['max_k_for_force'] or proj < full):
            continue
        if _won(E, r):
            dbg.append(f'ECO-02 R{r} eco 赢了：不出（和赢下来的回合放一起读着矛盾）')
            continue
        hit_rounds.add(r)
        after_rev = (pr in E and _same_half(ctx, pr, r) and E[pr]['us']['buy'] == '全起' and E[pr]['them']['buy'] == 'eco'
                     and not _won(E, pr))
        streak1 = pr in E and _same_half(ctx, pr, r) and not _won(E, pr) and not (
            pr - 1 in E and _same_half(ctx, pr - 1, r) and not _won(E, pr - 1))
        # 买了又退：谁退了什么（长枪 / 冲锋枪 / 手枪 / 甲）
        refunds = []
        for p in u['players']:
            items = [it for it in _refunded(p) if _item_cn(it)]
            if items:
                sid = C.sid_of(ctx, p['name'])
                order = _first_buy_tick(ctx, r, sid, items)
                refunds.append(dict(name=p['name'], sid=sid, items=sorted(items, key=lambda it: (_item_order(it), items.index(it))),
                                    big=any(it in ARMOR_CN or _tier(it) >= 1 for it in items), order=order))
        uname = _user_name(ctx)
        refunds.sort(key=lambda x: (x['name'] == uname, x['order'] if x['order'] is not None else 10 ** 9, x['name']))
        n_big = sum(1 for x in refunds if x['big'])
        # 时刻：你最后一次退钱；你没退就用队友第一次退长枪 / 甲（退回 >= refund_min）；都没有就用买枪结束
        kt = _tl(ctx)[r]['key_ticks']
        tick = int(kt['buy_end'])
        if n_big >= th['refund_people']:
            rt = _refund_ticks(ctx, r, {x['sid'] for x in refunds})
            if rt:
                mine = [tk for sid, tk, d in rt if sid == C.user_sid(ctx)]
                mates = [tk for sid, tk, d in rt if sid != C.user_sid(ctx) and d >= th['refund_min']]
                if mine or mates:
                    tick = max(mine) if mine else min(mates)
        t = C.t_of(ctx, tick, r)
        # 句子
        nx_word = '也全起不了' if proj < full else ('只够勉强起枪、道具少' if proj < full + th['comfort'] else '才刚够全起')
        sit = '反 eco 被翻后' if after_rev else '全起不了的回合'
        if mp:
            why = '对面赛点，这回合输了就没有下回合'
        elif k <= th['max_k_for_force']:
            why = f'{sit}{"刚输一回合、" if streak1 else ""}输局补偿还不高，eco 输了下回合{nx_word}'
        else:
            why = f'{sit}，eco 输了下回合{nx_word}'
        if n_big >= th['refund_people']:
            bits = []
            for i, x in enumerate(refunds):
                w = _who(ctx, x['name'])
                its = _join(_item_cn(it) for it in x['items'])
                bits.append(f'{w}先买了{its}又退掉' if i == 0 else f'{w}退了{its}')    # 空格由 _tidy 补
            text = f'买枪阶段 {"，".join(bits)}，最后全队 eco；{why}'
        else:
            text = f'全队钱够强起，最后 eco；{why}'
        # 还可以：强起再输 → 下回合 eco → 再下回合全起 成立时才加“三回合里两回合有枪”
        force_cost = C.force_line(ctx, s)
        proj2 = u['avg_start'] - force_cost + _lb(ctx, k + 1) - th['eco_spend'] + _lb(ctx, k + 2)
        if mp:
            alt = '还可以有多少钱起多少、买了不退：对面赛点时 eco 攒下的钱用不上，高分局和职业在对面赛点都不 eco'
        else:
            alt = '还可以按强起买、买了不退：这时 eco 换不来下回合的真全起，高分局和职业在这种处境几乎都强起'
            if proj2 >= full:
                alt += '；强起再输就下回合 eco、再下回合全起，三回合里两回合有枪'
        f = _round_facts(E, r, 'moment')
        f.update(loss_counter=k, proj_if_eco_loss=proj, full_line=full, comfort_line=full + th['comfort'],
                 after_reverse_eco=after_rev, refunds=[dict(name=x['name'], items=[_item_cn(i) for i in x['items']]) for x in refunds],
                 proj_force_loss_eco_full=int(proj2), t=C.sec(t), them_match_point=mp)
        ev = f'开局人均 {C.money(u["avg_start"])}，{sum(1 for p in u["players"] if p["start"] >= force_cost)} 人的钱够强起'
        H.append(_hit(ctx, 'ECO-02', '问题', r, 'moment', TEAM, '队友或团队', f, text, alt, ev, t=t, tick=tick))
        # 经济栏：另一种思路
        fi = dict(f, slot='economy.idea')
        H.append(_hit(ctx, 'ECO-02', '思路', r, 'round', TEAM, '队友或团队', fi,
                      f'另一种思路是强起（{FORCE_GUNS[s]}加头盔甲），因为'
                      + ('对面赛点，这回合输了就没有下回合' if mp else f'eco 输了下回合也{nx_word.removeprefix("也")}'), '', ev))
        # 回合主因的经济部分（这回合输了）
        m = E[r]['them']
        them_guns = [x for x in (f'{m["n_rifle"]} 把步枪' if m['n_rifle'] else '', f'{m["n_awp"]} 把 AWP' if m['n_awp'] else '',
                                 f'{m["n_smg"]} 把冲锋枪' if m['n_smg'] else '') if x]
        fm = dict(f, slot='main_cause', them_rifle=m['n_rifle'], them_awp=m['n_awp'], them_smg=m['n_smg'])
        head = '买枪阶段买了又退，最后全队 eco' if n_big >= th['refund_people'] else '全队钱够强起，最后 eco'
        H.append(_hit(ctx, 'ECO-02', '判定', r, 'round', TEAM, '队友或团队', fm,
                      f'{head}，拿手枪打对面{"、".join(them_guns) if them_guns else "全起"}', '', ev))
    return hit_rounds


# ================================================================ ECO-03 强起输了下回合
def eco03(ctx, E, H, dbg):
    for r in sorted(E):
        n = r + 1
        if E[r]['us']['buy'] not in ('强起', '半起') or _won(E, r) or n not in E or not _same_half(ctx, r, n) \
                or _last_of_half(ctx, E, n):
            continue
        s, u = _side(E, n), E[n]['us']
        b = u['buy']
        if b == 'eco':
            dbg.append(f'ECO-03 R{n} 上回合{E[r]["us"]["buy"]}输了、这回合 eco：买法没问题，不出')
            continue
        full = C.full_buy_line(ctx, s)
        if u['avg_start'] >= full or b not in ('强起', '半起', '全起'):
            continue
        if _won(E, n):
            dbg.append(f'ECO-03 R{n} 又起了枪并且赢了：不出（和赢下来的回合放一起读着矛盾）')
            continue
        if _them_mp(ctx, E, n):
            dbg.append(f'ECO-03 R{n} 对面赛点，没有下回合：不出“eco 一回合攒钱”（审核单 #23）')
            continue
        # 审核单 #23：“eco 一回合、下回合真全起”按实测连败计数核一遍（这回合 eco 输了下回合人均够不够全起线）
        proj = int(u['avg_start']) - int(_th(ctx)['eco_spend']) + _loss_reward(ctx, E, n)
        if proj < full:
            dbg.append(f'ECO-03 R{n} eco 了下回合人均也只有 {proj}，不到全起线：不出')
            continue
        f = _round_facts(E, n, 'economy.idea')
        f.update(prev_buy=E[r]['us']['buy'], full_line=full, next_avg_if_eco=proj, loss_counter=_lc(E, n))
        what = '又硬起了一套' if b == '全起' else '又起了半套'
        text = f'上回合{E[r]["us"]["buy"]}输了，这回合钱不够全起，{what}'
        # 完整性复核二：上回合是半起时不写“强起输了”（bd R19：R18 经济栏写的是半起）
        alt = f'还可以 eco 一回合攒钱，下回合真全起；高分局和职业{E[r]["us"]["buy"]}输了，下回合多数 eco'
        ev = f'开局人均 {C.money(u["avg_start"])}，全起线 {C.money(full)}'
        H.append(_hit(ctx, 'ECO-03', '思路', n, 'round', TEAM, '队友或团队', f, text, alt, ev))


# ================================================================ ECO-04 钱够长枪却拿冲锋枪 / 余钱没发枪 / 你自己没买
def _self_nobuy(ctx, E, r):
    """审核单 #22（手册 ECON-C）：全队起了枪（全起 / 半起）、你开局的钱够步枪加甲，却没有主武器、也没买长枪。
    开局 >= 步枪 + 甲，或 >= 便宜步枪（Galil / 法玛斯）+ 甲 且这回合输了下一档就是最大一档。
    返回 dict（ECO-04 (c) 用、COST-01 据此不出），不成立返回 None"""
    th = _th(ctx)
    if r not in E or _pistol(ctx, r):
        return None
    u, m = E[r]['us'], E[r]['them']
    if u['buy'] not in th['nobuy_team_buys'] or m['buy'] == 'eco':
        return None
    me = next((p for p in u['players'] if p.get('user') or p['name'] == _user_name(ctx)), None)
    if me is None or me.get('primary') or any(_tier(it) >= 1 for it in _bought(me)):
        return None
    # 队友真的起了枪（主武器是冲锋枪及以上）：全队半起其实是“甲 + 手枪”的那种不算（461d R8、51920faa R5：全队都只有手枪）
    mates = [p for p in u['players'] if p is not me]
    n_long = sum(1 for p in mates if _tier(p.get('primary')) >= 1)
    if n_long < int(th['nobuy_min_mates_long']):
        return None
    s = _side(E, r)
    start = int(me.get('start') or 0)
    full_line = th['rifle_price'][s] + th['vest_price']
    cheap_line = th['cheap_rifle_price'][s] + th['vest_price']
    reward = _loss_reward(ctx, E, r)
    capped = reward >= _lb_max(ctx)
    if start >= full_line:
        gun = 'AK' if s == 'T' else 'M4'
        line = full_line
    elif start >= cheap_line and capped:
        gun = th['cheap_rifle_name'][s]
        line = cheap_line
    else:
        return None
    held = [x for x in (C.weapon_cn(me['pistol']) if me.get('pistol') else '',
                        '头盔甲' if me.get('helmet') else ('甲' if me.get('armor') else '')) if x]
    return dict(name=me['name'], start=start, left=int(me.get('left') or 0), gun=gun, line=line, held=held,
                capped=capped, loss_reward=reward, loss_counter=_lc(E, r), team_buy=u['buy'], n_mates_long=n_long,
                all_mates_long=n_long == len(mates))


def _them_guess(ctx, E, r):
    """审核单 #36（手册 ECON-F）：买枪时推得出来的“对面这回合大概率全起”的理由（上回合结果 / 对面上回合 eco /
    对面输局补偿拿满）；对面实际不是全起、或者推不出来时返回 ''"""
    th = _th(ctx)
    pr = r - 1
    if E[r]['them']['buy'] != '全起' or pr not in E or not _same_half(ctx, pr, r):
        return ''
    if not _won(E, pr):
        return '对面上回合赢了，这回合大概率全起'
    if E[pr]['them']['buy'] == 'eco':
        return '对面上回合 eco，这回合大概率全起'
    if _lc(E, r, 'them') >= int(th['them_guess_k']):
        return '对面连输了几回合、输局补偿拿满，这回合大概率全起'
    return ''


def eco04(ctx, E, H, dbg):
    th = _th(ctx)
    seen = {}                                   # (半场, 名字) → 第一次 (a) 的回合
    merged = {}                                 # (半场, 名字) → [后面合并到总评的回合]
    for r in sorted(E):
        if _pistol(ctx, r) or _second(ctx, r):
            continue
        u, m = E[r]['us'], E[r]['them']
        nb = _self_nobuy(ctx, E, r)
        if (u['buy'] != '全起' or m['buy'] == 'eco') and nb is None:
            continue
        s = _side(E, r)
        line = th['rifle_price'][s] + th['vest_price'] + th['smg_margin']
        afford, short, rich = [], [], []
        for p in sorted(u['players'] if u['buy'] == '全起' and m['buy'] != 'eco' else [], key=lambda x: x['name']):
            prim = p.get('primary')
            cls = C.weapon_class(prim) if prim else None
            if cls == '冲锋枪' and prim in _bought(p) and p['start'] >= line:
                afford.append(p)
            elif (prim is None or cls == '冲锋枪') and p['start'] < line and not (nb and p['name'] == nb['name']):
                short.append(p)
            if p['left'] >= th['rifle_price'][s] and prim and _tier(prim) >= 2:
                rich.append(p)
        rich.sort(key=lambda x: (-x['left'], x['name']))
        short.sort(key=lambda x: (x['start'], x['name']))
        pairs = list(zip(rich, short))
        written = []
        for p in afford:
            key = (_half(ctx, r), p['name'])
            if key in seen:
                merged.setdefault(key, []).append(r)
            else:
                seen[key] = r
                written.append(p)
        if not afford and not pairs and nb is None:
            continue
        # 时刻：(a) 那把冲锋枪的购买 tick（含合并到总评的人），没有 (a) 用买枪结束
        kt = _tl(ctx)[r]['key_ticks']
        ticks = [tk for tk in (_buy_tick(ctx, r, C.sid_of(ctx, p['name']), p['primary']) for p in afford) if tk is not None]
        tick = min(ticks) if ticks else int(kt['buy_end'])
        if not written and not pairs and nb is None:
            dbg.append(f'ECO-04 R{r} 只有本半场已经写过的人（{"、".join(p["name"] for p in afford)}）：并进总评')
            continue
        t = C.t_of(ctx, tick, r)
        rifle = 'M4' if s == 'CT' else 'AK'
        enemy_rifle = 'AK' if s == 'CT' else 'M4'
        bits = [f'{_who(ctx, p["name"])} 的钱够买 {rifle} 加甲，拿的是 {C.weapon_cn(p["primary"])}' for p in written]
        bits += [f'{_who(ctx, a["name"])} 买完余钱还够给 {_who(ctx, b["name"])} 发一把枪' for a, b in pairs]
        if nb:                                  # (c) 审核单 #22：全队起了枪，你自己钱够却只拿手枪
            mates_w = '队友都起了枪' if nb['all_mates_long'] else f'{nb["n_mates_long"]} 个队友起了枪'
            got = f'只拿了{_join(nb["held"])}' if nb['held'] else '什么都没买'
            bits.insert(0, f'{mates_w}，{_who(ctx, nb["name"])}的钱够买 {nb["gun"]} 加甲，{got}')
        n = len(written) + len(pairs) + (1 if nb else 0)
        # 审核单 #36（手册 ECON-F）：句尾理由只用买枪时推得出来的（上回合结果 / 对面上回合 eco / 对面输局补偿拿满），
        # 推不出来、或者对面实际没全起时写“远处对枪更稳”
        guess = _them_guess(ctx, E, r)
        tail_w = f'{guess}，拿步枪更稳' if guess else '远处对枪更稳'
        text = f'买法另一种思路：{"；".join(bits)}，这回合能多 {n} 把步枪；{tail_w}'
        if nb and nb['capped']:
            text += f'；就算这回合输了，下回合也有 {C.money(nb["loss_reward"])} 输局补偿'
        alt = '还可以钱够的都起步枪，钱多的给钱少的发枪：对面拿长枪时，冲锋枪远距离吃亏'
        if nb and not written and not pairs:
            alt = '还可以全队起枪时跟着起步枪：一个人拿手枪跟着长枪一起打，等于全队少一把枪'
        subj = written[0]['name'] if written else (pairs[0][0]['name'] if pairs else nb['name'])
        f = _round_facts(E, r, 'economy.idea')
        f.update(t=C.sec(t), n_more_rifles=n, line=line, them_guess=guess or None,
                 them_won_prev=bool(r - 1 in E and _same_half(ctx, r - 1, r) and not _won(E, r - 1)),
                 them_loss_counter=_lc(E, r, 'them'),
                 afford=[dict(name=p['name'], start=p['start'], primary=C.weapon_cn(p['primary'])) for p in written],
                 merged=[p['name'] for p in afford if p not in written],
                 drop=[dict(giver=a['name'], left=a['left'], to=b['name'], to_start=b['start']) for a, b in pairs],
                 self_nobuy=nb)
        ev_bits = [f'{_who(ctx, p["name"])} 开局 {C.money(p["start"])}' for p in written]
        ev_bits += [f'{_who(ctx, a["name"])} 买完还剩 {C.money(a["left"])}' for a, _ in pairs]
        if nb:
            ev_bits.insert(0, f'{_who(ctx, nb["name"])} 开局 {C.money(nb["start"])}，买完还剩 {C.money(nb["left"])}')
            if not written and not pairs:
                f['branch'] = 'self_nobuy'
        blame = '—' if subj == _user_name(ctx) else '队友或团队'
        H.append(_hit(ctx, 'ECO-04', '思路', r, 'moment', subj, blame, f, text, alt, '，'.join(ev_bits), t=t, tick=tick))
    # 同一个人本半场后面的 (a) 并进总评
    for (h, name), rs in sorted(merged.items()):
        first = seen[(h, name)]
        allr = [first] + rs
        f = dict(slot='summary.team', rounds=allr, name=name)
        text = f'{_who(ctx, name)}长枪局常拿冲锋枪（第 {"、".join(str(x) for x in allr)} 回合钱都够步枪加甲）'
        alt = '还可以钱够的都起步枪：对面拿长枪时，冲锋枪远距离吃亏'
        H.append(_hit(ctx, 'ECO-04', '思路', first, 'match', name, '—' if name == _user_name(ctx) else '队友或团队',
                      f, text, alt, ''))


# ================================================================ ECO-05 eco 打全起输 = 装备差距
def eco05(ctx, E, H, dbg, eco02_rounds):
    for r in sorted(E):
        u, m = E[r]['us'], E[r]['them']
        if u['buy'] != 'eco' or m['buy'] != '全起' or _won(E, r) or r in eco02_rounds:
            continue
        guns = Counter()
        for p in u['players']:
            g = p.get('primary') or p.get('pistol')
            if g:
                guns[C.weapon_cn(g)] += 1
        gl = '、'.join(g for g, _ in sorted(guns.items(), key=lambda x: (-x[1], x[0])))
        n, na = int(u['n']), int(u['n_armor'])
        arm = '全员没甲' if na == 0 else ('都有甲' if na >= n else ('大多有甲' if na * 2 > n else f'只有 {na} 人有甲'))
        awp = '带 AWP' if m['n_awp'] else ''
        f = _round_facts(E, r, 'main_cause')
        f.update(n_armor=na, them_awp=m['n_awp'], guns=gl)
        text = f'eco 只有{gl}，{arm}，打对面全起{awp}，回合输主要是装备差距'
        ev = f'对面 {m["n_rifle"]} 把步枪、{m["n_awp"]} 把 AWP、{m["n_smg"]} 把冲锋枪'
        f.update(them_rifle=m['n_rifle'], them_smg=m['n_smg'])
        H.append(_hit(ctx, 'ECO-05', '判定', r, 'round', TEAM, '合理代价', f, text, '', ev))


# ================================================================ ECO-06 反 eco 被翻（势头·坏）
def _carried(ctx, E, r, who):
    """r+1 回合 who 一方带进来的、不是自己买的枪（保留装备 >= $1000、r 回合开局还没有这把枪、不是队友这回合买来发的）"""
    n = r + 1
    if n not in E or not _same_half(ctx, r, n):
        return []
    prev = {p['name']: p for p in E[r][who]['players']}
    # 上回合这一方自己就有的枪（谁拿着都算：队友死了别人捡回来、发给队友的）不是从对面捡的（128 R10：MP9 是对面自己的）
    prev_team = Counter(p.get('primary') for p in E[r][who]['players'] if p.get('primary'))
    team_bought = Counter()                                     # 这回合买来发给队友的枪（买了、但不是自己手上那把）
    for p in E[n][who]['players']:
        own = [it for it in _bought(p) if _tier(it) >= 1]
        if p.get('primary') in own:
            own.remove(p['primary'])
        team_bought.update(own)
    out = []
    for p in sorted(E[n][who]['players'], key=lambda x: x['name']):
        g = p.get('primary')
        if not g or _tier(g) < 1 or g in _bought(p) or p.get('kept', 0) < 1000:
            continue
        if (prev.get(p['name']) or {}).get('primary') == g:
            prev_team[g] -= 1
            continue                                            # 上回合自己就拿着（保下来的枪）
        if prev_team[g] > 0:
            prev_team[g] -= 1
            continue
        if team_bought[g] > 0:                                  # 队友这回合买了发给他的
            team_bought[g] -= 1
            continue
        out.append(dict(name=p['name'], gun=g))
    return out


def _awp_from(ctx, E, r):
    """r 之后本半场对面第一次有 AWP 的回合 F，并且 F 之后本半场 >= awp_share 的回合都有；不成立返回 None"""
    rs = [x for x in _half_rounds(ctx, E, r) if x > r]
    F = next((x for x in rs if E[x]['them']['n_awp'] >= 1), None)
    if F is None:
        return None
    after = [x for x in rs if x >= F]
    share = sum(1 for x in after if E[x]['them']['n_awp'] >= 1) / len(after)
    return F if share >= _th(ctx)['awp_share'] else None


def _pistols_only(m):
    """这一方全员只拿手枪（没有步枪、冲锋枪、狙、霰弹 / 机枪）：economy 的 buy 写半起（护甲 + 升级手枪），
    打起来和 eco 一样（e6b R16：Tec-9×4 + 沙鹰，我们 4 把步枪 + AWP 输掉）"""
    n = m.get('n') or 0
    guns = sum(int(m.get(k) or 0) for k in ('n_rifle', 'n_smg', 'n_awp', 'n_heavy', 'n_auto', 'n_scout'))
    return n > 0 and guns == 0 and int(m.get('n_pistol_only') or 0) == n


def eco06(ctx, E, H, dbg):
    th = _th(ctx)
    for r in sorted(E):
        u, m = E[r]['us'], E[r]['them']
        # 决定 Q21：对面半起不算反 eco，按正常回合判；“半起但全员只拿手枪”只在 pistols_only_is_eco 打开时算
        if u['buy'] != '全起' or not (m['buy'] == 'eco' or (th['pistols_only_is_eco'] and _pistols_only(m))) or _won(E, r):
            if u['buy'] == '全起' and m['buy'] != 'eco' and _pistols_only(m) and not _won(E, r):
                dbg.append(f'ECO-06 R{r} 对面{m["buy"]}（全员手枪）：Q21 半起不算反 eco，不写“被翻”')
            continue
        carried = _carried(ctx, E, r, 'them')
        # 同名的合在一起，和买法一句同一种写法（635 R2：“M4A4、M4A1-S、M4A1-S” → “M4A4、M4A1-S×2”）
        gcnt = Counter(C.weapon_cn(x['gun']) for x in carried)
        gord = list(dict.fromkeys(C.weapon_cn(x['gun']) for x in carried))
        guns = '、'.join(f'{g}×{gcnt[g]}' if gcnt[g] > 1 else g for g in gord)
        parts = []
        n1, n2 = r + 1, r + 2
        nx_word = None
        if n1 in E and _same_half(ctx, r, n1):
            a1, s1 = E[n1]['us']['avg_start'], _side(E, n1)
            b1 = E[n1]['us'].get('buy')
            # 按下回合实际的买法写（635 R2：下回合其实强起了，不能写“只能 eco”）；买法不知道时才按钱推
            if b1 == 'eco':
                nx_word = '只能 eco'
            elif b1 in ('强起', '半起'):
                nx_word = f'钱不够全起，只能{b1}'
            elif b1 == '全起':
                nx_word = None
            else:
                nx_word = '只能 eco' if a1 < C.force_line(ctx, s1) else ('全起不了' if a1 < C.full_buy_line(ctx, s1) else None)
            if nx_word:
                parts.append(f'我们下回合{nx_word}')
        hard = (n2 in E and _same_half(ctx, r, n2) and E[n2]['us']['buy'] == '全起'
                and E[n2]['us']['avg_start'] < C.full_buy_line(ctx, _side(E, n2)) + th['comfort'])
        if hard:
            parts.append('再下回合起得很勉强')
        F = _awp_from(ctx, E, r)
        if F:
            # 半场只剩一两回合时“从第 F 回合开始每回合有狙”读着像整场（128 R10：只说了第 11、12 回合）
            n_after = len([x for x in _half_rounds(ctx, E, r) if x >= F])
            all_awp = all(E[x]['them']['n_awp'] >= 1 for x in _half_rounds(ctx, E, r) if x >= F)
            if n_after >= th['awp_from_min_rounds']:
                parts.append(f'对面从第 {F} 回合开始每回合有狙')
            elif all_awp and F == r + 1:
                parts.append('这个半场剩下的回合对面都有狙')
            else:
                F = None
        head = f'长枪打手枪被翻，对面捡走 {guns}' if guns else '长枪打手枪被翻'
        text = head + ('；' + '，'.join(parts) if parts else '')
        s = _side(E, r)
        if s == 'T' and E[r]['plant']:
            alt = '还可以反 eco 抱团守包、交叉架枪，不给手枪近身和捡枪的机会'
        elif s == 'CT':
            alt = '还可以抱团守点、一起回防，不给手枪近身和捡枪的机会'
        else:
            alt = '还可以反 eco 时抱团一起打，不给手枪近身和捡枪的机会'
        f = _round_facts(E, r, 'summary.momentum')
        f.update(carried=[dict(name=x['name'], gun=C.weapon_cn(x['gun'])) for x in carried], next_word=nx_word,
                 hard_next2=hard, first_awp_round=F,
                 next_avg=E[n1]['us']['avg_start'] if n1 in E else None)
        ev = '；'.join([f'{x["name"]} 下回合拿的 {C.weapon_cn(x["gun"])} 不是自己买的' for x in carried]) or ''
        H.append(_hit(ctx, 'ECO-06', '势头', r, 'match', TEAM, '队友或团队', f, text, alt, ev))


def eco06_awp(ctx, E, H, dbg):
    """ECO-06 的另一支（facts.branch = awp_carry，只进这一回合的经济栏）：输掉的回合我们倒下的人身上的 AWP 被对面捡走、
    带进下一回合（8d R22：队友B 的 AWP 被 对手2 捡走，R23 对面两把 AWP，我们这边钱只够 Galil）。
    跨回合的链条要写出来（用户规则），但这不是“反 eco 被翻”，不进比赛走势"""
    th = _th(ctx)
    pk = None
    try:
        from engine.rules import team as TM
        pk = TM._pickups(ctx)
    except (ImportError, OSError, KeyError):
        pk = None
    if pk is None or not len(pk):
        return
    for r in sorted(E):
        n = r + 1
        if _won(E, r) or n not in E or not _same_half(ctx, r, n):
            continue
        if E[r]['us']['buy'] != '全起' and not any(p.get('primary') == 'AWP' for p in E[r]['us']['players']):
            continue
        info = C.round_info(ctx, r)
        D = C.deaths(ctx, r)
        nxt = {p['name']: p for p in E[n]['them']['players']}
        found = []
        for p in sorted(E[r]['us']['players'], key=lambda x: x['name']):
            if p.get('primary') != 'AWP':
                continue
            sid = C.sid_of(ctx, p['name'])
            d = D[D['vic'] == sid]
            if not len(d):
                continue                                    # 活下来了，枪还在自己手上
            dt = int(d['tick'].iloc[0])
            dpos = (float(d['user_X'].iloc[0]), float(d['user_Y'].iloc[0]), float(d['user_Z'].iloc[0]))
            w = pk[(pk['tick'] >= dt) & (pk['tick'] <= info['end'] + int(th['awp_pick_after_s'] * C.TR))]
            w = w[w['item'].astype(str).str.lower().str.contains('awp')]
            for x in w.itertuples():
                if x.sid not in C.enemies(ctx, r):
                    continue
                if C.dist3((x.user_X, x.user_Y, x.user_Z), dpos) > th['awp_pick_r']:
                    continue
                q = nxt.get(C.name_of(ctx, x.sid))
                if q and q.get('primary') == 'AWP' and 'AWP' not in _bought(q):
                    found.append((p['name'], C.name_of(ctx, x.sid)))
                break
        if not found:
            continue
        n_awp = E[n]['them']['n_awp']
        owner, picker = found[0]
        text = (f'这回合输了，{owner} 的 AWP 被 {picker} 捡走带进下回合'
                + ('，下回合对面两把 AWP' if n_awp == 2 else (f'，下回合对面 {n_awp} 把 AWP' if n_awp > 2 else '')))
        f = _round_facts(E, r, 'economy.swing')
        f.update(branch='awp_carry', owner=owner, picker=picker, next_n_awp=n_awp)
        ev = f'{picker} 下回合拿的 AWP 不是自己买的'
        H.append(_hit(ctx, 'ECO-06', '势头', r, 'round', TEAM, '队友或团队', f, text, '', ev))


# ================================================================ 跨回合经济链（第六轮拍板 5，只进这一回合的经济栏）
def _died(ctx, r):
    """r 回合倒下的人名（击杀栏，含回合结束后被补的）"""
    tl = C.tl_round(ctx, r) or {}
    return {k.get('victim') for k in tl.get('kills') or [] if k.get('victim')}


def _first_awp_of_half(ctx, E, n):
    """n 是不是对面这个半场第一次有 AWP（手枪局以外）"""
    if E[n]['them'].get('n_awp', 0) < 1:
        return False
    return all(E[x]['them'].get('n_awp', 0) < 1 for x in _half_rounds(ctx, E, n) if x < n)


def _is_awp(w):
    return bool(w) and C.wkey(w) == 'awp'


def _chain_saved(ctx, E, n, parts, branches, xf):
    """审核单 #33 chain_saved：上回合（r = n-1）输了，这个人活下来，这回合主武器还是那把、不是自己买的（kept >= carry_min_kept）。
    AWP 一直写；步枪只在全队 eco / 强起 / 半起时写（全起时带一把步枪进来不值得单独写）。只是事实，不定责"""
    th = _th(ctx)
    r = n - 1
    u, pu = E[n]['us'], E[r]['us']
    dead = _died(ctx, r)
    prevp = {p['name']: p for p in pu['players']}
    low = u['buy'] in ('eco', '强起', '半起')
    saved = []
    for p in sorted(u['players'], key=lambda x: (not x.get('user'), x['name'])):
        q, g = prevp.get(p['name']), p.get('primary')
        if not q or not g or q.get('primary') != g or p['name'] in dead or g in _bought(p):
            continue
        if int(p.get('kept') or 0) < th['carry_min_kept']:
            continue
        if C.is_sniper(g) or (_tier(g) >= 2 and low):
            saved.append(p)
    if not saved:
        return
    items = [f"{_who(ctx, p['name'])}保下的 {C.weapon_cn(p['primary'])}" for p in saved]
    s = f"上回合{'、'.join(items)}带进这回合"
    if low:
        if len(saved) == 1:
            s += f"，全队{u['buy']}时{_who(ctx, saved[0]['name'])}还有一把 {C.weapon_cn(saved[0]['primary'])}"
        else:
            s += f"，全队{u['buy']}时还有 {len(saved)} 把长枪"
    parts.append(s)
    branches.append('chain_saved')
    xf['chain_saved'] = [dict(name=p['name'], gun=C.weapon_cn(p['primary']), kept=int(p['kept']), start=int(p['start']))
                         for p in saved]


def _chain_rebuy(ctx, E, n, parts, branches, xf):
    """审核单 #33 chain_rebuy：上回合（r = n-1）输了，这个人倒下时拿着 AWP，这回合自己又买了 AWP；买完余钱 < 一把步枪，
    并且没买头盔，或者有队友拿冲锋枪 / 手枪、开局钱 < 步枪 + 甲 + smg_margin（ECO-04 同一条线）→“重买后钱紧”。
    上回合丢 AWP 已经写在 chain_lost 里时不重复“AWP 没保下来”。只是事实，不定责"""
    th = _th(ctx)
    r = n - 1
    side = _side(E, n)
    u, pu = E[n]['us'], E[r]['us']
    dead = _died(ctx, r)
    prevp = {p['name']: p for p in pu['players']}
    line = th['rifle_price'][side] + th['vest_price'] + th['smg_margin']
    out = []
    for p in sorted(u['players'], key=lambda x: (not x.get('user'), x['name'])):
        q = prevp.get(p['name'])
        if not q or not _is_awp(q.get('primary')) or p['name'] not in dead:
            continue
        if not any(_is_awp(it) for it in _bought(p)) or not _is_awp(p.get('primary')):
            continue
        left = int(p.get('left') or 0)
        if left >= th['rifle_price'][side]:
            continue
        short = [x for x in sorted(u['players'], key=lambda x: (int(x.get('start') or 0), x['name']))
                 if x['name'] != p['name'] and int(x.get('start') or 0) < line
                 and (not x.get('primary') or C.weapon_class(x['primary']) == '冲锋枪')]
        no_helmet = not p.get('helmet')
        if not short and not no_helmet:
            continue
        out.append((p, short, no_helmet, left))
    if not out:
        return
    p, short, no_helmet, left = out[0]
    w = _who(ctx, p['name'])
    said = 'chain_lost' in branches and any(_is_awp(q.get('primary')) for q in pu['players'] if q['name'] in dead)
    s = f'{w}这回合重买 AWP 后钱紧' if said else f'上回合{w}的 AWP 没保下来，这回合重买后钱紧'
    bits = (['没买头盔'] if no_helmet else []) + ([f"{'也' if no_helmet else ''}没钱给 {'、'.join(_who(ctx, x['name']) for x in short)} 发枪"]
                                                 if short else [])
    parts.append(s + '，' + '，'.join(bits))
    branches.append('chain_rebuy')
    xf['chain_rebuy'] = dict(name=p['name'], start=int(p['start']), left=left, helmet=bool(p.get('helmet')),
                             short=[dict(name=x['name'], start=int(x['start']),
                                         primary=C.weapon_cn(x['primary']) if x.get('primary') else None) for x in short])


def eco_chain(ctx, E, H, dbg):
    """上一回合怎么打完，解释了这一回合的经济局面时，在这一回合的经济栏写出来（用户规则：买法没问题、上回合也解释不了什么的不写）：
      chain_lost  上回合输了，长枪 / AWP 没保下来（倒下的人身上的）>= chain_lost_min 把或者有 AWP，这回合只能 eco / 强起，
                  或者有人钱不够起步枪（开局钱 < 步枪 + 甲 + smg_margin、拿的是冲锋枪 / 手枪）；
      chain_bonus 上回合对面又输了、连败补偿刚拿满，这回合对面全起（上回合没全起），或者第一次起狙；
      chain_pick  上回合捡了对面的 AWP 带进这回合（这回合我们多一把狙）；我们 eco / 强起时捡来的步枪也写；
      chain_force 上回合我们 eco / 强起赢下对面全起，对面这回合只能 eco / 强起；
      chain_saved 审核单 #33：上回合输了，保下来的 AWP（或全队 eco / 强起 / 半起时保下来的步枪）带进这回合（facts.chain_saved）；
      chain_rebuy 审核单 #33：上回合输了、AWP 没保下来，这回合重买 AWP 后钱紧：没买头盔 / 没钱给拿冲锋枪的队友发枪（facts.chain_rebuy）。
    facts.slot = economy.swing、branch 如上；compose 只把它放进经济栏，不进标题、走势、标签"""
    th = _th(ctx)
    max_k = max(int(k) for k in th['loss_bonus'])
    for n in sorted(E):
        r = n - 1
        if r not in E or not _same_half(ctx, r, n) or _pistol(ctx, n):
            continue
        side = _side(E, n)
        u, m, pu, pm = E[n]['us'], E[n]['them'], E[r]['us'], E[r]['them']
        parts, branches, ev, xf = [], [], [], {}
        # ---- 上回合丢了长枪 → 这回合钱紧
        if not _won(E, r):
            dead = _died(ctx, r)
            lost = [p.get('primary') for p in pu['players'] if p['name'] in dead and _tier(p.get('primary')) >= 2]
            n_awp = sum(1 for g in lost if C.is_sniper(g))
            n_rifle = len(lost) - n_awp
            if n_awp or len(lost) >= th['chain_lost_min']:
                # 钱不够 = 开局钱连 AK / M4 加甲都不够（不加 smg_margin：8d R21 队友B 3400 够 AK 加甲）；
                # 只算上回合自己倒下、身上的长枪没保下来的人（bd R19：上回合本来就拿冲锋枪的人钱紧不是这次丢枪造成的）
                need = th['rifle_price'][side] + th['vest_price']
                lost_own = {p['name'] for p in pu['players'] if p['name'] in dead and _tier(p.get('primary')) >= 2}
                tight = [p for p in sorted(u['players'], key=lambda x: x['name'])
                         if int(p.get('start') or 0) < need and _tier(p.get('primary')) <= 1 and p['name'] in lost_own]
                eff = ''
                full = int(u.get('avg_start') or 0) >= C.full_buy_line(ctx, side)
                if u['buy'] == 'eco' and not full:
                    # 钱够强起时不写“只够 eco”（和经济栏“这回合也能强起”的另一种思路矛盾，8d R16）
                    eff = '这回合钱不够全起' if C.can_force(ctx, n) else '这回合只够 eco'
                elif u['buy'] in ('强起', '半起') and not full:
                    eff = f"这回合钱不够全起，只能{u['buy']}"
                elif tight:
                    guns = '、'.join(dict.fromkeys(C.weapon_cn(p.get('primary') or p.get('pistol') or '') for p in tight))
                    rifle = 'AK' if side == 'T' else 'M4'
                    eff = f"这回合 {'、'.join(_who(ctx, p['name']) for p in tight)} 钱不够起 {rifle} 加甲，拿的是{guns}"
                if eff:
                    what = '和'.join(x for x in (('AWP' if n_awp == 1 else f'{n_awp} 把 AWP') if n_awp else '',
                                                 f'{n_rifle} 把步枪' if n_rifle else '') if x)
                    parts.append(f"上回合输了，{what}{'都' if len(lost) > 1 else ''}没保下来，{eff}")
                    branches.append('chain_lost')
                    ev.append(f'上回合倒下的人身上的长枪 {len(lost)} 把')
        # ---- 审核单 #33（手册 ECON-I）：上回合输了，保下的枪带进这回合 / 丢了 AWP 这回合重买后钱紧
        if not _won(E, r):
            _chain_saved(ctx, E, n, parts, branches, xf)
            _chain_rebuy(ctx, E, n, parts, branches, xf)
        # ---- 上回合对面输了、连败补偿拿满 → 对面这回合全起 / 第一次起狙
        lc_n, lc_r = int((E[n].get('loss_counter') or {}).get('them') or 0), int((E[r].get('loss_counter') or {}).get('them') or 0)
        if _won(E, r) and lc_n >= max_k > lc_r and m['buy'] == '全起':
            awp = _first_awp_of_half(ctx, E, n)
            if awp or pm['buy'] != '全起':
                who = [p['name'] for p in sorted(m['players'], key=lambda x: x['name']) if C.is_sniper(p.get('primary'))]
                tail = (f"第一次起了狙（{'、'.join(who)}）" if awp and who else '全起了')
                parts.append(f'上回合对面又输了，连败补偿拿满，这回合对面{tail}')
                branches.append('chain_bonus')
        # ---- 上回合捡了对面的枪
        carried = _carried(ctx, E, r, 'us')
        c_awp = [x for x in carried if C.is_sniper(x['gun'])]
        c_rifle = [x for x in carried if not C.is_sniper(x['gun']) and _tier(x['gun']) >= 2]
        if c_awp:
            parts.append(f"上回合 {'、'.join(_who(ctx, x['name']) for x in c_awp)} 捡了对面的 AWP，这回合我们多一把狙")
            branches.append('chain_pick')
        elif c_rifle and u['buy'] in ('eco', '强起', '半起'):
            gs = '、'.join(dict.fromkeys(C.weapon_cn(x['gun']) for x in c_rifle))
            parts.append(f"这回合 {'、'.join(_who(ctx, x['name']) for x in c_rifle)} 拿着上回合捡来的 {gs}")
            branches.append('chain_pick')
        # ---- 上回合低配赢下全起 → 对面这回合只能 eco / 强起
        if _won(E, r) and pu['buy'] in BUY_WORD and pm['buy'] == '全起' and m['buy'] in ('eco', '强起', '半起'):
            parts.append(f"上回合我们{BUY_WORD[pu['buy']]}赢下对面全起，对面这回合只能{m['buy']}")
            branches.append('chain_force')
        if not parts:
            continue
        good = all(b in ('chain_pick', 'chain_force', 'chain_saved') for b in branches)
        f = _round_facts(E, n, 'economy.swing')
        f.update(branch='chain', chains=branches, lost_rounds=[r])
        f.update(xf)
        H.append(_hit(ctx, 'ECO-07' if good else 'ECO-06', '势头', n, 'round', TEAM, '—', f, '；'.join(parts), '',
                      '；'.join(ev)))


# ================================================================ ECO-07 低配赢全起（势头·好）
BUY_WORD = {'eco': 'eco', '强起': '强起', '半起': '半起'}


def eco07(ctx, E, H, dbg):
    th_c = ctx.th['common']
    out = []
    for r in sorted(E):
        u, m = E[r]['us'], E[r]['them']
        if u['buy'] not in BUY_WORD or m['buy'] != '全起' or not _won(E, r) or _last_of_half(ctx, E, r) or r + 1 not in E:
            continue
        n = 0                                                   # 本半场在 r 之前连输几回合
        x = r - 1
        while x in E and _same_half(ctx, x, r) and not _won(E, x):
            n += 1
            x -= 1
        bw = BUY_WORD[u['buy']]
        if n >= 2:
            title = f'连输 {n} 回合后{bw}翻盘，止住连败'
        elif _pistol(ctx, r - 1) and r - 1 in E and not _won(E, r - 1):
            title = f'手枪局输了后{bw}赢下'
        else:
            title = f'{bw}翻盘，打断对面经济'
        carried = _carried(ctx, E, r, 'us')
        text = title + (f'，还带了 {len(carried)} 把捡来的枪进下一回合' if carried else '')
        nb = E[r + 1]['them']['buy']
        tail = ''
        if nb == 'eco':
            tail = '：对面下回合只能 eco'
            if not _last_of_half(ctx, E, r + 1) and E[r + 1]['them']['if_loss_next']['avg'] < C.awp_line(ctx):
                tail += '，再拿下一回合，对面接下来也很难起狙'
        elif nb in ('强起', '半起'):
            tail = f'：对面下回合只能{nb}'
        text += tail
        f = _round_facts(E, r, 'summary.momentum')
        f.update(streak=n, carried=[dict(name=c['name'], gun=C.weapon_cn(c['gun'])) for c in carried], n_carried=len(carried),
                 them_next_buy=nb, them_next_if_loss_avg=E[r + 1]['them']['if_loss_next']['avg'], awp_line=int(th_c['awp_need']))
        ev = '；'.join(f'{_who(ctx, c["name"])} 下回合的 {C.weapon_cn(c["gun"])} 是捡来的' for c in carried)
        H.append(_hit(ctx, 'ECO-07', '势头', r, 'match', TEAM, '—', f, text, '', ev))
        out.append((r, bw))
    return out


# ================================================================ ECO-08 满起一回合下包被拆 / 被打光（势头·坏）
def eco08(ctx, E, H, dbg):
    th = _th(ctx)
    TL = _tl(ctx)
    for r in sorted(E):
        if _side(E, r) != 'T' or not E[r]['plant'] or _won(E, r) or r not in TL:
            continue
        if E[r]['them']['buy'] in ('eco', '手枪局') or E[r]['us']['buy'] == '手枪局':
            continue
        a, b = _score(E, r)
        if abs(a - b) > th['score_diff_max']:
            continue
        half_t = [x for x in _half_rounds(ctx, E, r) if x <= r and _side(E, x) == 'T' and x in TL]
        eqs = {x: TL[x]['econ']['equip_freeze_end']['us']['sum'] for x in half_t}
        mx, eq = max(eqs.values()), eqs[r]
        if eq < th['equip_frac'] * mx:
            continue
        u = E[r]['us']
        guns = _gun_count_text([p.get('primary') for p in u['players']])
        best = eq >= mx
        word = '被拆' if E[r]['reason'] == 'bomb_defused' else '被打光'
        ud, pl = TL[r].get('user_death'), TL[r].get('plant')
        dead_before = bool(ud and pl and not ud.get('post_round') and ud['t'] < pl['t'])
        inner = f'{guns}、{u["n_nades"]} 颗道具' if guns else f'{u["n_nades"]} 颗道具'
        text = (f'{a}:{b} 时，T 方全队满起（{inner}{"，是这半场到这回合装备最好的一回合" if best else ""}）'
                f'下了包{word}，比分变 {a}:{b + 1}' + ('；你下包前已经阵亡' if dead_before else ''))
        f = _round_facts(E, r, 'summary.momentum')
        f.update(equip_sum=eq, half_max=mx, best=best, n_nades=u['n_nades'], reason=E[r]['reason'], score_after=f'{a}:{b + 1}',
                 user_dead_before_plant=dead_before, user_death_t=C.sec(ud['t']) if ud else None,
                 plant_t=C.sec(pl['t']) if pl else None,
                 blame_you_if=['POST-03', 'POST-04', 'POS-01'])   # 这回合这几条判了你的责任时，merge 把责任改成“你的责任”
        ev = f'冻结结束装备总值 {C.money(eq)}'
        if dead_before:
            ev += f'；你第 {C.fmt_sec(ud["t"])} 秒阵亡，第 {C.fmt_sec(pl["t"])} 秒才下包'
        H.append(_hit(ctx, 'ECO-08', '势头', r, 'match', TEAM, '队友或团队', f, text, '', ev))


# ================================================================ ECO-09 经济转折点（总评）
def eco09(ctx, E, H, dbg, eco07_hits):
    th = _th(ctx)
    hr = th['half_rounds']
    last = max(E)
    fa, fb = _score_after(E, last)
    seen_half = set()
    for r in sorted(E):
        h = _half(ctx, r)
        if h in seen_half:
            continue
        u, m = E[r]['us'], E[r]['them']
        a, b = _score(E, r)
        if u['buy'] != '全起' or m['buy'] not in ('eco', '强起', '半起') or _won(E, r) or a < b:
            continue
        rs = _half_rounds(ctx, E, r)
        nxt = [x for x in rs if r < x <= r + th['crash_window']]
        # 缺钱的回合：eco / 强起 / 半起，或者全起但钱只够硬起、还输了；全起赢下的不算（128 R19：M4A1-S×4 全起赢下）
        poor = [x for x in nxt if E[x]['us']['buy'] in ('eco', '强起', '半起')
                or (E[x]['us']['buy'] != '全起'
                    and E[x]['us']['avg_start'] < C.full_buy_line(ctx, _side(E, x)) + th['comfort'])
                or (E[x]['us']['buy'] == '全起' and not _won(E, x)
                    and E[x]['us']['avg_start'] < C.full_buy_line(ctx, _side(E, x)) + th['comfort'])]
        rest = [x for x in rs if x >= r + 2]
        awp = [x for x in rest if E[x]['them']['n_awp'] >= 1]
        if len(poor) < th['crash_min_poor'] or not rest or len(awp) / len(rest) < th['awp_share']:
            continue
        seen_half.add(h)
        what = '反 eco 被翻' if m['buy'] == 'eco' else '长枪打低配输了'
        later = [x for x in rs if x > r]
        # 领先真的丢了吗：这回合输掉之后，本半场有没有哪一回合打完时我们不再领先
        lead_lost = any(x <= y for x, y in [_score_after(E, r)] + [_score_after(E, x) for x in later])
        finished = max(fa, fb) >= hr + 1 or (fa == fb == hr)
        res = ('赢下' if fa > fb else '输掉' if fa < fb else '打平') if finished else ''
        # 总评：领先没丢、或者整场赢了，就不是“经济崩盘”（128：7:5 领先、第 17 回合输了，最终 13:11 赢下）——总结用模板标题
        if lead_lost and not (finished and fa > fb):
            h1 = _score(E, hr + 1) if hr + 1 in E else None
            fin = f'最终 {fa}:{fb}' + (f' {res}' if res else '')
            if h1 and h == 1:
                lead = '领先' if h1[0] > h1[1] else ('落后' if h1[0] < h1[1] else '打平')
                head = f'上半场 {h1[0]}:{h1[1]} {lead}，第 {r} 回合{what}后经济崩盘，{fin}'
            elif h1 and h == 0:
                head = f'第 {r} 回合{what}后经济崩盘，上半场打成 {h1[0]}:{h1[1]}，{fin}'
            else:
                head = f'第 {r} 回合{what}后经济崩盘，{fin}'
            f = dict(slot='summary.headline', turn=r, poor_rounds=poor, awp_rounds=awp, final=f'{fa}:{fb}',
                     half_score=f'{h1[0]}:{h1[1]}' if h1 else None, score_before=E[r]['score_before'])
            ev = f'第 {r} 回合开局 {a}:{b}；之后 {len(poor)} 个回合缺钱，对面 {len(awp)} 个回合有 AWP'
            H.append(_hit(ctx, 'ECO-09', '势头', r, 'match', TEAM, '队友或团队', f, head, '', ev))
        ev = f'第 {r} 回合开局 {a}:{b}；之后 {len(poor)} 个回合缺钱，对面 {len(awp)} 个回合有 AWP'
        # 势头后续
        tail = ['之后对面几乎每回合有狙']
        best = max(later, key=lambda x: (E[x]['us']['n_rifle'] + E[x]['us']['n_awp'], E[x]['us']['n_helmet'],
                                         E[x]['us']['n_nades'], -x)) if later else None
        if best is not None and not _won(E, best):
            ba, bb = _score(E, best)
            tail.append(f'第 {best} 回合 {ba}:{bb} 时我们装备最好的一回合也没打下')
        after = [_score_after(E, x) for x in later]
        fb_i = next((i for i, (x, y) in enumerate(after) if x < y), None)
        if fb_i is not None and not any(x >= y for x, y in after[fb_i + 1:]):
            # 只是这个半场没追平；整场最后赢了的话要说清楚是半场，不然和“最终赢”读着矛盾
            tail.append('这个半场再没追平' if fa > fb else '再没追平')
        prev7 = [x for x in eco07_hits if x[0] < r and _half(ctx, x[0]) == h]
        pre = f'第 {prev7[-1][0]} 回合{prev7[-1][1]}赢下本可把对面经济压到底，' if prev7 else ''
        turn_w = '把优势还回去' if lead_lost else '，领先被缩小'
        text = f'{pre}第 {r} 回合{what}{turn_w}；' + '，'.join(tail)
        f2 = dict(slot='summary.momentum', turn=r, best_buy_round=best, eco07_round=prev7[-1][0] if prev7 else None,
                  best_buy_score=E[best]['score_before'] if best else None)
        H.append(_hit(ctx, 'ECO-09', '势头', r, 'match', TEAM, '队友或团队', f2, text, '', ev))


# ================================================================ ECO-G1 捡枪升级
DEFINDEX_ITEM = {60: 'm4a1_silencer', 61: 'usp_silencer', 63: 'cz75a', 64: 'revolver'}


def _user_pickups(ctx):
    """用户的 item_pickup（ctx.cache('ev_item_pickup')），没有这个缓存时返回空表"""
    import pandas as pd
    try:
        pk = ctx.cache('ev_item_pickup')
    except (FileNotFoundError, KeyError, OSError):
        return pd.DataFrame(columns=['tick', 'item', 'user_steamid'])
    pk = pk[pk['user_steamid'].astype(str) == C.user_sid(ctx)].copy()
    # item_pickup 的 item 名对消音版 / 换皮枪不分（M4A1-S 记成 m4a1、USP 记成 hkp2000……），按 defindex 认回真名（635 高分玩家50 R2）
    if 'defindex' in pk.columns and len(pk):
        pk['item'] = [DEFINDEX_ITEM.get(int(d), it) if str(d).lstrip('-').isdigit() else it
                      for d, it in zip(pk['defindex'], pk['item'])]
    return pk.sort_values(['tick', 'item'], kind='mergesort')


def ecog1(ctx, E, H, dbg):
    th = _th(ctx)
    tr = int(ctx.th['common']['tickrate'])
    usid = C.user_sid(ctx)
    TL = _tl(ctx)
    pk = _user_pickups(ctx)
    for ri in C.rounds(ctx):
        r = ri['n']
        if r not in TL:
            continue
        eu = TL[r]['econ'].get('user') or {}
        cur_w = eu.get('primary') or eu.get('pistol')
        cur = max(_tier(cur_w), 0)
        dt = C.death_tick(ctx, usid, r)
        rows = pk[(pk.tick > ri['start']) & (pk.tick <= ri['end'])]
        for p in rows.itertuples():
            it = p.item
            if not C.is_gun(it) or (dt is not None and p.tick > dt):
                continue
            tier = _tier(it)
            if tier <= cur:
                continue
            tick = int(p.tick)
            t = C.t_of(ctx, tick, r)
            place = C.place_cn_at(ctx, usid, tick, default='')
            old_cls = C.weapon_class(cur_w) if cur_w and _tier(cur_w) >= 0 else '手枪'
            new_cls = C.weapon_class(it)
            d = C.deaths(ctx, r)
            ks = d[(d['att'] == usid) & (d['tick'] > tick) & (d['tick'] <= ri['end']) & (d['wk'] == C.wkey(it))]
            victims = [C.name_of(ctx, v) for v in ks['vic']]
            text = (f'第 {C.fmt_sec(t)} 秒{"在" + place if place else ""}捡起 {C.weapon_cn(it)}，从{old_cls}换成了{new_cls}'
                    + (f'，后面用它打死 {"、".join(victims)}' if victims else ''))
            # 还可以：捡枪后 10 秒内离最近活着的队友 3D > 900 的时间超过一半。
            # 只按活着的队友算：捡枪时你已经是最后一个（1vN），或这段时间队友都倒了，就没有“回到队友身边”（e6b R8）
            n_all = n_far = 0
            end = min(tick + int(th['alone_window_s'] * tr), ri['end'], dt if dt is not None else 10 ** 12)
            last_alive = (C.alive_counts(ctx, tick + 1, lag_s=0) or (0, 0))[0] <= 1
            for tk in range(tick, end + 1, int(th['alone_step_ticks'])):
                me = C.pos(ctx, usid, tk)
                ms = [s for s in C.alive_list(ctx, r, tk) if s != usid]
                if not ms:
                    continue
                dmin = min(C.dist3(me, C.pos(ctx, s, tk)) for s in ms)
                n_all += 1
                n_far += dmin > th['alone_dist']
            alone = (not last_alive) and n_all > 0 and n_far / n_all > th['alone_frac']
            alt = '还可以捡完枪先回到队友身边再打' if alone else ''
            # 为什么好：只按事实写。用它打出了击杀才写击杀；没击杀的捡枪只是换了把好枪，不写“为什么好”，
            # 也不让它把你的部分抬成“打得好”、不上标题（merge / compose 认 facts.neutral）
            why_good = f'捡到比手上更好的枪，后面用它打死了 {"、".join(victims)}' if victims else ''
            f = dict(slot='moment', t=C.sec(t), gun=C.weapon_cn(it), old=C.weapon_cn(cur_w) if cur_w else None,
                     old_class=old_cls, new_class=new_cls, place=place or None, victims=victims,
                     kill_t=[C.sec(C.t_of(ctx, int(k), r)) for k in ks['tick']], econ=E[r]['us']['buy'] if r in E else None,
                     alone=alone, why_good=why_good, neutral=not victims)
            ev = f'捡枪前手上最好的是{C.weapon_cn(cur_w) if cur_w else "手枪"}'
            if victims:
                ev += f'，之后用 {C.weapon_cn(it)} 打出 {len(victims)} 个击杀'
            H.append(_hit(ctx, 'ECO-G1', '好', r, 'moment', _user_name(ctx), '—', f, text, alt, ev, t=t, tick=tick))
            cur, cur_w = tier, it


# ================================================================ COST-01 装备差距下输掉对枪
def cost01(ctx, E, H, dbg):
    th = _th(ctx)
    tr = int(ctx.th['common']['tickrate'])
    usid = C.user_sid(ctx)
    TL = _tl(ctx)
    pk = _user_pickups(ctx)
    for ri in C.rounds(ctx):
        r = ri['n']
        dt = C.death_tick(ctx, usid, r)
        if dt is None or r not in E or r not in TL:
            continue
        d = C.deaths(ctx, r)
        row = d[(d['vic'] == usid) & (d['tick'] == dt)]
        if not len(row):
            continue
        row = row.iloc[0]
        killer = str(row['att'])
        if killer == usid or not C.is_enemy(ctx, usid, killer, r):
            continue
        fs = C.fires(ctx, r, sid=usid)
        fs = fs[(fs['tick'] <= dt) & (fs['tick'] >= ri['start'])]
        win = fs[fs['tick'] >= dt - int(th['fire_window_s'] * tr)]
        if not len(win):
            continue                                            # 没开枪就死的归站位、时机类
        my_wk = win.sort_values('tick', kind='mergesort').iloc[-1]['wk']
        kw = row['wk']
        if C.weapon_class(my_wk) != '手枪' or C.weapon_class(kw) not in ('步枪', '狙', '冲锋枪'):
            continue
        # 身上有没有长枪：买的主武器、死前捡过的枪、这回合开过的枪（没有 inventory 时的近似）
        eu = TL[r]['econ'].get('user') or {}
        has_long = _tier(eu.get('primary')) >= 1
        picked = pk[(pk.tick > ri['start']) & (pk.tick < dt)]
        has_long = has_long or any(_tier(x) >= 1 for x in picked['item'])
        has_long = has_long or any(_tier(x) >= 1 for x in fs['wk'])
        if has_long:
            dbg.append(f'COST-01 R{r} 你身上有长枪（自己切的手枪）：不算装备差距')
            continue
        if _self_nobuy(ctx, E, r):
            # 审核单 #22（手册 ECON-C）：装备差距是自己的买法造成的，不算合理代价；经济栏由 ECO-04 (c) 写买法思路
            dbg.append(f'COST-01 R{r} 全队起了枪、你钱够步枪加甲却只拿手枪：不出（ECO-04 写买法思路）')
            continue
        us, them = C.alive_at_death(ctx, usid, r)
        b, mb = E[r]['us']['buy'], E[r]['them']['buy']
        if mb == 'eco' and b == '全起':
            continue
        down = them - us >= th['alive_diff']
        our_eco = b == 'eco' and mb in ('全起', '强起', '半起')
        if not (down or our_eco):
            dbg.append(f'COST-01 R{r} 手枪对长枪但人数不差 {th["alive_diff"]} 个、也不是我们 eco：不出')
            continue
        t = C.t_of(ctx, dt, r)
        kname = C.name_of(ctx, killer)
        reasons = [f'你只有手枪，他拿 {C.weapon_cn(kw)}']
        if down:
            reasons.append(C.alive_str(us, them))
        if our_eco:
            reasons.append('我们 eco')
        text = f'第 {C.fmt_sec(t)} 秒被 {kname} 打死：{"、".join(reasons)}'
        s = _side(E, r)
        if our_eco and us <= 1:
            # 只剩你一个人：没有队友可以一起打交叉（e6b R20 1v4）
            alt = ('还可以找个近点的拐角等人过来，手枪能赢的基本只有先手第一枪' if s == 'T'
                   else '还可以缩在近点拐角等人进来，先手打第一个')
        elif our_eco:
            alt = ('还可以和队友同时出、打交叉，手枪能赢的基本只有先手第一枪' if s == 'T'
                   else '还可以缩在近点拐角等人进来，和队友一起打交叉')
        else:
            alt = ''
        f = dict(slot='moment', t=C.sec(t), killer=kname, killer_weapon=C.weapon_cn(kw), my_weapon=C.weapon_cn(my_wk),
                 us=us, them=them, alive=C.alive_str(us, them), buy=b, them_buy=mb, man_down=down, our_eco=our_eco,
                 # 审核单 #32（手册 T-G）：eco 局同一次阵亡只留一条“还可以”。同一次阵亡（death_t 相同）有 TEAM-03 的
                 # “思路”（_eco_hunt，decisions #24“去找对面拿步枪或 AWP 的人”）时，merge 去掉这条的 alt、只留那一条
                 alt_yields_to=['TEAM-03'] if our_eco else [])
        ev = f'你死前最后一枪用的是 {C.weapon_cn(my_wk)}，这回合身上没有长枪'
        H.append(_hit(ctx, 'COST-01', '判定', r, 'moment', _user_name(ctx), '合理代价', f, text, alt, ev, t=t, tick=int(dt),
                      death_t=C.sec(t), death_of=_user_name(ctx)))


# ================================================================ 入口
def run(ctx, debug=None):
    """跑全部经济类规则，返回 Hit 列表（按回合、时刻、规则排好序）。debug 传 list 时写入“为什么没出”的说明。"""
    E = _econ(ctx)
    H, dbg = [], debug if debug is not None else []
    eco01(ctx, E, H, dbg)
    e02 = eco02(ctx, E, H, dbg)
    eco03(ctx, E, H, dbg)
    eco04(ctx, E, H, dbg)
    eco05(ctx, E, H, dbg, e02)
    eco06(ctx, E, H, dbg)
    eco06_awp(ctx, E, H, dbg)
    eco_chain(ctx, E, H, dbg)
    e07 = eco07(ctx, E, H, dbg)
    eco08(ctx, E, H, dbg)
    eco09(ctx, E, H, dbg, e07)
    ecog1(ctx, E, H, dbg)
    cost01(ctx, E, H, dbg)
    order = {k: i for i, k in enumerate(RULES)}
    H.sort(key=lambda h: (h['round'], h['t'] if h['t'] is not None else 0.0, order[h['rule']], h['facts'].get('slot', '')))
    return H
