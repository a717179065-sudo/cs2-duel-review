# -*- coding: utf-8 -*-
"""合并层 M1–M7（rules_v1 merge_layer）：一回合的全部 hit → 定稿的 moments、主要原因、你的部分。

  M1 每次阵亡只定一次责：同一次用户阵亡按固定顺序取第一条做主因，其余写成补充从句
  M2 技术问题照记：SHOT-01..05 不随阵亡定责改变；这次死被判合理代价时单独成一个时刻，句尾加“这次死本身算合理代价”
  M3 同一击杀只夸一句：同一次击杀的好操作按优先级取主句，其余作从句
  M4 好坏两面都写：同一时刻有好操作又有你的问题——问题是“你的责任”时好操作只给问题句提供事实；
     问题是“小问题”时两面都写在好操作这个时刻里
  M5 同一件事只出一次：同一回合同一个 merge_key（同一颗道具/同一串连杀/同一次转点）只由编号最靠前的规则出句子
  M6 还可以只取规则 alt：去掉知识库编号和“对标…”括注，不自己编
  M7 句子检查：每句定稿的话过一遍 validate.text_issues，问题记在 issues 里（validate 阶段才真正拦）

入口：merge_round(hits, info) → {'moments', 'main_cause', 'your_part', 'clauses', 'ideas', 'issues', 'used'}
info = {'round', 'won', 'user'(用户名), 'enemies'(对方名单), 'names'(全部玩家), 'reason_cn', 'round_start_tick', 'tickrate'}
所有排序都显式写（确定性）。
"""
import json
import re

from . import common as C
from . import config
from . import validate

# ---------------------------------------------------------------- 优先级表（rules_v1 merge_layer 原文）
# M1：同一次用户阵亡取主因的顺序；同一档用同一个数字（SHOT-01/02/05 同档，按规则编号先后）
M1_ORDER = {r: i for i, grp in enumerate([
    ['POST-01'], ['POST-02'], ['POST-03'], ['TIME-06'], ['UTIL-03'], ['TIME-07'], ['TIME-08'], ['TIME-05'],
    ['POS-01'], ['POS-08'], ['COST-01'], ['POS-05'], ['POS-04'], ['POS-02'], ['SHOT-01', 'SHOT-02', 'SHOT-05', 'SHOT-06'],
    ['POST-04'], ['TIME-03'], ['TEAM-04']]) for r in grp}
# M3：同一次击杀的好操作取主句的顺序（SHOT-G2 / SHOT-G1 同档）；不在表里的好操作排在最后
M3_ORDER = {r: i for i, grp in enumerate([
    ['TEAM-G1'], ['SHOT-G4'], ['SHOT-G3'], ['SHOT-G2', 'SHOT-G1'], ['TEAM-G2'], ['UTIL-G2']]) for r in grp}
SHOT_RULES = ('SHOT-01', 'SHOT-02', 'SHOT-03', 'SHOT-04', 'SHOT-05', 'SHOT-06')   # SHOT-06 = 对枪动作判定（审核单第 2 条）
# M5：规则原文写明“并进这里”的，不按编号先后（TEAM-03：“同一个 K 的 POS-02 命中并进这里”）
M5_FIRST = ('TEAM-03', 'POS-03')   # POS-03 写的是整段几次出来一共开了几枪，同一串的 SHOT-03 并进它
# 只在回合层面用、不成为时刻的规则（主要原因 / 经济栏 / 总结）
ROUND_ONLY = ('TEAM-01', 'TEAM-02', 'ECO-05', 'ECO-06', 'ECO-07', 'ECO-08', 'ECO-09',
              'SUM-01', 'SUM-02', 'SUM-03', 'TIME-09')
TAIL_RULES = ('UTIL-G2', 'POS-G2')            # 句尾带“随后 / 又打死了谁”的夸奖：那一杀另有时刻时只写前半句（facts.text_short）
TEAM04_CLAUSE = '比附近的队友先露头'          # M1：TEAM-04 不做主因时只并成这半句（身边本来就没队友时用下一句）
TEAM04_CLAUSE_ALONE = '身边没有队友，一个人先露头'
M2_SUFFIX = '，这次死本身算合理代价'
# 主因是 POST-01（人数劣势没保枪）时不并进来的从句：都是在教这次拉枪怎么打好，和“保枪”方向相反
POST01_FIGHT = ('UTIL-01', 'POS-02', 'POS-03', 'POS-05', 'TEAM-04', 'TIME-01', 'TIME-05', 'COST-01')
MAX_MOMENTS = 6                               # 每回合最多几个时刻（超出的按优先级丢掉，规则命中仍进标签和汇总）
M4_SAME_S = 1.0                               # M4：好坏两面算“同一时刻”的秒数
BUY_RULES = ('ECO-01', 'ECO-02', 'ECO-03', 'ECO-04')
M3_SAME_S = 0.6                               # M3：没有目标信息时，两条好操作算同一次击杀的秒数
SAME_VICTIM_S = 3.0                           # 同一个目标在几秒内算同一次击杀
DEATH_SAME_S = 0.3                            # death_t 相差多少算同一次阵亡

_RULES = None


def rules():
    """rules_v1.json 的规则表：{'order': {id: 序号}, 'by_id': {id: rule}}（只读一次）"""
    global _RULES
    if _RULES is None:
        with open(config.RULES_DIR / 'rules_v1.json', encoding='utf-8') as f:
            d = json.load(f)
        _RULES = {'order': {r['id']: i for i, r in enumerate(d['rules'])}, 'by_id': {r['id']: r for r in d['rules']}}
        # 审核单新增的编号（config.EXTRA_RULES，rules_v1.json 只读）：排在 after 那条后面
        for x in config.EXTRA_RULES:
            if x['id'] not in _RULES['by_id']:
                _RULES['order'][x['id']] = _RULES['order'].get(x.get('after'), 998) + 0.5
                _RULES['by_id'][x['id']] = {k: v for k, v in x.items() if k != 'after'}
    return _RULES


def rule_idx(rid):
    return rules()['order'].get(rid, 999)


def rule_name(rid, short=True):
    """规则名（去掉括号里的补充，给标签、标题用）"""
    r = rules()['by_id'].get(rid)
    if not r:
        return rid
    n = r['name']
    return re.sub(r'（[^）]*）', '', n).strip() if short else n


# ---------------------------------------------------------------- hit 小工具
def norm_hit(h):
    """补齐 hit 的可选字段（不改原对象）"""
    h = dict(h)
    h.setdefault('facts', {})
    h['facts'] = h['facts'] or {}
    for k in ('text', 'alt', 'evidence', 'blame', 'kind', 'scope', 'subject', 'merge_key', 'death_of'):
        h.setdefault(k, None)
    h['text'] = h['text'] or ''
    h['alt'] = h['alt'] or ''
    h['evidence'] = h['evidence'] or ''
    h['scope'] = h['scope'] or 'moment'
    return h


def hit_key(h):
    """全局固定排序：回合、秒、规则编号、tick、句子"""
    t = h.get('t')
    return (h.get('round') or 0, t if isinstance(t, (int, float)) else -1e9, rule_idx(h.get('rule')),
            h.get('tick') if isinstance(h.get('tick'), int) else -1, h.get('rule') or '', h.get('text') or '')


BLAME_BOTH = '你的责任 + 队友或团队'
BLAME_MINOR = '小问题'
# 时刻上算“你的”问题的 blame（页面各有一个小标签）：你的部分不能写“打得好”、标签排前面都按这一组
YOUR_BLAMES = ('你的责任', BLAME_BOTH, BLAME_MINOR)


def blame_parts(b):
    """hit.blame → (时刻上显示的 blame, 对“你的部分”的分量 'major'/'minor'/'none')
    页面认得的 moment.blame：— / 你的责任 / 你的责任 + 队友或团队 / 小问题 / 队友或团队 / 合理代价。
    hit 判的是“你的责任 + 队友或团队”或“小问题”时照原样带到时刻上（8d R5、635 高分玩家45 R17 的 TEAM-06 是两边都有）"""
    b = (b or '').strip()
    if not b or b.startswith('—') or b.startswith('另一种思路'):
        return '—', 'none'
    if '你的责任' in b and '队友' in b:
        return BLAME_BOTH, 'major'
    if '合理代价' in b and '小问题' in b:
        return '合理代价', 'minor'
    if '小问题' in b:
        return BLAME_MINOR, 'minor'
    if b.startswith('你的责任'):
        return '你的责任', 'major'
    if '合理代价' in b:
        return '合理代价', 'none'
    if '队友' in b or '团队' in b:
        return '队友或团队', 'none'
    return '—', 'none'


def is_good(h):
    return h.get('kind') == '好'


def victim_of(h):
    f = h.get('facts') or {}
    for k in ('victim', 'target', 'enemy', 'killed'):
        if isinstance(f.get(k), str) and f.get(k):
            return f[k]
    return None


_KB_ID = r'(?:GEN|CT|T)-[A-Z]{1,4}-\d{1,3}'
_RULE_ID = r'(?:SHOT|POS|TEAM|UTIL|TIME|POST|ECO|COST|SUM)-G?\d{1,2}'
_PAREN_REF = re.compile(r'[（(][^（）()]*(?:' + _KB_ID + '|' + _RULE_ID + r'|对标|知识库|固定句)[^（）()]*[）)]')
TEMPLATE_RE = re.compile(r'[{}｛｝]')          # 还没填的模板占位符
_SP1 = re.compile(r'(?<=[\u4e00-\u9fff])(?=[A-Za-z0-9_$])')
_SP2 = re.compile(r'(?<=[A-Za-z0-9_\-])(?=[\u4e00-\u9fff])')    # 名字可能以 - 结尾（例如 队友A-）
TEAM_NO_ALT = ''        # 团队责任的时刻规则没给 alt 时不写“还可以”（以前的固定句是记账说明，不是另一种思路，8d R15）


def sp(s):
    """中文和英文/数字之间补一个空格（“在CT 出生点” → “在 CT 出生点”），只动空格不动字"""
    return _SP2.sub(' ', _SP1.sub(' ', s or ''))


def unfilled(s):
    """句子里还留着模板占位符（{报点}、{victim}…）"""
    return bool(TEMPLATE_RE.search(s or ''))


def clean_alt(alt):
    """M6：规则 alt 去掉知识库编号、规则编号、“对标…”括注；“不配”这类说明、没填完的模板返回空"""
    a = (alt or '').strip()
    if not a or '不配' in a[:12] or a.startswith('（') or a.startswith('只在') or unfilled(a):
        return ''
    a = _PAREN_REF.sub('', a)
    a = re.sub(r'[，、；]?\s*' + _KB_ID, '', a)
    a = re.sub(r'\s+([，。；])', r'\1', a).strip().rstrip('。；，')
    return a


def better_text(alt):
    """时刻的 better：页面已经加了“还可以：”，去掉开头重复的“还可以”"""
    a = clean_alt(alt)
    return re.sub(r'^还可以[：:，,\s]*', '', a)


def _part(s):
    return (s or '').strip().rstrip('。；，')


def clause_text(h):
    """补充从句：TEAM-04 固定半句；规则给了 facts.clause 用它；否则用整句"""
    if h.get('rule') == 'TEAM-04':
        f = h.get('facts') or {}
        if f.get('variant') == 'entry' and isinstance(f.get('clause'), str) and f['clause']:
            return _part(f['clause'])       # 审核单第 28 条：“你第一个进 X 点，后面的人没有及时跟上”（团队）
        near = f.get('near')
        return TEAM04_CLAUSE if near or near is None else TEAM04_CLAUSE_ALONE
    c = (h.get('facts') or {}).get('clause')
    return _part(c if isinstance(c, str) and c else h.get('text'))


def is_post01_background(h):
    """M1：POST-01 的豁免、合理代价分支只做背景，不当主因"""
    if h.get('rule') != 'POST-01':
        return False
    br = str((h.get('facts') or {}).get('branch') or '')
    return blame_parts(h.get('blame'))[0] in ('合理代价', '—') or br in ('豁免', '合理代价', '好', '好的操作', '保枪')


# ---------------------------------------------------------------- 时刻单元
def _unit(h, death=False):
    mb, contrib = blame_parts(h.get('blame'))
    return {'hit': h, 'clauses': [], 'annot': [], 'facts_from': [], 'death': death, 'key': death,
            'suffix': '', 'mblame': mb, 'contrib': contrib}


def _unit_hits(u):
    return [u['hit']] + u['clauses'] + u['annot'] + u['facts_from']


def _fold(win, u):
    """把单元 u 并进 win（作从句）"""
    win['clauses'] += [u['hit']] + u['clauses']
    win['annot'] += u['annot']
    win['facts_from'] += u['facts_from']


def _m1(death_hits):
    """M1：一次阵亡的全部问题 hit → (主因 hit, 从句 hit 列表)"""
    cands = [h for h in death_hits if h['rule'] in M1_ORDER and not is_post01_background(h)]
    if cands:
        main = sorted(cands, key=lambda h: (M1_ORDER[h['rule']], rule_idx(h['rule']), hit_key(h)))[0]
    else:
        # 没有排序表里的规则：先取你自己的问题（团队级的 TEAM-03 之类留给主要原因），再按编号最靠前
        main = sorted(death_hits, key=lambda h: (is_post01_background(h), not h.get('is_user'), rule_idx(h['rule']),
                                                 hit_key(h)))[0]
    rest = [h for h in death_hits if h is not main]
    return main, rest


def _cluster_deaths(hits, user):
    """按 death_t 把用户阵亡相关的 hit 分组（相差 ≤ DEATH_SAME_S 算同一次）"""
    ds = sorted({float(h['death_t']) for h in hits if h.get('death_of') == user and isinstance(h.get('death_t'), (int, float))})
    groups = []
    for d in ds:
        if groups and d - groups[-1][-1] <= DEATH_SAME_S:
            groups[-1].append(d)
        else:
            groups.append([d])
    out = []
    for g in groups:
        lo, hi = g[0] - 1e-6, g[-1] + 1e-6
        out.append((g[0], [h for h in hits if h.get('death_of') == user and isinstance(h.get('death_t'), (int, float))
                           and lo <= float(h['death_t']) <= hi]))
    return out


def _mate_death(h):
    """讲的是一个队友的阵亡（主角不是你、带 death_of / death_t）"""
    return (not h.get('is_user') and isinstance(h.get('death_of'), str) and h.get('death_of') == h.get('subject')
            and isinstance(h.get('death_t'), (int, float)))


def _m5(units):
    """M5：同一个 merge_key 只留一个单元（阵亡单元优先，其次规则编号最靠前）；
    另外同一 tick 的两条开枪类问题（同一串）也并成一句"""
    order = sorted(units, key=lambda u: (not u['death'], u['hit']['rule'] not in M5_FIRST,
                                         rule_idx(u['hit']['rule']), hit_key(u['hit'])))
    owner, out = {}, []
    for u in order:
        keys = sorted({h.get('merge_key') for h in [u['hit']] + u['clauses'] if h.get('merge_key')})
        for h in [u['hit']] + u['clauses']:
            # 同一 tick 的开枪类问题（同一串）并成一句；POS-03（几次出来一共开了 N 枪）和同一串的 SHOT-03 也是同一件事（8d R18）
            if (h['rule'] in SHOT_RULES or h['rule'] == 'POS-03') and isinstance(h.get('tick'), int):
                keys.append(f"_shot:{h.get('subject')}:{h['tick']}")
            # 同一个队友的同一次阵亡只出一个时刻（128 R22：高分玩家04 那次死 POS-02、POS-06 各写一种原因）
            if _mate_death(h):
                keys.append(f"_vdeath:{h['death_of']}:{C.sec(h['death_t'])}")

        win = next((owner[k] for k in keys if k in owner), None)
        if win is not None:
            _fold(win, u)
            for k in keys:
                owner.setdefault(k, win)
            continue
        for k in keys:
            owner[k] = u
        out.append(u)
    return out


TG1_FAR_S = 3.0          # TEAM-G1“他找过来”：队友倒下到你打掉他隔了这么多秒以上，“替队友换了回来”不是这一杀的要点


def _m3_rank(h):
    """M3 主句顺序；TEAM-G1“他找过来”隔得久（8d R9：队友C 倒下 6.9 秒后才打掉 对手1，中间 队友A 刚倒、打成 2v4）
    排到 SHOT-G4（人数怎么变）后面：这一杀是在扳人数，不是替 7 秒前倒下的队友换回来"""
    f = h.get('facts') or {}
    if h['rule'] == 'TEAM-G1' and f.get('variant') == '他找过来' and isinstance(f.get('dt'), (int, float)) \
            and f['dt'] > TG1_FAR_S:
        return M3_ORDER['SHOT-G4'] + 0.5
    return M3_ORDER.get(h['rule'], 99)


_NVM = re.compile(r'\d+\s*v\s*\d+')


def _m3(goods):
    """M3：好操作按“同一次击杀”分组，每组取一个主句，其余作从句"""
    goods = sorted(goods, key=hit_key)
    groups = []
    for h in goods:
        v, t = victim_of(h), h.get('t') or 0
        placed = None
        for g in groups:
            for x in g:
                xv, xt = victim_of(x), x.get('t') or 0
                same_key = h.get('merge_key') and h.get('merge_key') == x.get('merge_key')
                same_vic = v and xv and v == xv and abs(t - xt) <= SAME_VICTIM_S
                near = abs(t - xt) <= M3_SAME_S and not (v and xv and v != xv)
                if same_key or same_vic or near:
                    placed = g
                    break
            if placed is not None:
                break
        if placed is None:
            groups.append([h])
        else:
            placed.append(h)
    units = []
    for g in groups:
        g = sorted(g, key=lambda h: (_m3_rank(h), rule_idx(h['rule']), hit_key(h)))
        u = _unit(g[0])
        u['clauses'] = g[1:]
        units.append(u)
    return units


def _m4(goods, probs):
    """M4：好坏同一时刻。返回 (留下的好操作单元, 留下的问题单元)"""
    keep_g, drop_p = [], set()
    for g in sorted(goods, key=lambda u: hit_key(u['hit'])):
        gh = g['hit']
        gt, gv = gh.get('t') or 0, victim_of(gh)
        gk = _kill_tick(gh)
        best = None
        for i, p in enumerate(sorted(probs, key=lambda u: hit_key(u['hit']))):
            ph = p['hit']
            if id(p) in drop_p or p['contrib'] == 'none' or not (ph.get('is_user') or p['death']):
                continue
            pt, pv = ph.get('t') or 0, victim_of(ph)
            # 一枪没中的那一串（SHOT-03 五枪全空）不是打死他的那一串：同一场对枪，但不是同一次击杀（8d R21）
            pk = None if (ph['rule'] in SHOT_RULES and (ph.get('facts') or {}).get('hits') == 0) else _kill_tick(ph)
            same_k = gk is not None and pk is not None and gk == pk
            near = abs(pt - gt) <= M4_SAME_S
            if not (near or same_k or (gv and pv and gv == pv and abs(pt - gt) <= SAME_VICTIM_S)):
                continue
            # 你的责任的问题只在同一枪 / 同一时刻时盖掉夸奖；同一个对手早两秒的另一串（8d R21：20.9 秒五枪全空 vs
            # 22.9 秒和 队友B 一起拿下他）是另一件事，夸奖先认同一次击杀的小问题（22.7 秒第一枪跑着开的）
            if p['contrib'] == 'major' and not (same_k or near):
                continue
            # 同一时刻的你的责任（第 3 场 R13 47.2：POS-01 站位，SHOT-G4 只作事实）> 同一次击杀的小问题 > 其余
            d = (not (p['contrib'] == 'major' and (same_k or near)), not same_k, abs(pt - gt))
            if best is None or d < best[0]:
                best = (d, i, p)
        if best is None:
            keep_g.append(g)
            continue
        p = best[2]
        ph = p['hit']
        if ph['rule'] == 'SHOT-05' and p['contrib'] != 'major' and not p['death'] and victim_of(ph) and any(
                x['rule'] in ('SHOT-G1', 'SHOT-G2') and victim_of(x) == victim_of(ph) for x in [gh] + g['clauses']):
            # 同一场对枪：先被打中（没停住）后又站住反杀。夸奖这一面赢了，问题不再并进去写成“没乱动；不过人还在动”（635 R23）
            drop_p.add(id(p))
            keep_g.append(g)
            continue
        same_kill = isinstance(gh.get('tick'), int) and gh.get('tick') == ph.get('tick')
        if p['contrib'] == 'major' or (p['death'] and same_kill):
            # 好操作只给问题句子提供事实。阵亡单元的小问题如果和好操作是同一次击杀（同一个 tick，例如 POS-01“打他时站的位置
            # 另一个人也打得到”+ TEAM-G1“替队友换了回来”），也只留问题这一句——同一枪一夸一批并排读着矛盾（d5a R21）
            p['facts_from'] += [gh] + g['clauses']
        elif p['death']:
            keep_g.append(g)                             # 阵亡单元的小问题：两边各自成时刻
        else:
            g['annot'] += [p['hit']] + p['clauses']      # 小问题：两面都写在好操作这个时刻
            drop_p.add(id(p))
            keep_g.append(g)
    return keep_g, [p for p in probs if id(p) not in drop_p]


REPEAT_S = 5.0                                # 同一条开枪规则、同一个对手、几秒内一模一样的两句并成一句


def _merge_repeats(units):
    """同一个对手几秒内两串一模一样的开枪问题（128 R6 第 31、34.1 秒两串 AK 连开六枪全空；R3 34.8 / 36 秒两次没停住）：
    并成一句“第 31 秒和第 34.1 秒两次……”，第二条只给标签提供事实，不再单独成时刻、也不再在你的部分里写第二遍"""
    out = []
    for u in sorted(units, key=lambda u: hit_key(u['hit'])):
        h = u['hit']
        v = victim_of(h)
        if not u['death'] and h.get('is_user') and h['rule'] in SHOT_RULES and v and isinstance(h.get('t'), (int, float)):
            rest = _LEAD_SEC.sub('', _part(h['text']))
            prev = next((o for o in out if not o['death'] and o['hit']['rule'] == h['rule'] and victim_of(o['hit']) == v
                         and isinstance(o['hit'].get('t'), (int, float)) and 0 <= h['t'] - o['hit']['t'] <= REPEAT_S
                         and not o.get('repeat') and _LEAD_SEC.sub('', _part(o['hit']['text'])) == rest), None)
            if prev is not None and _LEAD_SEC.match(_part(prev['hit']['text'])):
                t0, t1 = C.fmt_sec(prev['hit']['t']), C.fmt_sec(h['t'])
                prev['hit'] = dict(prev['hit'], text=f'第 {t0} 秒和第 {t1} 秒两次' + rest)
                prev['repeat'] = True
                prev['facts_from'] += [h] + u['clauses']
                continue
        out.append(u)
    return out


# ---------------------------------------------------------------- 时刻 → moment dict
def _sec(t):
    """时刻的秒数：和 common.fmt_sec 同一个取整，整数写成 int（页面上显示 44s 而不是 44.0s）"""
    if not isinstance(t, (int, float)):
        return 0
    v = C.sec(t)
    return int(v) if v == int(v) else v


def _tick(h, info):
    if isinstance(h.get('tick'), int):
        return h['tick']
    rs = info.get('round_start_tick')
    if isinstance(rs, int) and isinstance(h.get('t'), (int, float)):
        return rs + int(round(h['t'] * info.get('tickrate', 64)))
    return None


def _join(parts, sep='；'):
    seen, out = set(), []
    for p in parts:
        p = _part(p)
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return sep.join(out)


_LEAD_SEC = re.compile(r'^第\s*[−\-]?\d+(?:\.\d+)?\s*秒[，,]?\s*')
_LEAD_RANGE = re.compile(r'^第\s*[−\-]?\d+(?:\.\d+)?(?:\s*[–—\-~]\s*\d+(?:\.\d+)?)?\s*秒')   # “第 19.7–22.8 秒”也算句首带了时间
CLAUSE_SAME_S = 1.0                           # 从句和主句相差不到这么多秒：从句开头的“第 N 秒”不再重复


CONTEXT_FIRST = ('POST-04',)                  # 交代一段局面的从句，比主句早时放在主句前面
MAX_CLAUSES = 4                               # 一个时刻最多并几条从句（多出来的按时间先后丢掉，规则仍进标签）

# 阵亡时刻的从句：主句已经写了这次死，从句结尾再写一遍“被打死 / 倒了”读起来像死了两次，去掉
_DEATH_END = re.compile(r'[，；,]\s*(?:随后|最后|然后|很快)?你?也?(?:还白着就|一接触就|就)?'
                        r'(?:被[^，；。：]{0,40}?)?(?:打死了?|倒了|被秒)(?:（[^（）]*）)?$')
_COST_END = re.compile(r'[，；,]\s*(?:这次|这)死?(?:本身)?算合理代价$|[，；,]\s*这是合理代价$')
_DEATH_END2 = re.compile(r'(?<=[^，；,])就被(?:[^，；。：]{0,40}?)?打死了?$')


def strip_death(s):
    """从句去掉结尾的阵亡说法（“，被打死”“，还白着就被打死了”“，随后你也倒了”“一枪没开就被打死”→“一枪没开”）"""
    s = _part(s)
    for _ in range(3):
        s2 = _COST_END.sub('', s)
        s2 = _DEATH_END.sub('', s2)
        s2 = _DEATH_END2.sub('', s2)
        if s2 == s:
            break
        s = _part(s2)
    return s


def _covered(c, h):
    """主句已经说过的从句内容：返回要保留的部分（'' = 整条不写）。
    TEAM-G1 主句已写补枪拉回人数 → 同一次击杀的 SHOT-G4 不再重复；
    TIME-05 主句已写“第一枪还没停稳，没中” → SHOT-01 不再重复；
    TIME-06 主句已写被谁的闪白了 → 同一颗闪的 UTIL-03 只留主句没有的事实（之前闪过一次 / 你站在他前面）"""
    hr, cr = h.get('rule'), c.get('rule')
    if hr == 'TEAM-G1' and cr == 'SHOT-G4':
        # 补枪的句子写了人数（“4v5 拉回 4v4”）或“残局赢下”：SHOT-G4 不再重复；“他找过来”没写人数，留下人数怎么变（8d R9）
        hf, cf = h.get('facts') or {}, c.get('facts') or {}
        if (hf.get('variant') == '补枪' or _NVM.search(h.get('text') or '') or '残局' in (h.get('text') or '')
                or not cf.get('alive') or not cf.get('alive_after')):
            return ''
        return f"当时 {cf['alive']}，扳成 {cf['alive_after']}"
    if _mate_death(h) and _mate_death(c) and h['death_of'] == c['death_of'] and C.sec(h['death_t']) == C.sec(c['death_t']):
        return ''                    # 同一个队友的同一次死，主句已经给了一种解释，另一种不再并排写（128 R22）
    if hr == 'POS-03' and cr == 'SHOT-03' and victim_of(h) and victim_of(h) == victim_of(c):
        return ''                    # “几次出来一共开了 12 枪一枪没中”已经包含了这一串（8d R18 又写“连开五枪全空”）
    if is_good(h) and is_good(c) and _same_kill(h, c):
        # 同一次击杀的几条夸奖：主句已经写了打死谁、人数怎么变，从句只补“怎么打的”那半句（8d R3 38.0 / R5 43.4）
        m = (c.get('facts') or {}).get('manner')
        return _part(m) if isinstance(m, str) else ''
    if hr == 'POS-05' and cr == 'POS-04':
        return ''                    # “走出去接他的正面枪”和“他一直没露过面”说的是同一次死的两种原因，读着矛盾（635 R1）
    if cr in ('POS-04', 'COST-01') and blame_parts(c.get('blame'))[0] == '合理代价' \
            and blame_parts(h.get('blame'))[1] == 'major':
        return ''                    # 主因判了你的责任，从句不再写“只能硬回防 / 这次算合理代价”（635 高分玩家50 R23）
    if cr == 'TEAM-03' and (c.get('facts') or {}).get('user_in') and c.get('death_of'):
        # 连杀里有你：主句已经写了你的死，从句只补“他这一波杀了几个”，不把你的死再写一遍（bd R24）
        f = c.get('facts') or {}
        return f"{f.get('killer')} 这一波连杀了 {f.get('n')} 个" if f.get('killer') and f.get('n') else ''
    if hr == 'POST-02' and (cr in ('TEAM-04', 'POS-04')
                            or (cr == 'UTIL-01' and '都没用' in (h.get('text') or ''))):
        # “回防一个人先进”主句已经写了一个人先走、谁在身后、道具都没用、被谁打死：
        # “比附近的队友先露头”“身上还有道具到死都没扔”“只能硬回防，你还在和 X 对枪”再写一遍读着啰嗦（8d R12 60.4）
        return ''
    if hr == 'POS-01' and cr == 'SHOT-04' and (h.get('facts') or {}).get('event') == 'miss'             and h.get('merge_key') and h.get('merge_key') == c.get('merge_key'):
        # 主句已经写了“用狙打 X 没中”：同一枪的 SHOT-04 只补这一段一共空了几枪（128 R? 27.4）
        n = (c.get('facts') or {}).get('n') or 1
        return '' if n <= 1 else f"这一段 AWP 一共开了{C.num_cn(n)}枪都没中"
    if hr == 'TIME-05' and cr == 'TEAM-04':
        return ''                    # 主句已经写了“一个人 / 抢在附近的队友前面冲”（8d R18）
    if cr == 'SHOT-05' and (c.get('facts') or {}).get('shots') == 0 and '一枪没开' in (h.get('text') or ''):
        return ''                    # 主句已经写了“一枪没开，被打死”，没开枪的 SHOT-05 再写一遍（bd R26）
    if cr == DUEL_RULE and '一枪没开' in (h.get('text') or '') and '一枪没开' in (c.get('text') or ''):
        return ''                    # 同上：对枪判定“他打中你那一刻你还没停住，一枪没开”（10ce R17 TIME-05 主句已写一枪没开）
    if hr == 'TEAM-03' and cr == 'POS-02' and (h.get('facts') or {}).get('killer') == (c.get('facts') or {}).get('killer'):
        return ''                    # 同一个人的连杀已经写了这几个人怎么倒下的（rules_v1：TEAM-03 收 POS-02，128 R4）
    if hr == 'POST-03' and cr == 'POS-05':
        return ''                    # “人多守包时走出去找人”和“人多时去接五五开的枪”是同一次死，换个说法再说一遍（R21）
    if hr == 'TIME-05' and cr == 'SHOT-01' and '第一枪还没停稳' in (h.get('text') or ''):
        return ''
    if hr == 'TIME-06' and cr == 'UTIL-03':
        hf, cf = h.get('facts') or {}, c.get('facts') or {}
        if hf.get('by') and hf.get('by') == cf.get('mate'):
            # 写扔闪队友的名字：前面一条从句说的可能是对手（“他刚打死你队友”），“他”会指错人（8d R20）
            mn = cf.get('mate')
            keep = ([f'之前已经被 {mn} 闪过一次'] if cf.get('earlier') else []) + ([f'你站在 {mn} 前面、离闪更近'] if cf.get('ahead') else [])
            return '，'.join(keep)
    return None


def _head_sec(h):
    """主句开头写的“第 N 秒”（没有返回 None）"""
    m = _LEAD_SEC.match(_part(h.get('text')))
    if not m:
        return None
    return float(re.search(r'[−\-]?\d+(?:\.\d+)?', m.group(0)).group(0).replace('−', '-'))


def _sub_clause(c, h, annot=False):
    """并进主句的从句：
    - 主句已经说过的内容不重复（_covered）；
    - 和主句同一时刻（≤ CLAUSE_SAME_S 秒）去掉开头的“第 N 秒”，隔得远又没写时间的补上“第 N 秒”；
    - M4 写在好操作时刻里的小问题前面加“不过”"""
    s = _covered(c, h)
    covered = s is not None
    ct, ht = c.get('t'), h.get('t')
    far = isinstance(ct, (int, float)) and isinstance(ht, (int, float)) and abs(ct - ht) > CLAUSE_SAME_S
    hs = _head_sec(h)
    if far and hs is not None and isinstance(ct, (int, float)) and abs(ct - hs) <= CLAUSE_SAME_S:
        # 主句开头写的那一秒和从句是同一时刻（POS-05 的 t 是阵亡、句子从拉出去那一秒写起，10ce R11）：按同一时刻并
        far, ht = False, hs
    if s is None:
        # 和主句隔得远的从句用整句（带地点、对象）；半句（facts.clause）只适合补充同一时刻
        # 雷 / 换位的夸奖并进击杀时刻：主句已经写了这一杀，不再带“随后把他打死 / 又打死了 X”（8d R7、R15）
        full = (c.get('facts') or {}).get('text_short') if c.get('rule') in TAIL_RULES else None
        s = _part(full or c.get('text')) if far and c.get('rule') != 'TEAM-04' else (_part(full) if full else clause_text(c))
    if not s:
        return ''
    # 完整性复核：开枪类从句打的不是这次打死你的人时，点出打的是谁（bd R13：“被 对手2（第4场） 打死；连开四枪全空”其实是打 对手3（第4场） 的四枪）
    cf = c.get('facts') or {}
    if (c.get('rule') in ('SHOT-01', 'SHOT-02', 'SHOT-03', 'SHOT-05') and cf.get('died') and not cf.get('died_to_target')
            and isinstance(cf.get('enemy'), str) and cf['enemy'] and cf['enemy'] not in s
            and cf['enemy'] not in {(h.get('facts') or {}).get(k) for k in ('enemy', 'victim', 'target', 'killer', 'attacker')}):
        s = f"对 {cf['enemy']} " + s
    # TEAM-04 固定半句、被主句覆盖后剩下的半句都是在补充主句那一刻，不另写时间
    if isinstance(ct, (int, float)) and isinstance(ht, (int, float)) and not covered and c.get('rule') != 'TEAM-04':
        if abs(ct - ht) <= CLAUSE_SAME_S or s.startswith(f'第 {C.fmt_sec(ht)} 秒'):
            s = _LEAD_SEC.sub('', s)            # 同一时刻，或者从句开头写的就是主句那一秒
        elif not _LEAD_RANGE.match(s):
            s = f'第 {C.fmt_sec(ct)} 秒' + s
    if annot:
        s = re.sub(r'^(?:不过|但是?)[，,]?', '', s)
        s = '不过' + s
    return s


def _kill_tick(h):
    f = h.get('facts') or {}
    if isinstance(f.get('kill_tick'), int):
        return f['kill_tick']
    if h.get('rule') in ('TEAM-G1', 'TEAM-G2', 'SHOT-G4') and isinstance(h.get('tick'), int):
        return h['tick']
    return None


KILL_GOOD = ('SHOT-G1', 'SHOT-G2', 'SHOT-G3', 'SHOT-G4', 'TEAM-G1', 'TEAM-G2')   # 夸的就是这一杀本身的规则


def _same_kill(a, b):
    """两条好操作夸的是同一次击杀：击杀 tick 一样，或者目标一样、时间挨着。
    UTIL-G2（之前的雷打掉了血）、UTIL-G3（杀完扔道具拖时间）说的是这一杀前后的另一件事，不算"""
    if a.get('rule') not in KILL_GOOD or b.get('rule') not in KILL_GOOD:
        return False
    ka, kb = _kill_tick(a), _kill_tick(b)
    if ka is not None and kb is not None:
        return ka == kb
    va, vb = victim_of(a), victim_of(b)
    ta, tb = a.get('t'), b.get('t')
    return bool(va and va == vb and isinstance(ta, (int, float)) and isinstance(tb, (int, float))
                and abs(ta - tb) <= SAME_VICTIM_S)


def is_entry(h):
    """TEAM-04 / TEAM-05 的进点分支（审核单第 28 条：进点没人跟上，记团队）"""
    return h.get('rule') in ('TEAM-04', 'TEAM-05') and (h.get('facts') or {}).get('variant') in ('entry', 'entry_follow')


def u01_out(u):
    """UTIL-01 已经写了“（一个人）出去接枪”：TEAM-04 的“一个人先露头 / 比附近的队友先露头”是同一个意思再说一遍（8d R3、R22），
    TEAM-04 的从句不写；compose 的标签也按这个算（页面上没写出来的不做标签，8d R3/R19/R22）。
    TEAM-04 的进点分支（后面的人没跟上）说的是另一件事，不受影响"""
    return any(x['rule'] == 'UTIL-01' and '出去接枪' in (x.get('text') or '') for x in [u['hit']] + u['clauses'])


def _subs(u, h):
    """从句列表：前面一条从句写了别的秒数（隔得远的整句）之后，紧跟着的同一时刻半句要把自己的秒数写回来，
    不然读着像接着上一句说同一枪（8d R3 44.4：第 42.6 秒“第一枪没中”后面又一个不带时间的“第一枪是跑着开的，没中”）"""
    skip = u.get('skip') or ()
    out, other_time = [], False
    allh = [h] + u['clauses']
    # 主句开头写的那一秒（可能和主句 hit 的 t 差零点几秒，10ce R11：t 是 87.7、句子写第 87.8 秒）
    hm = _LEAD_SEC.match(_part(h.get('text')))
    head_sec = re.search(r'[−\-]?\d+(?:\.\d+)?', hm.group(0)).group(0) if hm else None
    # UTIL-01 已经写了“出去接枪”时不并 TEAM-04（u01_out）；TEAM-04 说附近有队友时，UTIL-01 也不写“一个人”
    u01_out = any(x['rule'] == 'UTIL-01' and '出去接枪' in (x.get('text') or '') for x in allh)
    t04_near = any(x['rule'] == 'TEAM-04' and (x.get('facts') or {}).get('near') for x in allh)
    for c in sorted(u['clauses'], key=hit_key):
        if any(c is x for x in skip):
            continue
        if u01_out and c['rule'] == 'TEAM-04' and not is_entry(c):
            continue
        s = _sub_clause(c, h)
        if c['rule'] == 'SHOT-02' and s.startswith('他一出来') and victim_of(c) and victim_of(c) not in (h.get('text') or '')[-12:]:
            s = f'{victim_of(c)} 一出来' + s[len('他一出来'):]      # 主句最后说的是别人时“他”指不清（128 R15）
        if c['rule'] == 'UTIL-01' and t04_near:
            s = s.replace('一个人出去接枪', '出去接枪')
        if c['rule'] == 'UTIL-01' and h['rule'] == 'TEAM-04' and not is_entry(h):
            s = re.sub(r'，(?:一个人)?出去接枪$', '', s)      # 主句已经写了一个人先露头（e6b R6）
        if u['death']:
            s = strip_death(s)                  # 这次死只在主句里写一次
        if not s:
            continue
        ct, ht = c.get('t'), h.get('t')
        lead = _LEAD_SEC.match(s)
        if lead and not s.startswith(f'第 {C.fmt_sec(ht)} 秒') and not (head_sec and s.startswith(f'第 {head_sec} 秒')):
            other_time = True
        elif other_time and not lead and isinstance(ct, (int, float)) and c.get('rule') != 'TEAM-04' \
                and _covered(c, h) is None:
            s = f'第 {C.fmt_sec(ct)} 秒' + s
            other_time = C.fmt_sec(ct) != C.fmt_sec(ht)
        out.append(s)
    return out[:MAX_CLAUSES]


def _split_good_alt(alt):
    """好操作的 alt → (理由, [还可以……])：
    “继续这样……”“还可以继续……”是指令不是理由，丢掉；“还可以……”（包括“站位上还可以……”）放进 also；其余的分句是理由"""
    why, tips = [], []
    for seg in clean_alt(alt).split('；'):
        seg = _part(seg)
        if not seg:
            continue
        i = seg.find('还可以')
        if seg.startswith('继续') or seg.startswith('还可以继续') or (0 <= i and seg[i:].startswith('还可以继续')):
            continue
        if i >= 0:
            tips.append(_part(re.sub(r'^还可以[：:，,\s]*', '', seg[i:])))
        else:
            why.append(seg)
    return '；'.join(why), [t for t in tips if t]


def to_moment(u, info):
    h = u['hit']
    kind = '关键' if u['key'] else ('好' if is_good(h) else '问题')
    subs = _subs(u, h)
    head = _part(h['text']) + u['suffix']
    if u['death']:
        # 主句结尾“随后你被 X 打死”，后面的从句又写了和 X 的那场对枪：死挪到那场对枪后面写，
        # 不然读着先死了再去和他对枪（635 高分玩家45 R1 14.3：“随后你被 高分玩家49 打死；第 15.7 秒……和 高分玩家49 对枪”）
        dm = re.search(r'，随后你被 (\S+?) 打死', head)
        if dm:
            i = next((j for j, x in enumerate(subs) if f' {dm.group(1)} ' in f' {x} '), None)
            if i is not None:
                head = head.replace(dm.group(0), '', 1)
                subs = subs[:i] + [subs[i] + '，最后被他打死'] + subs[i + 1:]
    # 交代局面的从句（POST-04“第 40.5 秒变成 1v2 守包，之后……”）比主句早：放在主句前面，按时间读（8d R13 47.2）
    pre = []
    if subs:
        cs = [c for c in sorted(u['clauses'], key=hit_key) if not any(c is x for x in (u.get('skip') or ()))]
        for c in cs:
            if c['rule'] in CONTEXT_FIRST and isinstance(c.get('t'), (int, float)) and isinstance(h.get('t'), (int, float))                     and c['t'] < h['t'] - CLAUSE_SAME_S:
                hit = next((x for x in subs if x.startswith(f"第 {C.fmt_sec(c['t'])} 秒")), None)
                if hit is not None:
                    pre.append(hit)
                    subs = [x for x in subs if x is not hit]
    what = _dedupe_segs(_join(pre + [head] + subs + [_sub_clause(a, h, annot=True) for a in u['annot']]))
    also = ''
    if is_good(h):
        # 好操作时刻：页面把 better 标成“为什么好：”。这里只放夸奖的理由（规则 facts.why_good，或 alt 里不是建议的那几句）；
        # “还可以……”（规则给的改进、M4 并进来的小问题的改法）放到 also，页面单独一行标“还可以：”；
        # “继续这样……”是指令，两边都不放
        f = h.get('facts') or {}
        why_alt, tips = _split_good_alt(h['alt'])
        better = f['why_good'] if isinstance(f.get('why_good'), str) else why_alt
        # “还可以”只给一个思路：并进来的小问题有改法就用它，没有才用夸奖规则自己的（8d R5 98 秒一次塞了两条）
        ann = [better_text(a['alt']) for a in u['annot'] if better_text(a['alt'])]
        also = ann[0] if ann else (tips[0] if tips else '')
    else:
        better = _join([better_text(h['alt'])] + [better_text(a['alt']) for a in u['annot']])
    if not better and not is_good(h):
        # 主句的规则没给能用的“还可以”：取第一条从句的（M6 仍然只用规则 alt）
        better = next((better_text(c['alt']) for c in u['clauses'] if better_text(c['alt'])), '')
    if is_good(h) and not better and not isinstance((h.get('facts') or {}).get('why_good'), str):
        # 好操作的规则没配 alt：页面写“为什么好：”，用规则 plain 说明
        # 括注是口径说明（几秒内算补枪之类），不上页面
        # 只取第一句：“；”后面是口径说明（POS-G2“换了位置后又拿到人的写成一个时刻”、UTIL-G1“……记一个小问题”），不上页面
        better = _part(re.sub(r'（[^）]*）', '', (rules()['by_id'].get(h['rule']) or {}).get('plain', '')).split('；')[0])
    # 证据只写主句规则自己的一句；M4 并进来的好操作不贴进证据（夸奖贴在问题时刻里读着矛盾，时间也会对不上）
    evidence = _part(h.get('evidence') or '')
    evidence = evidence + '。' if evidence else ''
    blame = '—' if is_good(h) else u['mblame']
    m = {'t': _sec(h.get('t')), 'tick': _tick(h, info), 'kind': kind, 'what': sp(what), 'better': sp(better),
         'evidence': sp(evidence), 'blame': blame, 'rule': h['rule']}
    if also and sp(also) != m['better']:
        m['also'] = sp(also)                 # 只有好操作时刻带改进建议时才有这个字段
    return m


_BLAME_LEAD = re.compile(r'^这(?:颗|一颗|条|次)[^；]{0,30}?主要是[^；]*；\s*(?:你这边)?')


def _strip_blame(a):
    """“还可以”里开头的责任说明（“这颗闪主要是扔闪的队友没看你的位置；”）不是另一种思路，去掉"""
    return _part(_BLAME_LEAD.sub('', a or ''))


def _idea_key(a):
    """一条建议的字：去掉“还可以 / 先 / 再 / 一起”这类虚字后的二字片段集合"""
    t = re.sub(r'还可以|先|再|一起|就|也|的|了|，|；|、|。', '', a or '')
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _similar(a, b):
    if not a or not b:
        return False
    return len(a & b) / min(len(a), len(b)) >= IDEA_SAME


def _idea_family(a):
    """一条建议做的是哪类动作（字面不同、说的是同一件事：先扔闪再一起进 / 两个人一起出；往回退两步 / 往后退半步；
    等拆包声再打 / 听到拆包再扔……，8d R12/R20/R21）"""
    a = a or ''
    fam = set()
    if '闪' in a and ('一起' in a or '和' in a):
        fam.add('闪一起')
    if '两个人一' in a or '一起出' in a or '一起进' in a or '到身边' in a or '到位' in a:
        fam.add('等人一起')
    if '退' in a and ('白' in a or '闪' in a):
        fam.add('白屏退')
    if '拆包' in a:
        fam.add('拆包')
    if '保住' in a or '保下' in a:
        fam.add('保枪')
    # 完整性复核二：“一枪不中先收回掩体”和“开完枪先退回刚才的掩体 / AWP 打空先缩回去”是同一件事（bd R22 狙空后还站着）
    if ('收回' in a or '退回' in a or '缩回' in a) and ('掩体' in a or '打空' in a or '不中' in a):
        fam.add('打完收回')
    return fam


IDEA_SAME = 0.6          # 两条建议的二字片段重合到这个比例算同一条


def _dedupe_segs(s):
    """按“；”切开去掉完全相同的分句（M3 把两条 SHOT-G1 并进一个时刻时，各自带的“包下了之后，你隔着烟先打中”会重复）"""
    out = []
    for seg in (s or '').split('；'):
        if seg and seg not in out:
            out.append(seg)
    return '；'.join(out)


def is_neutral(h):
    """只是记一笔的好操作（ECO-G1 捡了枪但没用它打出击杀）：照样成时刻，但不把你的部分抬成“打得好”，也不上标题"""
    return is_good(h) and bool((h.get('facts') or {}).get('neutral'))


# ---------------------------------------------------------------- 主要原因 / 你的部分
CAUSE_WHO = ('你', '队友', '团队', '经济/装备差距')


def team01_who(h):
    """TEAM-01 功劳 → '你' / '你和队友' / '队友'"""
    f = h.get('facts') or {}
    w = str(f.get('credit') or f.get('who') or '')
    t = h.get('text') or ''
    if w.startswith('你和') or '是你和' in t:
        return '你和队友'
    if w in ('你', '关键一枪') or t.startswith('这回合赢主要是你') or t.startswith('你打出了这回合的人数优势') or '关键一枪是你' in t:
        return '你'
    return '队友'


def team02_who(h):
    """TEAM-02 → main_cause.who（rules_v1 TEAM-02 各分支）：
    你和队友都先倒下 → 团队；只有你 → 你；装备差距 → 经济/装备差距；连杀 → 团队；
    只有队友先倒下 / 送枪 / 留你打 1vN → 队友；残局：你残局前已死 → 队友，活到残局 → 团队"""
    f = h.get('facts') or {}
    b = (h.get('blame') or '').strip()
    why = str(f.get('why') or '')
    if '你的责任' in b and '队友' in b:
        return '团队'
    if b.startswith('你的责任'):
        return '你'
    if '合理代价' in b:
        return '经济/装备差距'
    if f.get('who') in CAUSE_WHO:
        return f['who']
    if why in ('deficit', 'gift', 'clutch'):
        return '队友'
    if why == 'close':
        return '队友' if f.get('user_died_before') else '团队'
    return '团队'


def main_cause(rhits, info):
    """主要原因 {who, text} 和它对应的 hit（可能是时刻里的 hit，用来标“关键”）"""
    by = {}
    for h in sorted(rhits, key=hit_key):
        by.setdefault(h['rule'], []).append(h)
    if info.get('won'):
        if 'TEAM-01' in by:
            h = by['TEAM-01'][0]
            w = team01_who(h)
            # 只有功劳是你一个人（含关键一枪）才记“你”；“你和队友一起打出来的”记团队（8d R6/R11：队友份额比你高）
            who = '你' if w == '你' else ('团队' if w == '你和队友' else '队友')
            return {'who': who, 'text': sp(_part(h['text']))}, h
    else:
        t02 = by.get('TEAM-02', [None])[0]
        if 'TEAM-07' in by:
            h = by['TEAM-07'][0]
            return {'who': '你' if h.get('is_user') else '队友', 'text': sp(_part(h['text']))}, h
        if 'TIME-04' in by:
            # 全队拖到太晚才打（rules_v1 第 3 场 R17：回合层面 TIME-04）：节奏是主要原因，TEAM-02 写最后输在哪一段（8d R17）
            h = by['TIME-04'][0]
            text = _part(h['text'])
            if t02 is not None and str((t02.get('facts') or {}).get('why') or '') not in ('eco', 'gift'):
                text += '；' + _part(t02['text'])
            return {'who': '团队', 'text': sp(text)}, h
        if t02 is not None:
            why = str(t02['facts'].get('why') or '')
            streak = why in ('streak', '连杀', 'TEAM-03') or (not why and '连杀' in (t02.get('text') or ''))
            if (streak or why == 'close') and 'TEAM-03' in by and any(not x.get('kind') == '思路' for x in by['TEAM-03']):
                h = next(x for x in by['TEAM-03'] if x.get('kind') != '思路')
                return {'who': '团队', 'text': sp(_part(h['text']))}, h
            if streak and 'TEAM-03' in by:
                h = by['TEAM-03'][0]
                return {'who': '团队', 'text': sp(_part(h['text']))}, h
            if 'ECO-05' in by and blame_parts(t02.get('blame'))[0] == '合理代价':
                h = by['ECO-05'][0]
                return {'who': '经济/装备差距', 'text': sp(_part(h['text']))}, h
            return {'who': team02_who(t02), 'text': sp(_part(t02['text']))}, t02
        if 'ECO-05' in by:
            h = by['ECO-05'][0]
            return {'who': '经济/装备差距', 'text': sp(_part(h['text']))}, h
    # 没有归属规则：按回合结果写固定句
    reason = info.get('reason_cn') or ''
    text = (f"这回合赢下（{reason}）" if info.get('won') else f"这回合输掉（{reason}）") if reason else \
        ('这回合赢下' if info.get('won') else '这回合输掉')
    return {'who': '团队', 'text': text}, None


def your_part(units, rhits, info, cause):
    """你的部分 {level, text}。等级表见 docs/dev/COMPOSE_NOTES.md"""
    user = info.get('user')
    mine = [u for u in units if u['death'] or u['hit'].get('is_user')]
    probs = [u for u in mine if not is_good(u['hit'])]
    goods = [u for u in mine if is_good(u['hit']) and not is_neutral(u['hit'])]
    # 这回合你死了：只有一次“掉血后退出视线”（POS-G1）不够把你的部分抬成“打得好”（e6b R2：0 伤害、2v4 阵亡）
    lvl_goods = [u for u in goods if not (info.get('user_died') and u['hit']['rule'] == 'POS-G1' and not u['clauses'])]
    contribs = []
    for u in mine:
        for h in _unit_hits(u):
            if (h.get('is_user') or h.get('death_of') == user) and not is_good(h) \
                    and not any(h is x for x in u['facts_from']):
                contribs.append(blame_parts(h.get('blame'))[1])
    major = 'major' in contribs
    minor = 'minor' in contribs
    t01 = next((h for h in sorted(rhits, key=hit_key) if h['rule'] == 'TEAM-01'), None)
    t02 = next((h for h in sorted(rhits, key=hit_key) if h['rule'] == 'TEAM-02'), None)
    death_major = any(u['death'] and u['contrib'] == 'major' for u in mine)
    you_lost_it = (t02 is not None and (t02.get('blame') or '').startswith('你的责任')) or cause.get('who') == '你'
    # 页面上标着“你的责任”的你的时刻：有它就不能写“打得好”（635 R1/R7、8d R21：打得好旁边是一次你的责任的阵亡）
    yours_bad = any(not is_good(u['hit']) and u['mblame'] in YOUR_BLAMES for u in probs)
    good_first = False
    if not info.get('won') and death_major and you_lost_it:
        level = '主要问题'
    elif info.get('won') and t01 is not None and team01_who(t01) == '你' and not yours_bad:
        level = '打得好'
    elif info.get('won') and t01 is not None and team01_who(t01) == '你':
        level = '小问题'                     # 功劳是你的，但也有一次你的责任：先写打得好的那一面，再写问题
        good_first = True
    elif major or minor:
        level = '小问题'
    elif (lvl_goods or any(h.get('is_user') and is_good(h) and not is_neutral(h) for h in rhits)) and not probs:
        level = '打得好'                     # 只有好操作、没有任何判到你的时刻（合理代价也算有）
    else:
        level = '没问题'
    # 输的回合：TEAM-02 数“先丢的人”时把你也数进去了（你倒下把领先打成平、或在落后那一段倒下），
    # 而这次死没有单独的时刻定责——你的部分不能写“打得好 / 没问题”，至少写明这次死（e6b R16）
    ul = ((t02.get('facts') or {}).get('user_lost') if t02 is not None and not info.get('won') else None)
    lost_line = lost_tail = ''
    # 这次死另有合理代价的时刻：不因为“被数进先倒下的人”抬成小问题——页面上没有一个要改的点，标题又写着
    # “你的阵亡算合理代价”，等级和句子对不上（8d R15）
    cost_dead = any(u['death'] and u['mblame'] == '合理代价' for u in mine)
    level0 = level
    if isinstance(ul, dict) and level in ('打得好', '没问题') and not cost_dead:
        # 输的回合 TEAM-02 把你数进“先倒下的人”：你的部分至少小问题（e6b R16）；
        # 这次死有单独的合理代价时刻时（8d R15），句子照写那次死，再补一句人数是从哪里丢的
        level = '小问题'
        tail = ('领先的人数从这里开始丢掉' if ul.get('gave_lead') else
                '人数从这里开始落后' if ul.get('first') and not ul.get('behind_before') else
                '扳平之后人数又从这里落后' if ul.get('first') else '是人数落后那一段倒下的人之一')
        if any(u['death'] and u['mblame'] == '合理代价' for u in mine):
            lost_tail = ''          # 这次死另有合理代价的时刻：不再补一句“人数从这里落后”，责任说法前后不一（8d R15）
        elif any(u['death'] for u in mine):
            lost_tail = _you_tail(tail)
        elif isinstance(info.get('duel_line'), dict) and _part(info['duel_line'].get('text')):
            # 这次死没有别的规则成句：按对枪动作写（审核单第 2 条，bd R3），再补一句人数是从哪里丢的
            lost_line = _part(info['duel_line']['text']) + '；' + _you_tail(tail)
            if duel_of(info['duel_line']).get('ok') is True and not major and not minor:
                level = level0          # 复核修正：判了“动作对，是枪法”，人数那半句只是事实，不把你的部分抬成小问题（bd R3）
        else:
            where = f"在{ul['place']}" if ul.get('place') and ul['place'] != '某处' else ''
            by = f"被 {ul['killer']} 打死" if ul.get('killer') else '倒下'
            lost_line = f"第 {C.fmt_sec(ul['t'])} 秒你{where}{by}，" + tail
    # 文字：先写决定等级的那一面，最多三句
    # 每一句带上秒数：选好要写哪几句以后按时间排（死在最后），不然读着先死了、后面又“你没倒”（e6b R3、128 R2）
    def _pt(h):
        t = h.get('t')
        return float(t) if isinstance(t, (int, float)) else 1e9
    def _short(h):
        sh = (h.get('facts') or {}).get('short')
        return _part(sh) if isinstance(sh, str) and sh else ''

    # 每个问题单元按它里面你自己的问题算分量：你的责任 > 小问题 > 合理代价（8d R12：阵亡主句是合理代价的 POST-02，
    # 从句里有小问题的 UTIL-01；决定等级的是另一个时刻的你的责任 SHOT-04）
    def _rank(u):
        cs = [blame_parts(h.get('blame'))[1] for h in [u['hit']] + u['clauses'] + u['annot']
              if (h.get('is_user') or h.get('death_of') == user) and not is_good(h)]
        return 0 if 'major' in cs else (1 if 'minor' in cs else 2)
    ctext = _part(cause.get('text'))

    def _ptext(u):
        # 主句就是主要原因那一句（TEAM-07 单走送枪既是主要原因又是这次死的主句）：你的部分前面写上是第几秒，
        # 不和主要原因写成同一句（8d 队友B R15）
        s = _part(u['hit']['text'])
        if s == ctext and isinstance(u['hit'].get('t'), (int, float)) and not _LEAD_SEC.match(s):
            return f"第 {C.fmt_sec(u['hit']['t'])} 秒" + s
        return s
    ps = [dict(s=_ptext(u), short=_short(u['hit']), t=_pt(u['hit']), k='p', death=u['death'], rank=_rank(u))
          for u in sorted(probs, key=lambda u: (_rank(u), not u['death'], hit_key(u['hit'])))]
    # 并进好操作时刻的小问题也算（等级是它定的时，句子里要有它，635 高分玩家50 R19）
    ps += [dict(s=_part(a['text']), short=_short(a), t=_pt(a), k='p', death=False, rank=blame_parts(a.get('blame'))[1] == 'minor' and 1 or 0)
           for u in sorted(goods, key=lambda u: hit_key(u['hit']))
           for a in u['annot'] if (a.get('is_user') or a.get('death_of') == user) and blame_parts(a.get('blame'))[1] != 'none']
    ps.sort(key=lambda x: x['rank'])
    # 有判到你的问题时，纯合理代价的（不是你的阵亡）不写进你的部分：它不是要改的点，排在前面读着像是你的错（8d R13 33.3 P250 远处打空）
    if any(x['rank'] < 2 for x in ps):
        ps = [x for x in ps if x['rank'] < 2 or x['death']]
    # 好的一面：关键的先，其次夸击杀本身的（第六轮拍板 6：你主导赢下的回合先写打出人数的那几杀，不先写捡枪）
    carried = info.get('won') and t01 is not None and team01_who(t01) == '你'
    gs = [dict(s=_part(u['hit']['text']), short=_short(u['hit']), t=_pt(u['hit']), k='g', death=False)
          for u in sorted(goods, key=lambda u: (not u['key'], carried and u['hit']['rule'] not in KILL_GOOD, hit_key(u['hit'])))]
    gs += [dict(s=_part(h['text']), short=_short(h), t=_pt(h), k='g', death=False) for h in sorted(rhits, key=hit_key)
           if h.get('is_user') and is_good(h) and not is_neutral(h)]
    mine_kills = (t01.get('facts') or {}).get('mine') if t01 is not None else None
    if not gs and t01 is not None and team01_who(t01) == '你':
        # 只有功劳归属（TEAM-01），没有单独的好操作时刻：写你自己的击杀，不和主要原因写成同一句（635 R20）
        gs = [dict(s=_part(mine_kills or t01['text']), t=1e9, k='g', death=False)]
    # 你这回合死了、但这次死一条规则都没挂上（bd R1：45 秒 CT 出生点被 对手1（第4场） 一枪爆头）：照样写明这次死，
    # 不能写“这回合你这边没有要改的”（COMPOSE_NOTES：都没有时写你的阵亡算合理代价）
    udi = info.get('user_death') if isinstance(info.get('user_death'), dict) else None
    death_line = None
    if udi and not udi.get('post_round') and not any(u['death'] for u in mine) and isinstance(udi.get('t'), (int, float)):
        kp = udi.get('killer_place_cn')
        by = (f"被{kp}的 {udi['killer']}" if kp and kp != '某处' else f"被 {udi['killer']}") if udi.get('killer') else ''
        where = f"在{udi['place_cn']}" if udi.get('place_cn') and udi['place_cn'] != '某处' else ''
        dl = info.get('duel_line')
        if dl is not None and _part(dl.get('text')):
            # 审核单第 2 条：这次死按对枪动作写（动作对，是枪法 / 不判动作的事实），不下“合理代价”的判语；
            # 只有思路规则判了这次死合理代价（Q16 中远早于 VIP 烟）才补这一句
            s0 = _part(dl['text']) + ('，这次死算合理代价' if info.get('death_cost') else '')
            dline_t = float(dl['t']) if isinstance(dl.get('t'), (int, float)) else float(udi['t'])
        else:
            s0 = (f"第 {C.fmt_sec(udi['t'])} 秒{where}{by}打死，这次死算合理代价" if by else
                  f"第 {C.fmt_sec(udi['t'])} 秒你{where}倒下，这次死算合理代价")
            dline_t = float(udi['t'])
        death_line = dict(s=s0, short='', t=dline_t, k='p', death=True, rank=2)
    for x in ps + gs:
        x['lvl'] = False
    if good_first:
        # 功劳是你的、但有一次判到你的问题：好的一面和决定等级的问题各一句，都不丢（8d R9：三杀一个字没写）
        parts = gs[:1] + ps[:1]
        for x in parts:
            x['lvl'] = True
    elif level == '打得好':
        parts = gs[:2] + ps[:1]
        if gs:
            gs[0]['lvl'] = True
    elif level == '没问题':
        parts = gs[:2]
    else:
        parts = ps[:2] + gs[:1]
        # 决定等级的是第一条判到你的问题（不是排在前面的合理代价阵亡，8d R12）
        lv = next((x for x in ps if x['rank'] < 2), ps[0] if ps else None)
        if lv is not None:
            lv['lvl'] = True
    if death_line is not None and not lost_line and not lost_tail and level in ('没问题', '小问题'):
        death_line['lvl'] = not parts
        if parts:
            # 旁边还有别的句子：只写这次死，不下“合理代价”的判语（规则没给这次死定责，和前面的问题并排读着像在开脱）
            death_line['s'] = re.sub(r'，这次死算合理代价$', '', death_line['s'])
        parts = parts + [death_line]
    if lost_line:
        parts = [dict(s=lost_line, t=float(ul['t']) if isinstance(ul.get('t'), (int, float)) else 1e9, k='p',
                      death=True, lvl=True)] + parts[:2]
    elif lost_tail:
        d = next((x for x in parts if x['death']), parts[0] if parts else None)
        if d is not None:
            d['tail'] = lost_tail
        else:
            parts = [dict(s=lost_tail, t=1e9, k='p', death=False, lvl=True)]
    parts = _yp_order(parts, good_first=good_first)
    text = _join(parts)
    if not text:
        dead = [u for u in mine if u['death']]
        if dead and dead[0]['mblame'] == '合理代价':
            text = '你的阵亡算合理代价'
        elif dead or (udi and not udi.get('post_round')):
            # 你死了，但这次死记在队友或团队（同一个人连杀里的一个、两个方向夹击）：写明这次死，不写“没有要改的”（10ce R13 等）
            if udi and isinstance(udi.get('t'), (int, float)) and udi.get('killer'):
                where = f"在{udi['place_cn']}" if udi.get('place_cn') and udi['place_cn'] != '某处' else ''
                text = f"第 {C.fmt_sec(udi['t'])} 秒{where}被 {udi['killer']} 打死，这次死主要原因不在你"
            else:
                text = '这次死主要原因不在你'
        else:
            text = '这回合你这边没有要改的'
    if text == cause.get('text') and mine_kills:
        text = _part(mine_kills)
    return {'level': level, 'text': sp(text)}


def _you_tail(tail):
    """TEAM-02 把你数进先倒下的人时补的那半句：“你倒下后人数从这里开始落后”；“扳平之后……”写成“扳平之后你倒下，人数又从这里落后”
    （原来拼成“你倒下后扳平之后人数又从这里落后”，128 R1）"""
    if tail.startswith('是'):
        return '你' + tail
    if tail.startswith('扳平之后'):
        return '扳平之后你倒下，' + tail[len('扳平之后'):]
    return '你倒下后' + tail


YP_MAX_SEGS = 3             # 你的部分最多三句（按“；”数，128 / 8d R3 R12 R22）
YP_MAX_CHARS = 200          # 你的部分太长时（把时刻全文都抄了进来，8d R12）先压短、再丢不决定等级的句子，至少留一句
_CONN = re.compile(r'^(?:另外|不过|但是?)[，,]?\s*')
_SEC_IN = re.compile(r'第\s*(\d+(?:\.\d+)?)\s*秒')


def _compress(x):
    """一句压短：规则给的短句（facts.short）> 第一个分句；压不了返回 False"""
    vs = _part(x['s']).split('；')
    if x.get('short') and len(x['short']) < len(_part(x['s'])):
        x['s'], x['short'] = x['short'], ''
        return True
    if len(vs) > 1:
        x['s'] = vs[0]
        return True
    return False


def _yp_order(parts, good_first=False):
    """你的部分：选好的几句按时间排（同一秒保持原来的先后）。超过三句 / 太长时：先压短、再丢不决定等级的
    （时间靠后的先丢），然后压短决定等级的；决定等级的好坏两面都在时最后才丢好的一面（8d R9：先把三枪的原因压成一句，
    “三杀”那一面留着；8d R12：决定等级的 SHOT-04 不能被一长句合理代价的阵亡挤掉）。
    前后一好一坏时后一句加“不过 / 另外”；后面还写了你的死时，前面那句不写“但你没倒”"""
    parts = [dict(x) for x in parts if _part(x.get('s'))]
    for i, x in enumerate(parts):
        x['i'] = i

    def segs(xs):
        return sum(len(_part(x['s']).split('；')) + (1 if x.get('tail') else 0) for x in xs)

    def chars(xs):
        return len('；'.join(_part(x['s']) + ('；' + x['tail'] if x.get('tail') else '') for x in xs)) + 2 * max(0, len(xs) - 1)

    def too_long(xs):
        return len(xs) > 1 and (segs(xs) > YP_MAX_SEGS or chars(xs) > YP_MAX_CHARS)

    while too_long(parts):
        drop = sorted([x for x in parts if not x['lvl']], key=lambda x: (x['k'] == 'p', -x['t'], -x['i']))
        if drop:
            if not _compress(drop[0]):
                parts = [x for x in parts if x is not drop[0]]
            continue
        # 决定等级的几句：先压问题、再压好的一面，都压不了才丢好的一面（主要原因里已经写了功劳）
        lv = sorted(parts, key=lambda x: (x['k'] != 'p', -len(_part(x['s']))))
        if any(_compress(x) for x in lv):
            continue
        gd = [x for x in parts if x['k'] == 'g']
        if not gd:
            break
        parts = [x for x in parts if x is not gd[0]]

    # 按句子开头写的那一秒排（TIME-06 的 t 是阵亡，句子从被闪那一秒写起，51920faa R3）
    def lead_t(x):
        m = _LEAD_SEC.match(_CONN.sub('', _part(x['s'])))
        return float(re.search(r'[−\-]?\d+(?:\.\d+)?', m.group(0)).group(0).replace('−', '-')) if m else x['t']
    parts = sorted(parts, key=lambda x: (lead_t(x), x['i']))
    if good_first:
        # 功劳是你的、降成小问题：先写打得好的那一面，再“不过……”写问题（等级表；8d R9 原来先写三枪全空、
        # 再“另外”写扳回人数；10ce R6 先写阵亡、后面又写阵亡前的两杀）
        parts = [x for x in parts if x['k'] == 'g'] + [x for x in parts if x['k'] != 'g']
    out, prev = [], None
    for j, x in enumerate(parts):
        sx = _CONN.sub('', _part(x['s']))
        if any(y['death'] for y in parts[j + 1:]):
            sx = sx.replace('但你没倒，也没打死他', '没打死他')
        if prev is not None and x['k'] != prev:
            sx = ('不过' if x['k'] == 'p' else '另外') + sx
        out.append(sx)
        if x.get('tail'):
            out.append(x['tail'])
        prev = x['k']
    return _yp_trim(out)


def _yp_trim(parts):
    """你的部分：后一句开头的“第 N 秒”和前一句写过的同一秒重复时去掉（128 R6“第 29.5 秒……；第 29.5 秒拿到人后”）；
    长度已经在 _yp_order 里按等级压过，这里只兜底（一句也超长时不再动）"""
    out = []
    for p in parts:
        p = _part(p)
        if not p:
            continue
        if out:
            m = re.match(r'^(另外|不过)?\s*第\s*(\d+(?:\.\d+)?)\s*秒[，,]?\s*', p)
            if m and m.group(2) in _SEC_IN.findall(out[-1]):
                p = (m.group(1) or '') + p[m.end():]
        out.append(p)
    return out


def _kill_sec(h):
    f = h.get('facts') or {}
    kt = f.get('kill_t')
    if isinstance(kt, list):
        kt = kt[-1] if kt else None
    return float(kt) if isinstance(kt, (int, float)) else (float(h['t']) if isinstance(h.get('t'), (int, float)) else None)


def _short_tails(keep):
    """UTIL-G2“……随后你把他打死”、POS-G2“……换了位置之后又打死了 X”：那一杀自己另有时刻时只写前半句（facts.text_short），
    同一个击杀不在三个时刻里各写一遍（8d R7 28.6 秒那一杀）"""
    for u in keep:
        h = u['hit']
        f = h.get('facts') or {}
        if h['rule'] not in TAIL_RULES or not f.get('text_short'):
            continue
        vic, kt = (f.get('victim'), f.get('kill_t')) if h['rule'] == 'UTIL-G2' else (f.get('victim2'), f.get('t2'))
        if not vic or not isinstance(kt, (int, float)):
            continue
        for o in keep:
            if o is u:
                continue
            if any(x['rule'] in KILL_GOOD and victim_of(x) == vic and _kill_sec(x) is not None
                   and abs(_kill_sec(x) - float(kt)) <= 1.0 for x in [o['hit']] + o['clauses']):
                u['hit'] = dict(h, text=f['text_short'])
                break


# ---------------------------------------------------------------- 只写值得分析的点（第六轮拍板 2）
# 阈值在 thresholds/salience.json（compose 经 info['salience'] 传进来）；这里只是没有那份文件时的默认值
SAL_DEFAULT = {
    'enabled': True,
    'kill_match_s': 1.5,            # 好操作对上时间线里你的哪一次击杀：同一个人、相差不超过这么多秒
    'multi_kill_n': 2,              # 多杀：这么多秒内你拿了 >= n 个
    'multi_kill_s': 10.0,
    'keep_mate_problem_won': False,  # 赢下的回合里，队友自己的问题（队友或团队、没挂在你身上）要不要写
    'util_rules': ['UTIL-G1', 'UTIL-G2', 'UTIL-G3'],
    'always_good': ['POS-06', 'TIME-G1', 'POST-01'],
    'never_good': ['POS-G1', 'POS-G2'],
    'keep_minor_rules': ['TEAM-05'],
    'keep_minor_duel_rules': ['SHOT-01', 'SHOT-02', 'SHOT-05'],
    'eco_pick_buys': ['eco', '强起', '半起'],
    'family': {'SHOT-G1': '停稳', 'SHOT-G2': '停稳'},
}


def _sal_th(info):
    t = dict(SAL_DEFAULT)
    t.update({k: v for k, v in (info.get('salience') or {}).items() if not k.startswith('_')})
    return t


def _user_kills(info):
    """时间线里你这回合的击杀：[(秒, 对手, 开局首杀, 之前人数 (us, them), 之后人数)]"""
    user = info.get('user')
    alive = info.get('alive') or []
    out = []
    for k in info.get('kills') or []:
        if k.get('killer') != user or k.get('teamkill') or k.get('killer') == k.get('victim') or k.get('post_round'):
            continue
        ev = f"{k['killer']} → {k['victim']}"
        i = next((j for j, a in enumerate(alive) if a.get('event') == ev), None)
        after = (alive[i]['us'], alive[i]['them']) if i is not None else None
        before = (alive[i - 1]['us'], alive[i - 1]['them']) if i is not None and i > 0 else None
        out.append(dict(t=float(k['t']), victim=k['victim'], opening=bool(k.get('opening')), before=before, after=after))
    return out


def _kill_score(u, info, th):
    """好操作单元里夸的那几次击杀够不够“亮眼”：开局首杀、多杀里的一个、残局（你一个人时）拿的、
    把人数扳平 / 扳回来（拿之前我方不多于对面）、赢下回合的最后一杀。返回 (分数, 理由列表)"""
    kills = _user_kills(info)
    hs = [x for x in [u['hit']] + u['clauses'] if is_good(x)]
    got = []
    for h in hs:
        v, kt = victim_of(h), _kill_sec(h)
        if not v or kt is None:
            continue
        k = next((k for k in kills if k['victim'] == v and abs(k['t'] - kt) <= th['kill_match_s']), None)
        if k is not None and k not in got:
            got.append(k)
    score, why = 0, []
    for k in got:
        near = [x for x in kills if abs(x['t'] - k['t']) <= th['multi_kill_s']]
        if k['opening']:
            score += 1
            why.append('首杀')
        if len(near) >= th['multi_kill_n']:
            score += 1
            why.append('多杀')
        b, a = k['before'], k['after']
        if b and b[0] == 1 and b[1] >= 1:
            score += 2
            why.append('残局')
        elif b and b[0] <= b[1]:
            score += 1
            why.append('扳人数')
        if a and a[1] == 0 and info.get('won'):
            score += 1
            why.append('最后一杀')
    if got and info.get('carried'):
        # 你主导赢下的回合（TEAM-01 功劳是你）：你的击杀就是打出这回合的那几杀，照写（第六轮拍板 6 要先写好的一面，bd R6）
        score += 1
        why.append('主导')
    return score, why


def _good_standout(u, info, th):
    """好操作单元值不值得单独写：返回 (分数, 理由)；分数 0 = 不写"""
    h = u['hit']
    rid = h['rule']
    f = h.get('facts') or {}
    if is_neutral(h) or rid in th['never_good']:
        return 0, []
    if rid in th['always_good']:
        # 只因为击杀栏里那边最后一个守点的倒了才往那边动：常规反应，不算亮眼（8d 队友B R12 48.1）
        if rid == 'TIME-G1' and f.get('only_death') and not f.get('anchors'):
            return 0, []
        return 1, [rid]
    if rid in th['util_rules']:
        # 道具要有看得到的效果：火烧到人、雷打掉血再拿下、拖时间的道具炸到 / 烧到了人
        # UTIL-G3 掉血后退出视线、改用道具：是对前面那次失误的处理，和那个问题时刻一起看（rules_v1：“失误掉血后，处理得对”）
        if rid == 'UTIL-G2' or (rid == 'UTIL-G1' and f.get('burned')) or (rid == 'UTIL-G3' and (f.get('hurt_by_nade') or f.get('trigger') == 'hurt')):
            return 1, ['道具']
        if rid == 'UTIL-G3' and f.get('trigger') == 'retake':
            return 1, ['道具']               # 审核单第 27 条：回防先往包点扔了两颗以上道具清点，写好的一面
        sc, why = _kill_score(u, info, th)
        return sc, why
    if rid == 'ECO-G1':
        # 捡枪升级：eco / 强起的回合用捡来的枪打出了击杀
        return (1, ['捡枪']) if info.get('buy') in th['eco_pick_buys'] else (0, [])
    return _kill_score(u, info, th)


def _salience(allu, info, cause):
    """第六轮拍板 2：每回合只写值得分析的点。
    一定留：关键时刻（阵亡、主要原因、关键一枪）、判到你的责任的问题、总结里引用到的（info['protect']，标题 / 主要原因 /
    你的部分都从留下来的单元里取）；
    问题：你的小问题 / 合理代价（不是阵亡）不写；队友自己的问题在赢下的回合不写（keep_mate_problem_won）；
    好操作：只留亮眼的（_good_standout），同一类夸奖一回合只留最强的一个。
    返回 (留下的, 去掉的)"""
    th = _sal_th(info)
    if not th['enabled']:
        return list(allu), []
    user = info.get('user')

    def _hs(u):
        return [x for x in _unit_hits(u) if not any(x is y for y in u['facts_from'])]

    def _in(u, g):
        return any(x['rule'] in g and (x.get('is_user') or x.get('death_of') == user) for x in _hs(u))
    # 总结引用的（回合, 一组规则）只保住一个单元：这组里最强的那个（never_good 的排最后）；关键 / 阵亡单元已经在这组里就不用再保
    # （8d R7：一条强项保住了这回合两次 AWP 击杀和两次换位，同一类夸奖只留一个、AWP 换位不单写都失效了）
    groups = [tuple(g) if isinstance(g, (list, tuple)) else (g,) for g in (info.get('protect') or ())]
    must = [u for u in allu if u['key'] or u['death']]
    prot = set()
    for g in groups:
        if any(_in(u, g) for u in must):
            continue
        cands = [u for u in allu if _in(u, g)]
        if not cands:
            continue

        def _pk(u):
            gh = u['hit']
            sc = _good_standout(u, info, th)[0] if is_good(gh) else (2 if u['contrib'] == 'major' else 1)
            return (gh['rule'] in th['never_good'], -sc, hit_key(gh))
        prot.add(id(min(cands, key=_pk)))
    keep, drop, goods = [], [], []
    for u in allu:
        if u['key'] or u['death'] or id(u) in prot:
            keep.append(u)
            continue
        h = u['hit']
        if is_good(h):
            sc, why = _good_standout(u, info, th)
            if sc > 0:
                goods.append((u, sc))
            else:
                drop.append(u)
            continue
        mine = [blame_parts(x.get('blame'))[1] for x in [h] + u['clauses'] + u['annot']
                if (x.get('is_user') or x.get('death_of') == user) and not is_good(x)]
        f = h.get('facts') or {}
        if 'major' in mine:
            keep.append(u)
        elif h.get('is_user') and blame_parts(h.get('blame'))[1] == 'minor' and (
                h['rule'] in th['keep_minor_rules']
                or (h['rule'] in th['keep_minor_duel_rules'] and f.get('killed') is False)):
            # 拉胯的小问题照写：能补没补（人数）、对枪动作不对又没打死他（这一枪的结果）
            keep.append(u)
        elif h.get('is_user') or mine:
            drop.append(u)                       # 你的小问题 / 合理代价，没挂在阵亡上：影响小
        elif u['mblame'] == '队友或团队' and (not info.get('won') or th['keep_mate_problem_won']):
            keep.append(u)
        else:
            drop.append(u)
    # 同一类夸奖一回合只留最强的一个（分数高的；一样高留先发生的）；关键 / 总结引用的那一个已经留下时，同类的不再写
    best = {}
    for u in keep:
        if is_good(u['hit']):
            best[th['family'].get(u['hit']['rule'], u['hit']['rule'])] = (u, 1e9)
    for u, sc in goods:
        fam = th['family'].get(u['hit']['rule'], u['hit']['rule'])
        cur = best.get(fam)
        if cur is None or sc > cur[1]:
            best[fam] = (u, sc)
    for u, sc in goods:
        fam = th['family'].get(u['hit']['rule'], u['hit']['rule'])
        (keep if best[fam][0] is u else drop).append(u)
    return keep, drop


# ---------------------------------------------------------------- 对枪动作判定（审核单第 2 条，SHOT-06）
DUEL_RULE = 'SHOT-06'
# 判出“动作对”时不当这次死的主因（手册 1.4：UTIL-01、“原地接枪”这类；TEAM-04“一个人先露头”的通用分支），不写
DUEL_OK_DEMOTE = ('UTIL-01', 'TEAM-04', 'POS-01', 'POS-05')   # 复核修正：站位（POS-01 “还站在原地”、POS-05）也不当主因（审核单第 2 条）；
# POS-01 打人时两个人都打得到 / 狙空后没收这两支说的是上一串，不在这里去掉
# 不判动作、也不加从句的结论（被闪 / 隔烟 / 还没看见他 / 续扫 / 边走边开打中了）：只在这次死没有别的规则时写进你的部分
DUEL_TWO = 'two_seeing'
DUEL_TWO_DROP = ('SHOT-01', 'SHOT-02', 'SHOT-03', 'SHOT-05')
PREFIRED_CLAUSE = '他一直瞄着你出来的位置'       # 审核单第 35 条（输的一面）
DUEL_NO_END = ('flashed', 'smoke')            # 这两种判定句只写到“开枪时被闪着 / 隔着烟交火”，写进你的部分时补上被打死
TEAM04_EXEMPT_ALONE = ('open', 'solo', 'traded', 'ct_post')
INTENT_OK_DROP = ('TEAM-04', 'POS-05', 'UTIL-01')  # 主句判了“有意图、合理”（POST-03 拦拆）时不再并进来的从句


def duel_of(h):
    return ((h or {}).get('facts') or {}).get('duel') or {}


def _duel_lift(h):
    """动作有错、又没有别的开枪规则把这次死写成时刻（covered_by 为空）：SHOT-06 自己成句，进 M1（和 SHOT-01/02/05 同档）。
    其余（动作对、不判、已被 SHOT-01..05 写了）留在 display=summary，只给你的部分 / 从句用"""
    d = duel_of(h)
    if h.get('rule') != DUEL_RULE or d.get('ok') is not False or d.get('covered_by'):
        return h
    f = dict(h.get('facts') or {})
    f.pop('display', None)
    return dict(h, facts=f)


def _duel_at(duels, t):
    """这次阵亡（秒 t）对应的 SHOT-06：death_t 相差 ≤ DEATH_SAME_S 里最近的一条"""
    best = None
    for h in duels:
        dt = h.get('death_t')
        if isinstance(dt, (int, float)) and abs(float(dt) - t) <= DEATH_SAME_S + 1e-6 and (
                best is None or abs(float(dt) - t) < abs(float(best['death_t']) - t)):
            best = h
    return best


def _duel_clause_hit(D, part):
    """SHOT-06 当阵亡单元的从句（主句是别的规则）：part='full' 写整句（动作对，是枪法 + 部位），
    part='pos' 只写站位那半句（开打的位置 X 和 Y 都打得到）。责任不算你的（kind 判定、blame —）"""
    d = duel_of(D)
    f = dict(D.get('facts') or {})
    f.pop('display', None)
    if part == 'pos':
        txt = _part(d.get('clause') or '')
        return dict(D, kind='判定', blame='—', text=f"第 {C.fmt_sec(D['t'])} 秒{txt}" if isinstance(D.get('t'), (int, float)) else txt,
                    facts=dict(f, clause=txt), alt=D.get('alt') or '')
    return dict(D, kind='判定', blame='—', facts=dict(f, clause=_LEAD_SEC.sub('', _part(D.get('text')))), alt='')


# ---------------------------------------------------------------- 一回合
def _good_overlaps(goods):
    """两条夸奖说的是同一件事、或者一条的“还可以”恰好是另一条夸的事，先在这里处理：
    - SHOT-G2 的“还可以拿到人后挪个位置”（facts.alt_if_no_posg2）：同一次击杀 POS-G2 已经夸了“马上挪开了位置”，去掉这句（8d R11）
    - POS-G1“被打残后马上退出视线”和 UTIL-G3“掉了一大截血后没回去硬架，退出视线、扔道具拖住”是同一次掉血：只留 UTIL-G3（635 高分玩家45 R28）"""
    posg2_kills = []
    for h in goods:
        if h['rule'] == 'POS-G2':
            kt = (h.get('facts') or {}).get('kill_t')
            posg2_kills += [float(x) for x in (kt if isinstance(kt, list) else [kt]) if isinstance(x, (int, float))]
    hurt_t = [float((h.get('facts') or {}).get('trigger_t')) for h in goods
              if h['rule'] == 'UTIL-G3' and (h.get('facts') or {}).get('trigger') == 'hurt'
              and isinstance((h.get('facts') or {}).get('trigger_t'), (int, float))]
    out = []
    for h in goods:
        f = h.get('facts') or {}
        if (h['rule'] == 'SHOT-G2' and f.get('alt_if_no_posg2') and isinstance(f.get('kill_t'), (int, float))
                and any(abs(float(f['kill_t']) - k) <= 0.2 for k in posg2_kills)):
            h = dict(h, alt='')
        if (h['rule'] == 'POS-G1' and f.get('sub') == '残血' and isinstance(h.get('t'), (int, float))
                and any(abs(float(h['t']) - t) <= 1.5 for t in hurt_t)):
            continue
        out.append(h)
    return out


def merge_round(hits, info):
    """一回合的 hit → 定稿。hits 里可以混着 round / match 级的，这里只挑本回合用得上的"""
    user = info.get('user')
    hits = sorted((norm_hit(h) for h in hits), key=hit_key)
    # 买法（ECO-01..04）不当“问题”时刻写、不分责任：用户规则是买法不说对错，只在经济栏给“另一种思路”（8d R16 买枪时）
    hits = [dict(h, kind='思路') if h['rule'] in BUY_RULES and h.get('kind') == '问题' else h for h in hits]
    # 审核单第 32 条：eco 局一次阵亡只给一个方向的建议。有“换个方向去找对面拿步枪或 AWP 的人”（TEAM-03 思路，decisions #24）时
    # 只留它：同一次阵亡上别的规则的“还可以”都清掉（8d R16 原来三条：同时出 / 往队友靠 / 找长枪），它写进阵亡时刻的还可以
    hunt_ts = sorted(float(h['death_t']) for h in hits if h['rule'] == 'TEAM-03' and h.get('kind') == '思路'
                     and h.get('death_of') == user and isinstance(h.get('death_t'), (int, float)) and clean_alt(h['alt']))

    def _on_hunt(h):
        dt = h.get('death_t')
        return (h.get('death_of') == user and isinstance(dt, (int, float))
                and any(abs(float(dt) - t) <= DEATH_SAME_S + 1e-6 for t in hunt_ts))
    hunt = [h for h in hits if h['rule'] == 'TEAM-03' and h.get('kind') == '思路' and _on_hunt(h)]
    if hunt:
        hits = [dict(h, alt='') if h.get('alt') and _on_hunt(h) and not any(h is x for x in hunt) else h for h in hits]
    # 审核单第 2 条：每次被枪打死都有一条 SHOT-06（对枪动作判定）。动作有错、没被别的开枪规则写过的提成时刻（_duel_lift）；
    # 其余只给这次死的从句 / 你的部分用
    hits = [_duel_lift(h) for h in hits]
    duels = [h for h in hits if h['rule'] == DUEL_RULE and h.get('death_of') == user and isinstance(h.get('death_t'), (int, float))]
    # TEAM-03 收同一个凶手、倒下的人在这串连杀里的 POS-02（rules_v1 TEAM-03“同一个 K 的 POS-02 并进这里”）：
    # 连杀那句已经写了这几个人怎么倒下的，POS-02 不再单独成时刻、也不并进别的时刻（128 R4 两个时刻说同一次死）
    streaks = [((h.get('facts') or {}).get('killer'), set((h.get('facts') or {}).get('victims') or []))
               for h in hits if h['rule'] == 'TEAM-03' and h.get('kind') != '思路']
    hits = [h for h in hits if not (h['rule'] == 'POS-02' and not h.get('is_user') and any(
        k and k == (h.get('facts') or {}).get('killer') and (h.get('facts') or {}).get('victim') in vs for k, vs in streaks))]
    rhits = [h for h in hits if h['scope'] == 'round' or h['rule'] in ROUND_ONLY]
    mhits = [h for h in hits if h['scope'] == 'moment' and h['rule'] not in ROUND_ONLY
             and not h['facts'].get('summary_only') and h['facts'].get('display') not in ('summary', 'merged')]
    used = set()
    units, ideas_src = [], []

    # M1 / M2：用户每次阵亡
    dead_ts = []
    for dt, grp in _cluster_deaths(mhits, user):
        D = _duel_at(duels, dt)
        dd = duel_of(D)
        probs = [h for h in grp if not is_good(h) and h.get('kind') != '思路']
        ideas_src += [h for h in grp if h.get('kind') == '思路']      # “另一种思路”不当主因，只进 ideas
        used |= {id(h) for h in grp if not is_good(h)}
        if dd.get('ok') is True:
            # 手册 1.4：判出“动作对、是枪法”时，道具（UTIL-01）、“一个人先露头”（TEAM-04 通用分支）不当这次死的主因，也不再写
            probs = [h for h in probs if not (h['rule'] in DUEL_OK_DEMOTE and not is_entry(h)
                                              and not (h['rule'] == 'POS-01' and (h.get('facts') or {}).get('branch') != 'stay'))]
            # “撞上人时第一枪还没停稳”和“贴脸边走边开，动作对”并排读着矛盾（审核单第 13 条：10 米内边走边开是正常打法，8d R18）
            probs = [dict(h, text=h['text'].replace('撞上人时第一枪还没停稳，没中，', '撞上人，'))
                     if h['rule'] == 'TIME-05' else h for h in probs]
        if dd.get('key') == DUEL_TWO:
            # 审核单第 10 条：开打时两个人都看得见你，一个人打不了两把枪，输了归站位：这次死不写急停 / 准星类问题
            # 只去掉这一次开打的那一串（和判定同一时刻）；更早对另一个人的那串照写（8d R3 42.6）
            probs = [h for h in probs if not (h['rule'] in DUEL_TWO_DROP and h.get('is_user') and isinstance(h.get('t'), (int, float))
                                              and isinstance(D.get('t'), (int, float)) and abs(float(h['t']) - float(D['t'])) <= CLAUSE_SAME_S)]
            # 复核修正：TIME-05 句子里的“撞上人时第一枪还没停稳”也是急停类结论，同样不写（bd R24 66.1）
            probs = [dict(h, text=h['text'].replace('撞上人时第一枪还没停稳，没中，', '撞上人，'))
                     if h['rule'] == 'TIME-05' else h for h in probs]
        if dd.get('key') == 'transfer' and dd.get('transfer_from'):
            # 复核修正（第 11 条）：打死一个人后没松开、一口气扫到下一个人身上——这次死按续扫写，不再写“打成 NvM 后你还站在原地”
            probs = [h for h in probs if not (h['rule'] == 'POS-01' and (h.get('facts') or {}).get('branch') == 'stay'
                                              and (h.get('facts') or {}).get('victim') == dd['transfer_from'])]
        # 复核修正：豁免记录在 SHOT-06 的 facts 顶层（rules/__init__._attach_team_exempt），原来只读 facts.duel，从来不生效
        te = dd.get('team_exempt') or ((D or {}).get('facts') or {}).get('team_exempt')
        if te in TEAM04_EXEMPT_ALONE:
            # 审核单第 7 / 17 条：这次先接触按 TEAM-04 豁免了（开局控图 / 单走一侧后出 / 进点被换 / CT 守位），
            # UTIL-01 不再用“一个人出去接枪”把“一个人先露头”写回来
            probs = [dict(h, text=h['text'].replace('一个人出去接枪', '出去接枪')) if h['rule'] == 'UTIL-01' else h for h in probs]
        if not probs:
            continue
        dead_ts.append(dt)
        main, rest = _m1(probs)
        u = _unit(main, death=True)
        # UTIL-01 第 5 道门（util facts.contra_rules / fact_only_rules）：主句已经在说“别出去 / 留着等”时 UTIL-01 不并成从句、
        # 它的还可以也不进 ideas；TIME-05 / POS-05 主句下只留“身上 X 都还在”这样的事实半句（clause_fact），不给还可以
        rest2 = []
        intent_ok = (main.get('facts') or {}).get('branch') == '拦拆'
        has_entry = any(is_entry(h) for h in probs)
        main_stay = main['rule'] == 'POS-01' and (main.get('facts') or {}).get('branch') == 'stay'
        for h in rest:
            f = h.get('facts') or {}
            if main_stay and h['rule'] == DUEL_RULE and (duel_of(h).get('peek') == 'me_peek' or duel_of(h).get('moving')):
                # 复核修正：主句“你还站在原地…随后被他打死”，判定却是“拉出去…还没停住”：两句方向相反、死也写了两遍，只留主句（bd R19）
                u['facts_from'].append(h)
                continue
            if main_stay and h['rule'] == 'POS-05':
                # 完整性复核二：主句“开枪没中后你还站在原地”，从句却是“走了一大段去接枪”、还可以又是“留在原来的位置”，
                # 三句方向打架，只留主句（8d 队友B R18）
                u['facts_from'].append(h)
                continue
            if has_entry and h['rule'] == 'UTIL-01':
                # 审核单第 28 条（手册 UTIL-E）：第一个进点倒下、后面的人没跟上记团队，进点道具也按团队写；
                # 你这边只留对枪那一条（第 2 场 R5：只留准星没放好）
                u['facts_from'].append(h)
                continue
            if intent_ok and h['rule'] in INTENT_OK_DROP and not is_entry(h):
                # Q18：下包后拿着火往包那边挪是去拦拆包（合理），“比附近的队友先露头 / 人多时去接枪”不再并进来（8d R21）
                u['facts_from'].append(h)
                continue
            if h['rule'] == 'UTIL-01' and main['rule'] in (f.get('fact_only_rules') or ()) and f.get('clause_fact'):
                rest2.append(dict(h, text=f['clause_fact'], alt='', facts=dict(f, clause=f['clause_fact'])))
            elif h['rule'] == 'UTIL-01' and main['rule'] in (f.get('contra_rules') or ()):
                u['facts_from'].append(h)
            else:
                rest2.append(h)
        rest = rest2
        if main['rule'] == 'POST-01':
            # 主因是“人数劣势没保枪”：教他怎么把这次拉枪打好的从句和思路（先扔闪再出、换个时机再拉、等队友一起出）
            # 和“退到远处保枪”方向相反，不并进来、也不进 ideas（e6b R16、8d R10）
            rest = [h for h in rest if h['rule'] not in POST01_FIGHT]
            ideas_src[:] = [h for h in ideas_src if not (h['rule'] in POST01_FIGHT and h.get('death_of') == user
                                                         and isinstance(h.get('death_t'), (int, float))
                                                         and abs(float(h['death_t']) - dt) <= 1.0)]
        # POST-01 的豁免 / 合理代价分支（“半场最后一回合，去打是对的”）不当从句：主句在说一个人先进没机会，
        # 后面接一句“去打是对的”读着自相矛盾（8d R12）；只留作背景事实
        u['facts_from'] += [h for h in rest if is_post01_background(h)]
        # 合理代价 / 保枪分支的 POST-01 只当背景，但它的“还可以”（往远离包点一侧撤、保住枪，Q12）照样进 ideas
        ideas_src += [h for h in rest if is_post01_background(h) and clean_alt(h['alt'])]
        rest = [h for h in rest if not is_post01_background(h)]
        for h in sorted(rest, key=hit_key):
            if h['rule'] in SHOT_RULES and u['mblame'] == '合理代价':
                s = _unit(h)                          # M2：开枪问题照记（责任用开枪规则自己的），单独成时刻
                # 复核修正：这次死判了动作有错（SHOT-06 ok False）时不补“这次死本身算合理代价”（拍板 8：不一刀切）
                s['suffix'] = '' if dd.get('ok') is False else M2_SUFFIX
                units.append(s)
            elif h.get('kind') == '思路':
                ideas_src.append(h)
            else:
                u['clauses'].append(h)
        # 对枪动作判定：主句是别的规则（不是开枪类）、判定也没被开枪规则写过时，这次死照样带上它（审核单第 2 条）——
        # 动作对写整句（动作对，是枪法 + 双方部位）；两个人都打得到（第 10 条）写站位那半句，还可以先挡掉一边
        if D is not None and main['rule'] not in SHOT_RULES and not dd.get('covered_by') \
                and not any(h['rule'] == DUEL_RULE for h in probs):
            if dd.get('key') == DUEL_TWO and dd.get('clause'):
                c2 = _duel_clause_hit(D, 'pos')
                if better_text(main.get('alt')):
                    c2 = dict(c2, alt='')        # 主句有自己的还可以：“先挡掉一边”只在主句没有时补上，不再进 ideas（还可以只给一个思路）
                u['clauses'].append(c2)
            elif dd.get('ok') is True and main['rule'] not in ('POS-01', 'POS-05'):
                # 复核修正：主句是站位（“打他时两个人都打得到”“走了一大段去接”）时不再并“动作对，是枪法”，
                # 两句方向相反（8d R13：一边走一边开枪……后面又写动作对）
                u['clauses'].append(_duel_clause_hit(D, 'full'))
        # 第 35 条输的一面：同一条线重复拉、他一直瞄着你出来的位置
        if main['rule'] == 'POS-02' and dd.get('prefired') and PREFIRED_CLAUSE not in (main.get('text') or ''):
            u['hit'] = dict(main, text=_part(main['text']) + '，' + PREFIRED_CLAUSE)
        units.append(u)
        used |= {id(h) for h in probs}

    # 你死了、但这次死没有一条规则成句（或动作对、道具 / 先露头不再当主因）：你的部分用对枪动作的判定写这次死，
    # 取代原来的默认句“这次死算合理代价”（审核单第 2 条）
    udi = info.get('user_death') if isinstance(info.get('user_death'), dict) else None
    duel_line = None
    death_cost = False
    if udi and not udi.get('post_round') and isinstance(udi.get('t'), (int, float)) \
            and not any(abs(float(udi['t']) - t) <= DEATH_SAME_S + 1e-6 for t in dead_ts):
        ut = float(udi['t'])
        duel_line = _duel_at(duels, ut)
        if duel_line is not None and clean_alt(duel_line.get('alt')):
            ideas_src.append(duel_line)          # 两个人都打得到：还可以先挡掉一边
        # 只有“另一种思路”判了这次死合理代价（Q16 中远早于 VIP 烟、隔烟被打死）：用思路那一句写这次死，补“这次死算合理代价”
        cost_h = next((h for h in mhits if h.get('kind') == '思路' and h.get('death_of') == user
                       and isinstance(h.get('death_t'), (int, float)) and abs(float(h['death_t']) - ut) <= DEATH_SAME_S + 1e-6
                       and (h.get('facts') or {}).get('death_verdict') == '合理代价' and _part(h.get('text'))), None)
        if cost_h is not None:
            duel_line, death_cost = dict(cost_h, t=ut), True
        elif duel_line is not None and duel_of(duel_line).get('key') in DUEL_NO_END \
                and not re.search(r'打死|打在你', duel_line.get('text') or ''):
            duel_line = dict(duel_line, text=_part(duel_line['text']) + '，最后被他打死')
    info = dict(info, duel_line=duel_line, death_cost=death_cost)

    # 其余问题 / 判定 → 各自一个单元；“思路”只进 ideas
    for h in mhits:
        if id(h) in used or is_good(h):
            continue
        if h.get('kind') == '思路':
            ideas_src.append(h)
            continue
        units.append(_unit(h))
    units = _merge_repeats(_m5(units))

    # 这回合判了“人数劣势没保枪”（POST-01 问题分支）：之后的 SHOT-G4“没躲，主动去找”不再夸主动找人，只写事实（635 高分玩家50 R7）
    p01 = [h for h in mhits if h['rule'] == 'POST-01' and h.get('is_user') and not is_good(h)
           and not is_post01_background(h) and isinstance(h.get('t'), (int, float))]
    if p01:
        # 从“局面已经打不赢”那一刻算（facts.t_s），不是从阵亡算（635 高分玩家50 R7：第 85 秒就打不赢了，104 秒的击杀还在夸主动）
        t_p = min(float((h.get('facts') or {}).get('t_s', h['t'])) for h in p01)
        for i, h in enumerate(mhits):
            f = h.get('facts') or {}
            if h['rule'] == 'SHOT-G4' and f.get('variant') == '主动' and (h.get('t') or 0) >= t_p and f.get('text_plain'):
                h2 = dict(h)
                h2['text'] = f['text_plain']
                h2['facts'] = dict(f, variant='中性', why_good='人少时拿下一个，把人数扳回来')
                mhits[i] = h2
    # 这回合有“站同一个位置连开”（SHOT-04 same_spot）：TEAM-G1“他找过来”的夸奖不再写“守住了位置 / 守在原来的位置”，
    # 不然一条批站着不动、下一条夸守住位置，并排读着矛盾（8d R9）
    if any(h['rule'] == 'SHOT-04' and h.get('is_user') and (h.get('facts') or {}).get('same_spot') for h in mhits):
        for i, h in enumerate(mhits):
            f = h.get('facts') or {}
            if h['rule'] == 'TEAM-G1' and f.get('variant') == '他找过来' and f.get('why_good_nopos'):
                mhits[i] = dict(h, evidence=f.get('ev_nopos') or h['evidence'], facts=dict(f, why_good=f['why_good_nopos']))
    # TEAM-G1 主句并掉同一次击杀的 SHOT-G4 时，SHOT-G4 的“刚连杀 N 人的 AWP”这件事要带进主句（8d R22：
    # “跟上去把刚杀人的 对手3 打掉”没写出他是刚连杀两人的 AWP）
    for i, h in enumerate(mhits):
        if h['rule'] != 'TEAM-G1' or not is_good(h):
            continue
        g4 = next((x for x in mhits if x['rule'] == 'SHOT-G4' and _same_kill(h, x)
                   and (x.get('facts') or {}).get('enemy_awp') and ((x.get('facts') or {}).get('streak') or 0) >= 2), None)
        v = victim_of(h)
        if g4 is None or not v:
            continue
        n = g4['facts']['streak']
        t = h.get('text') or ''
        t2 = t.replace(f'刚杀人的 {v}', f'刚连杀 {n} 人的 AWP {v}', 1)
        if t2 == t:
            t2 = t.replace(f'把 {v} ', f'把刚连杀 {n} 人的 AWP {v} ', 1)
        if t2 != t:
            mhits[i] = dict(h, text=t2, facts=dict(h.get('facts') or {}, awp_streak=n))
    # 同一次转点（merge_key rot:…）判了“转点路上开打了还静步”（TIME-01）时，不再夸“及时转点”（TIME-G1）：一夸一批并排读着矛盾
    slow_rot = {h.get('merge_key') for h in mhits if h['rule'] == 'TIME-01' and h.get('merge_key')}
    mhits = [h for h in mhits if not (h['rule'] == 'TIME-G1' and h.get('merge_key') in slow_rot)]
    # M3 / M5：好操作
    goods = _m5(_m3(_good_overlaps([h for h in mhits if is_good(h)])))
    # M4
    goods, units = _m4(goods, units)
    allu = units + goods

    # 主要原因；它对应的时刻、TEAM-01 关键一枪标“关键”
    cause, chit = main_cause(rhits + mhits, info)
    for u in allu:
        if chit is not None and any(x is chit for x in _unit_hits(u)):
            u['key'] = True
            if u['hit'] is not chit:
                u['skip'] = [chit]          # 主要原因那句已经写在 main_cause 里，时刻里不再并一遍
    # 合理代价、没有“还可以”的连杀（TEAM-03 冲出来几秒里连杀）主要原因已经整句写了：时刻里不再单独写一遍（8d R16）
    def _cause_says(u):
        h = u['hit']
        f = h.get('facts') or {}
        return (h['rule'] == 'TEAM-03' and not h.get('is_user') and u['mblame'] == '合理代价' and not better_text(h['alt'])
                and not u['clauses'] and isinstance(f.get('killer'), str) and f['killer'] in cause['text'] and '连杀' in cause['text'])
    allu = [u for u in allu if not _cause_says(u)]
    t01 = next((h for h in rhits if h['rule'] == 'TEAM-01'), None)
    if t01 is not None and team01_who(t01) == '你':
        dt = t01['facts'].get('key_t', t01['facts'].get('decisive_t'))
        if isinstance(dt, (int, float)):
            for u in allu:
                if is_good(u['hit']) and abs((u['hit'].get('t') or 0) - dt) <= 1.0:
                    u['key'] = True
                    break

    # 第六轮拍板 2：只写值得分析的点（去掉的命中仍在 hits.json；总结用的是没筛之前的单元，见 compose）
    t01c = next((h for h in rhits if h['rule'] == 'TEAM-01'), None)
    info = dict(info, carried=bool(info.get('won') and t01c is not None and team01_who(t01c) == '你'))
    allu, dropped = _salience(allu, info, cause)

    # 时刻太多时按优先级留 MAX_MOMENTS 个，再按时间排
    def prio(u):
        if u['key']:
            return 0
        if not is_good(u['hit']) and u['contrib'] == 'major':
            return 1
        return 2 if is_good(u['hit']) else 3
    keep = sorted(allu, key=lambda u: (prio(u), hit_key(u['hit'])))[:MAX_MOMENTS]
    keep = sorted(keep, key=lambda u: hit_key(u['hit']))
    _short_tails(keep)
    moments = [to_moment(u, info) for u in keep]
    # 第 32 条：eco 阵亡的“还可以”只留找长枪那一条，写进这次阵亡的时刻（ideas 里就不再重复）
    for u, m in zip(keep, moments):
        hd = u['hit'].get('death_t')
        if hunt and u['death'] and not m['better'] and isinstance(hd, (int, float)):
            hx = next((x for x in hunt if abs(float(x['death_t']) - float(hd)) <= DEATH_SAME_S + 1e-6), None)
            if hx is not None:
                m['better'] = sp(better_text(hx['alt']))

    # 复核修正（第 34 条）：这次阵亡页面上记着你的道具 / 开枪类问题时，不再夸“反 eco 没有一个人往前找人头”（51920 R22、d5a R17）
    if any(u['death'] and any(not is_good(h) and (h['rule'] == 'UTIL-01' or h['rule'] in SHOT_RULES or h['rule'] == DUEL_RULE)
                              and blame_parts(h.get('blame'))[1] != 'none' for h in [u['hit']] + u['clauses']) for u in keep):
        rhits = [h for h in rhits if h['rule'] != 'TEAM-G3']
    yp = your_part(keep, rhits, info, cause)
    # 筛掉的你的问题（合理代价 / 不影响结果的小问题）不写，但也不能因为它没写出来就把你的部分抬成“打得好”
    # （8d R1：0 杀 0 伤害，藏掉五枪全空以后只剩“一起进点回防”，被评成打得好）。你主导赢下的回合照旧
    t01_ = next((h for h in rhits if h['rule'] == 'TEAM-01'), None)
    carried = info.get('won') and t01_ is not None and team01_who(t01_) == '你'
    if yp['level'] == '打得好' and not carried and any(
            not is_good(u['hit']) and (u['death'] or u['hit'].get('is_user')) for u in dropped):
        yp = dict(yp, level='没问题')

    # ideas：从句和回合级 hit 的“还可以”、思路类 hit；和时刻里写过的不重复，最多 3 条
    betters = {m['better'] for m in moments}
    betters |= {seg for m in moments for seg in (m.get('also') or '').split('；') if seg}
    cand = []
    for u in keep:
        for c in u['clauses']:
            if u['hit']['rule'] == 'POS-03' and c['rule'] in SHOT_RULES:
                continue                    # 并进 POS-03 的同一串 SHOT-03：“停半拍再点”和“打一枪就缩回去”方向相反（8d R18）
            if (c.get('is_user') or c.get('death_of') == user) and not is_good(c) and _covered(c, u['hit']) != '':
                cand.append(c)              # 被主句整条盖掉的从句（POST-02 下的 TEAM-04）页面上没写，它的“还可以”也不列
    cand += [h for h in ideas_src if not h['rule'].startswith('ECO-')]      # 买法的另一种思路已经写在经济栏
    # 回合层面判到你的问题（TIME-01 转点慢之类）页面上没写出来时（你的部分不写它），它的“还可以”也不列：
    # 不然 ideas 在改一件页面上没提过的事，旁边你的部分还写着“没问题”（e6b R20）
    cand += [h for h in rhits if not h['rule'].startswith('ECO-') and h['rule'] not in ('TEAM-01', 'TEAM-02')
             and not (h.get('is_user') and not is_good(h) and blame_parts(h.get('blame'))[1] != 'none'
                      and _part(h['text']) not in yp['text'])]
    ideas = []
    # CT 下包前的 TEAM-03 补上的团队建议（“一个人先扔闪、另一个跟着进”）只挡一模一样的句子，
    # 不按“同一类”挡掉你自己那条“进超市前先扔一颗闪”（d5a R12）
    weak = {m['better'] for u, m in zip(keep, moments)
            if u['hit']['rule'] == 'TEAM-03' and (u['hit'].get('facts') or {}).get('alt_ct_pre')}
    fam_src = sorted(b for b in betters if b and b not in weak)
    seen_keys = [_idea_key(b) for b in fam_src]
    seen_fam = set().union(*[_idea_family(b) for b in fam_src]) if fam_src else set()
    # 从句的“还可以”已经整句抄进了时刻：同一句再列一遍（8d R10 第 3 条）
    whats = ' '.join(m['what'] for m in moments)
    for h in sorted(cand, key=hit_key):
        a = _strip_blame(clean_alt(h['alt']))
        if a.startswith('继续') or (h.get('kind') == '好'):
            continue                        # “继续这样……”是夸奖，不是另一种思路
        if not a or sp(_strip_blame(better_text(h['alt']))) in betters or sp(a) in ideas:
            continue
        k = _idea_key(a)
        if any(_similar(k, x) for x in seen_keys):
            continue                        # 和已经写过的一条说的是同一件事（8d R4 两条“往下水道回一颗道具”）
        fam = _idea_family(a)
        if fam & seen_fam:
            continue
        if sp(re.sub(r'^还可以[：:，,\s]*', '', a)) in whats:
            continue
        seen_keys.append(k)
        seen_fam |= fam
        ideas.append(sp(a))
    ideas = ideas[:3]

    # M7：句子检查（只记录，validate 阶段才拦）
    issues = []
    for h in hits:
        if unfilled(h['text']) or unfilled(h['alt']):
            issues.append(f"第 {info.get('round')} 回合 {h['rule']}：句子或“还可以”里还有没填的模板占位符，alt 已丢掉")
    for i, m in enumerate(moments):
        for k in ('what', 'better', 'also', 'evidence'):
            for why in validate.text_issues(m.get(k, ''), info.get('names'), info.get('enemies')):
                issues.append(f"第 {info.get('round')} 回合 moments[{i}].{k}（{m['rule']}）：{why}")
    for k, s in (('main_cause', cause['text']), ('your_part', yp['text'])):
        for why in validate.text_issues(s, info.get('names'), info.get('enemies')):
            issues.append(f"第 {info.get('round')} 回合 {k}：{why}")

    clauses = [[{'rule': c['rule'], 'text': clause_text(c)} for c in u['clauses']] for u in keep]
    used_rules = sorted({h['rule'] for u in keep for h in _unit_hits(u)} | {h['rule'] for h in rhits},
                        key=lambda r: (rule_idx(r), r))
    return {'moments': moments, 'main_cause': cause, 'your_part': yp, 'clauses': clauses, 'ideas': ideas,
            'issues': issues, 'used': used_rules, 'units': keep, 'round_hits': rhits, 'cause_hit': chit,
            'dropped': len(dropped)}
