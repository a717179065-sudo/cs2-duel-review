# -*- coding: utf-8 -*-
"""检查 review.json 里玩家能读到的每一句话（CONTRACT “validate.py 检查项”）。

检查：
  1. P6 用词：坐标、速度数字、角度（° / 数字+度）、百分比、对拉/对架、B 商店、英文报点名；
     教学、视频、应该、必须、上帝视角、你当时不知道、雷达上能看到、残血、只剩 X 血（对方血量）；
     报点、喊、约好、沟通、交流；下一场。（K/D/A、ADR、KAST 只警告）
  2. 人名都在本场玩家名单里（英文词逐个查：玩家名、武器名、少数固定词；名字简称只警告）。
  3. “第 N 秒”在本回合的 hit facts / 时间线事件里有对应（±0.5 秒）。
  4. “$钱数”在 hit facts、economy.json 或固定价目表里。
  5. “NvM”人数在本回合出现过（时间线人数变化 + hit facts 里的 us/them）。
  6. 每个 moment 都有 rule，并且 rule 在 rules_v1 里。
  另加：字段齐全、取值在页面认得的范围里（kind / blame / level / rating / momentum.kind / who）。

用法：
  pipeline：run(ctx) → 返回问题列表（空 = 通过），警告只打印。
  命令行：py -I engine/validate.py <review.json> [--work <work 目录>]   不通过时退出码 1。
  merge.py 的 M7 句子检查调用 text_issues(text)。
"""
import json
import re
import sys
from pathlib import Path

if __package__ in (None, ''):                      # 命令行直接跑：py -I engine/validate.py
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = 'engine'

from . import config  # noqa: E402

RULES_JSON = config.RULES_DIR / 'rules_v1.json'

# ---------------------------------------------------------------- 取值范围（和 match_template.html 一致）
MOMENT_KINDS = ('好', '问题', '关键')
MOMENT_BLAMES = ('—', '你的责任', '你的责任 + 队友或团队', '小问题', '队友或团队', '合理代价')
YOUR_BLAMES = ('你的责任', '你的责任 + 队友或团队', '小问题')     # 页面上算“你的”问题的时刻
LEVELS = ('打得好', '没问题', '小问题', '主要问题')
RATINGS = ('打得好', '正常', '有问题', '合理代价')
CAUSE_WHO = ('你', '队友', '团队', '经济/装备差距')
WEAK_WHO = ('你', '团队')
POINT_KINDS = ('good', 'bad')

# ---------------------------------------------------------------- 1. P6 用词
# (正则, 说明)；顺序就是报告顺序
P6_PATTERNS = [
    (r'坐标', '出现“坐标”'),
    (r'[（(]\s*-?\d{2,5}\s*[,，]\s*-?\d{2,5}(?:\s*[,，]\s*-?\d{2,5})?\s*[)）]', '像坐标的数字'),
    (r'(?<![A-Za-z])[XYZxyz]\s*[=＝:：]\s*-?\d', '像坐标的数字'),
    (r'速度\s*[:：是为约到]?\s*\d', '速度数字'),
    (r'\d+\s*(?:u/s|ups|单位/秒|单位每秒)', '速度数字'),
    (r'°', '角度符号'),
    (r'\d+(?:\.\d+)?\s*度(?![过日假])', '角度（数字+度）'),
    (r'[%％]|百分之', '百分比'),
    (r'对拉|对架', '用了“对拉/对架”'),
    (r'B\s*商店', '用了“B 商店”（应为超市）'),
    (r'教学|视频', '引用教学/视频'),
    (r'应该|必须', '说教用词（应该/必须）'),
    (r'上帝视角|你当时不知道|你看不到血量', '上帝视角说明'),
    (r'雷达', '写了雷达（玩家视角只作内部判断，页面上不解释）'),
    (r'(?<![可应])该(?:等|保|退|扔|买|起|补|收|停|走|拉|架|做|有|要|去|用)', '说教用词（该……）'),
    (r'报(?:了)?点', '报点建议（没有语音数据）'),
    (r'喊', '“喊”（沟通建议）'),
    (r'约好|沟通|交流', '沟通建议'),
    (r'下一场|下一把', '“下一场”'),
    (r'来分责任|按击杀栏数|口径|这一条记在队友或团队|不算在里面', '判法 / 记账说明上了页面'),
    # 数据和判法的说明：打中记录 / 击杀记录 / 一帧 / “在你的视野里或正和你交火”的“或” / 赛后推算（8d、128 R4）
    (r'记录|一帧|或正和|赛后推算', '数据 / 判法说明上了页面'),
    # 正文里记责任的话（页面已经有责任标签）：“输回合是合理代价，丢枪算你的”（8d R10、R12）
    (r'算你的|输回合是合理代价|回合输是合理代价', '正文里在记责任（记账）'),
    # 读不通的固定写法：“AWP 这一枪没中：对……”、“扔的雷你和队友都没看见人”、“停下连开”（8d R8、R7、R14）
    (r'这一枪没中：|都没看见人，往有动静|停下连开', '读不通的句式'),
    (r'不过但|但不过|不过不过|但但', '连接词重复（不过 / 但）'),
    # 规则模板里的标签上了页面（128 R17“好的操作配：”）、凑字的尾巴（128 R6“这回合你没死”）
    (r'好的操作配|不配[:：]', '规则模板的标签上了页面'),
    (r'这回合你没死', '凑字的结尾（“这回合你没死”）'),
    (r'[{}｛｝]', '还有没填的模板占位符'),
    # 第六轮拍板 1：弱守不写拖了几秒（拖够了 / 没拖够），他倒下后到下包的时间也不算他的
    (r'拖(?:了|住了?|住)\s*约?\s*\d+(?:\.\d+)?\s*秒|包点失守一共\s*\d', '写了拖了几秒（只写拖够了 / 没拖够）'),
    # 第八轮（手册 v1 审核单）：机械能查的几条
    (r'静步走了约?\s*\d', '写了静步走了几秒（审核单第 24 条）'),
    (r'等火烧起来再出', '“先扔一颗火，等火烧起来再出”（火不是开路道具，审核单第 26 条）'),
    (r'扔闪拖时间|闪拖时间', '“扔闪拖时间”（闪拖不了时间，审核单第 26 条）'),
    (r'补.{0,4}拱门.{0,4}烟|过点烟', '拱门烟 / 过点烟的建议（审核单第 31 条、Q17）'),
    (r'省下的钱.{0,8}发枪', '“省下的钱给队友发枪”（审核单第 20、21 条）'),
    (r'远处改成两三发|两三发一组点射', '“远处改成两三发一组点射”（审核单第 12 条）'),
    (r'\d+\s*(?:点)?血(?!量)|\d+\s*(?:点)?伤害|伤害\s*\d', '写了伤害 / 血量数字'),
    (r'(?<![第\d.])(?<!第 )\d+(?:\.\d+)?\s*秒多?(?:后|才|就被|就打|没交火|没和人)|等了\s*\d+(?:\.\d+)?\s*秒',
     '写了延迟秒数（只写第 N 秒这样的时间点）'),
    # 复核修正：秒和“就”中间夹了人名 / 代词（“开枪后 0.3 秒 高分玩家12 就打中了你”）、汉字写的延迟（“不到一秒就被他打中”）
    (r'(?<![第\d.])(?<!第 )\d+(?:\.\d+)?\s*秒[^，。；第]{0,15}?就|不到[一半两]秒|(?<![第\d])[一半两三]秒多?(?:后|才|就)',
     '写了延迟秒数（只写第 N 秒这样的时间点）'),
    (r'(?<![A-Za-z])(?:SHOT|POS|TEAM|UTIL|TIME|POST|ECO|COST|SUM)-G?\d{1,2}(?!\d)', '句子里出现了规则编号'),
    (r'(?<![A-Za-z])(?:GEN|CT|T)-[A-Z]{1,4}-\d{1,3}', '句子里出现了知识库编号'),
]
P6_RE = [(re.compile(p), why) for p, why in P6_PATTERNS]
# 只警告：计分板上已有的数字（“ADR 全场最高”这种特别的事实可以写，所以不算错）
WARN_RE = [(re.compile(r'(?<![A-Za-z])(?:K/D(?:/A)?|KDA|ADR|KAST)(?![A-Za-z])'), '提到计分板数字（K/D/A、ADR、KAST），确认不是重复')]

# 英文报点名（整词匹配；Jungle 按 C-PLACE 保留英文，不在这里）
EN_PLACES = sorted({k for k, v in config.PLACE_CN.items() if v != k} | {
    'Mid', 'TopMid', 'Window', 'Apps', 'Ramp', 'Tetris', 'Firebox', 'Ticket', 'Sandwich', 'Kitchen', 'Arches',
    'Balcony', 'Chair', 'Bench', 'Triple', 'Ninja', 'Default', 'Short', 'Cat', 'Con', 'Underpass', 'Tunnels'})

# 不是人名的英文词（≥3 个字母）：武器、装备、少数固定说法
EN_WORDS = {
    # 武器显示名（economy.json / common.weapon_cn 里会出现的）
    'AK-47', 'M4A4', 'M4A1-S', 'M4A1', 'AWP', 'MP7', 'MP9', 'MP5', 'MP5-SD', 'P90', 'UMP', 'UMP-45', 'USP', 'USP-S',
    'P2000', 'P250', 'Glock', 'Glock-18', 'Tec-9', 'CZ75', 'CZ75-Auto', 'Five-SeveN', 'Five-Seven', 'FN57', 'Desert',
    'Eagle', 'Deagle', 'Dual', 'Berettas', 'Galil', 'FAMAS', 'AUG', 'SG553', 'SSG', 'XM1014', 'MAG-7', 'Nova',
    'Sawed-Off', 'Negev', 'M249', 'MAC-10', 'SCAR-20', 'G3SG1', 'SSG-08', 'MP5SD', 'R8', 'CZ75a', 'PP-Bizon', 'Bizon', 'Revolver', 'Zeus', 'Kevlar', 'Helmet', 'Smoke',
    'Grenade', 'Flashbang', 'Molotov', 'Incendiary', 'Decoy', 'Defuse', 'Kit', 'High', 'Explosive', 'Knife',
    # 固定说法
    'eco', 'VIP', 'Jungle', 'demo', 'MVP',
}
TOKEN_RE = re.compile(r'[A-Za-z][A-Za-z0-9_\-]*')

# 价目和赏金（economy.json 里不一定有的固定钱数）
PRICES = {100, 150, 200, 250, 300, 350, 400, 500, 600, 650, 700, 750, 900, 1000, 1050, 1100, 1200, 1250, 1300,
          1400, 1500, 1700, 1750, 1800, 1900, 2000, 2050, 2100, 2250, 2400, 2500, 2700, 2900, 3000, 3100, 3250,
          3300, 3400, 3500, 3700, 3900, 4400, 4750, 5000, 5750, 16000}

SEC_RE = re.compile(r'第\s*([−\-]?\d+(?:\.\d+)?)\s*秒')
MONEY_RE = re.compile(r'\$\s?(\d[\d,]*)')
COUNTERFACTUAL = r'没让|不让|免得|如果|要是|否则|不会|就会|本可|本来能'
NVM_RE = re.compile(r'(?<![A-Za-z0-9])([0-5])\s?v\s?([0-5])(?![0-9])')


# 对方血量：“残血”“只剩/还剩 N 血”只在说对方时算问题（说你自己、说队友是正常描述）。
# 按从句（，。；）看：同一从句里有对方名字或“他/对方/对面”、并且没有“你”时才报。
# P1 例外：同一句（到句号为止）里写了你打中/打掉/炸到过他，这个血量是你自己打出来的，可以写。
HP_RE = re.compile(r'残血|(?:只剩|还剩|剩下?)\s*\d+\s*(?:点)?血')
CLAUSE_RE = re.compile(r'[^，。；,;！？]+')
SENTENCE_RE = re.compile(r'[^。；！？]+')
ENEMY_WORDS = ('他', '对方', '对面')
USER_DMG_RE = re.compile(r'打掉|打中|炸到|烧到|炸掉|烧掉|打残|伤害')


def _enemy_hp(text, enemies):
    """写了对方剩多少血 / 对方残血的从句（不算你自己打出来的血量）"""
    for sent in SENTENCE_RE.findall(text):
        if USER_DMG_RE.search(sent):
            continue
        for c in CLAUSE_RE.findall(sent):
            if not HP_RE.search(c) or '你' in c:
                continue
            if any(e in c for e in enemies or ()) or any(w in c for w in ENEMY_WORDS):
                return c
    return None


def text_issues(text, names=None, enemies=None):
    """一句话的 P6 用词检查（M7 也用）：返回 [原因]。
    names = 本场玩家名（排除名字里碰巧带的英文词）；enemies = 对方玩家名（判断血量说的是不是对方）"""
    if not isinstance(text, str) or not text:
        return []
    out = []
    for rx, why in P6_RE:
        if rx.search(text):
            out.append(why)
    if _enemy_hp(text, enemies):
        out.append('写了对方的血量（残血 / 只剩 X 血）')
    s = text
    for n in sorted(names or (), key=len, reverse=True):
        s = s.replace(n, ' ')
    for p in EN_PLACES:
        if re.search(r'(?<![A-Za-z0-9_])' + re.escape(p) + r'(?![A-Za-z0-9_])', s):
            out.append(f'英文报点名 {p}')
    return out


def name_issues(text, names):
    """英文词逐个查：玩家名 / 武器 / 固定词以外的都报。返回 ([错误], [警告])"""
    errs, warns = [], []
    if not isinstance(text, str):
        return errs, warns
    lower = {n.lower(): n for n in names}
    words = {w.lower() for w in EN_WORDS} | {p.lower() for p in EN_PLACES}
    rest = text
    for n in sorted(names, key=len, reverse=True):     # 先把完整的玩家名去掉（名字里可能有 - _ 这类字符）
        rest = rest.replace(n, ' ')
    for tok in TOKEN_RE.findall(rest):
        tok = tok.strip('-')
        if len(tok) < 3 or tok in names:
            continue
        if tok.lower() in words:
            continue
        if tok.lower() in lower:                    # 大小写不同
            warns.append(f'人名大小写和名单不同：{tok}')
            continue
        pre = [n for n in names if len(tok) >= 4 and n.lower().startswith(tok.lower())]
        if len(pre) == 1:
            warns.append(f'人名用了简称：{tok}（{pre[0]}）')
        else:
            errs.append(f'不认识的英文词：{tok}（不是本场玩家名，也不是武器名）')
    return errs, warns


# ---------------------------------------------------------------- 已知事实（第 N 秒 / 钱 / 人数）
def _numbers(obj, out):
    """递归收集所有数字"""
    if isinstance(obj, bool):
        return out
    if isinstance(obj, (int, float)):
        out.add(float(obj))
    elif isinstance(obj, dict):
        for k in sorted(obj, key=str):
            _numbers(obj[k], out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _numbers(v, out)
    elif isinstance(obj, str):
        for m in re.findall(r'-?\d+(?:\.\d+)?', obj):
            out.add(float(m))
    return out


def _pairs(obj, out):
    """递归收集 facts 里的人数对（us/them、n_us/n_them、我方/对方，以及 '3v2' 这样的字符串）"""
    if isinstance(obj, str):
        for m in NVM_RE.finditer(obj):
            out.add((int(m.group(1)), int(m.group(2))))
        return out
    if isinstance(obj, dict):
        for a, b in (('us', 'them'), ('n_us', 'n_them'), ('alive_us', 'alive_them'), ('我方', '对方')):
            if isinstance(obj.get(a), int) and isinstance(obj.get(b), int):
                out.add((obj[a], obj[b]))
        for k in sorted(obj, key=str):
            _pairs(obj[k], out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _pairs(v, out)
    return out


class Facts:
    """validate 用的已知事实：按回合的秒数、人数，全场的钱数、玩家名"""

    def __init__(self, timeline=None, economy=None, hits=None, rule_ids=None):
        self.names = set()
        self.enemies = set()
        self.secs = {}           # 回合 → {秒}
        self.pairs = {}          # 回合 → {(us, them)}
        self.money = set(PRICES)
        self.rule_ids = set(rule_ids or ())
        self.has_hits = hits is not None
        self.timeline = timeline
        self.hit_blames = {}     # (回合, 规则) → [(秒, hit.blame)]：时刻的 blame 要和 hit 判的一致
        self.long_spray = set()  # 有“20 米外一口气扫到底”（SHOT facts.long_spray）的回合：只有这些回合能写“一直按着没松开”
        if timeline:
            m = timeline.get('meta', {})
            self.names = {m.get('user')} | set(m.get('teammates') or []) | set(m.get('enemies') or [])
            self.names.discard(None)
            self.enemies = set(m.get('enemies') or [])
            for r in timeline.get('rounds', []):
                n = r['round']
                s = self.secs.setdefault(n, set())
                p = self.pairs.setdefault(n, {(5, 5)})
                for k in r.get('kills', []):
                    s.add(float(k['t']))
                for a in r.get('alive', []):
                    s.add(float(a['t']))
                    p.add((a['us'], a['them']))
                for key in ('user_util', 'team_util', 'enemy_util'):
                    for u in r.get(key, []):
                        for f in ('t', 'det_t', 'end_t'):
                            if isinstance(u.get(f), (int, float)):
                                s.add(float(u[f]))
                for key in ('user_damage_dealt', 'user_damage_taken', 'user_fire', 'user_fights'):
                    for u in r.get(key, []):
                        for f in ('t', 'start_t'):
                            if isinstance(u.get(f), (int, float)):
                                s.add(float(u[f]))
                for key in ('plant', 'defuse', 'user_death'):
                    if r.get(key) and isinstance(r[key].get('t'), (int, float)):
                        s.add(float(r[key]['t']))
                d = r.get('user_death') or {}
                if isinstance(d.get('alive_before'), dict):
                    p.add((d['alive_before'].get('us'), d['alive_before'].get('them')))
        if economy:
            _numbers(economy, self.money)
        for h in hits or []:
            n = h.get('round')
            s = self.secs.setdefault(n, set())
            for v in (h.get('t'), h.get('death_t')):
                if isinstance(v, (int, float)):
                    s.add(float(v))
            _numbers(h.get('facts') or {}, s)
            _numbers(h.get('facts') or {}, self.money)
            _pairs(h.get('facts') or {}, self.pairs.setdefault(n, {(5, 5)}))
            if isinstance(h.get('t'), (int, float)) and h.get('rule'):
                self.hit_blames.setdefault((n, h['rule']), []).append((float(h['t']), (h.get('blame') or '').strip()))
            if (h.get('facts') or {}).get('long_spray') or ((h.get('facts') or {}).get('duel') or {}).get('key') == 'long_spray':
                self.long_spray.add(n)

    def all_pairs(self):
        out = set()
        for n in sorted(self.pairs, key=lambda x: (x is None, x or 0)):
            out |= self.pairs[n]
        return out


# ---------------------------------------------------------------- 逐字段检查
def _strings(review):
    """(回合号或 None, 字段路径, 字符串)，按 review 的顺序"""
    out = []
    S = review.get('summary', {})
    out.append((None, 'summary.headline', S.get('headline')))
    mo = S.get('momentum') or {}
    out.append((None, 'summary.momentum.text', mo.get('text')))
    for i, p in enumerate(mo.get('points') or []):
        for k in ('title', 'text'):
            out.append((None, f'summary.momentum.points[{i}].{k}', p.get(k)))
    for sec in ('weaknesses', 'strengths'):
        for i, w in enumerate(S.get(sec) or []):
            for k in ('title', 'text'):
                out.append((None, f'summary.{sec}[{i}].{k}', w.get(k)))
    for r in review.get('rounds', []):
        n = r.get('round')
        for k in ('headline', 'situation', 'team'):
            out.append((n, k, r.get(k)))
        for k in ('idea', 'swing'):
            out.append((n, f'economy.{k}', (r.get('economy') or {}).get(k)))
        out.append((n, 'main_cause.text', (r.get('main_cause') or {}).get('text')))
        out.append((n, 'your_part.text', (r.get('your_part') or {}).get('text')))
        for i, s in enumerate(r.get('story') or []):
            out.append((n, f'story[{i}].text', s.get('text')))
        for i, m in enumerate(r.get('moments') or []):
            for k in ('what', 'better', 'also', 'evidence'):
                if k == 'also' and 'also' not in m:
                    continue                    # also 只在好操作时刻带改进建议时才有
                out.append((n, f'moments[{i}].{k}', m.get(k)))
        for i, t in enumerate(r.get('tags') or []):
            out.append((n, f'tags[{i}]', t))
        for i, t in enumerate(r.get('ideas') or []):
            out.append((n, f'ideas[{i}]', t))
    return out


def _schema(review, F):
    """字段和取值范围 → [(回合, 字段, 原因)]"""
    bad = []
    S = review.get('summary')
    if not isinstance(S, dict):
        return [(None, 'summary', '缺 summary')]
    for k in ('headline', 'momentum', 'weaknesses', 'strengths'):
        if k not in S:
            bad.append((None, f'summary.{k}', '缺字段'))
    for i, p in enumerate((S.get('momentum') or {}).get('points') or []):
        if p.get('kind') not in POINT_KINDS:
            bad.append((None, f'summary.momentum.points[{i}].kind', f'取值 {p.get("kind")!r} 不在 {POINT_KINDS}'))
        if not isinstance(p.get('round'), int):
            bad.append((None, f'summary.momentum.points[{i}].round', '回合号不是整数'))
    for i, w in enumerate(S.get('weaknesses') or []):
        if w.get('who') not in WEAK_WHO:
            bad.append((None, f'summary.weaknesses[{i}].who', f'取值 {w.get("who")!r} 不在 {WEAK_WHO}'))
        if not isinstance(w.get('rounds'), list):
            bad.append((None, f'summary.weaknesses[{i}].rounds', '不是列表'))
    for i, w in enumerate(S.get('strengths') or []):
        if not isinstance(w.get('rounds'), list):
            bad.append((None, f'summary.strengths[{i}].rounds', '不是列表'))
    keys = ('round', 'side', 'headline', 'situation', 'economy', 'main_cause', 'your_part', 'story', 'moments',
            'team', 'rating', 'tags', 'ideas')
    for r in review.get('rounds', []):
        n = r.get('round')
        for k in keys:
            if k not in r:
                bad.append((n, k, '缺字段'))
        if r.get('rating') not in RATINGS:
            bad.append((n, 'rating', f'取值 {r.get("rating")!r} 不在 {RATINGS}'))
        if (r.get('your_part') or {}).get('level') not in LEVELS:
            bad.append((n, 'your_part.level', f'取值 {(r.get("your_part") or {}).get("level")!r} 不在 {LEVELS}'))
        if (r.get('main_cause') or {}).get('who') not in CAUSE_WHO:
            bad.append((n, 'main_cause.who', f'取值 {(r.get("main_cause") or {}).get("who")!r} 不在 {CAUSE_WHO}'))
        for i, s in enumerate(r.get('story') or []):
            if not isinstance(s.get('t'), (int, float)):
                bad.append((n, f'story[{i}].t', '秒数不是数字'))
        for i, m in enumerate(r.get('moments') or []):
            if m.get('kind') not in MOMENT_KINDS:
                bad.append((n, f'moments[{i}].kind', f'取值 {m.get("kind")!r} 不在 {MOMENT_KINDS}'))
            if m.get('blame') not in MOMENT_BLAMES:
                bad.append((n, f'moments[{i}].blame', f'取值 {m.get("blame")!r} 不在 {MOMENT_BLAMES}'))
            elif m.get('blame') == '你的责任' and isinstance(m.get('t'), (int, float)):
                # hit 判的是“你的责任 + 队友或团队”/“小问题”，时刻上只写“你的责任”就丢了一半（8d R5 TEAM-06）
                hb = [b for t, b in F.hit_blames.get((n, m.get('rule')), []) if abs(t - m['t']) <= 0.06]
                if hb and all(b in ('你的责任 + 队友或团队', '小问题') for b in hb):
                    bad.append((n, f'moments[{i}].blame', f'hit 判的是 {hb[0]!r}，时刻只写了“你的责任”'))
            if not isinstance(m.get('t'), (int, float)):
                bad.append((n, f'moments[{i}].t', '秒数不是数字'))
            # 6. rule
            rule = m.get('rule')
            if not rule:
                bad.append((n, f'moments[{i}].rule', '没有 rule'))
            elif F.rule_ids and rule not in F.rule_ids:
                bad.append((n, f'moments[{i}].rule', f'{rule} 不在 rules_v1 里'))
    return bad


# 好操作时刻的“为什么好”只写理由：不带建议、不带指令
WHY_GOOD_BAD = re.compile(r'还可以|继续这样|继续和|^继续|应该|必须')
# 标签 / 标题里“没停稳”和“停稳拿下”不能并排（不带枪名分开写时）
STILL_BAD_TAGS = ('第一枪没停稳就开', '没停住就被先打中')
STILL_GOOD_TAGS = ('停稳先手拿下', '停住修准星拿下', '被先打中后站住反杀', '停稳开镜先手', 'AWP 停稳开镜先手', '先挨一枪后停住开镜反杀',
                   '狙空一枪后第二枪带走')


def _round_logic(review):
    """一回合里互相矛盾的结论 → [(回合, 字段, 原因, 原文)]"""
    bad = []
    for r in review.get('rounds', []):
        n = r.get('round')
        ms = r.get('moments') or []
        yp, mc = r.get('your_part') or {}, r.get('main_cause') or {}
        mine_bad = [m for m in ms if m.get('blame') in YOUR_BLAMES]
        if mine_bad and (yp.get('level') == '打得好' or r.get('rating') == '打得好'):
            bad.append((n, 'your_part.level', '你的部分 / 评级是“打得好”，但这回合有判到你的问题时刻（你的责任 / 你的责任 + 队友或团队 / 小问题）', mine_bad[0].get('what', '')))
        if yp.get('text') and yp.get('text') == mc.get('text'):
            bad.append((n, 'your_part.text', '你的部分和主要原因是同一句', yp.get('text')))
        for i, m in enumerate(ms):
            if m.get('kind') == '好' and m.get('better') and WHY_GOOD_BAD.search(m['better']):
                bad.append((n, f'moments[{i}].better', '“为什么好”里有建议或指令（还可以 / 继续这样 / 应该）', m['better']))
            if m.get('also') and re.match(r'继续', m['also']):
                bad.append((n, f'moments[{i}].also', '“还可以”里是“继续这样”的指令', m['also']))
        tg = r.get('tags') or []
        b = [t for t in tg if t in STILL_BAD_TAGS]
        g = [t for t in tg if t in STILL_GOOD_TAGS]
        if b and g:
            bad.append((n, 'tags', f'标签“{b[0]}”和“{g[0]}”并排（同一类枪只留问题，不同类枪要带枪名）', '、'.join(tg)))
        head = r.get('headline') or ''
        if b and any(head.endswith('你' + x) or ('你' + x + '；') in head for x in STILL_GOOD_TAGS):
            bad.append((n, 'headline', f'标题夸“停稳”，标签却是“{b[0]}”', head))
    return bad


_HALF_MIN = re.compile(r'第\s*(\d+(?:\.\d+)?)\s*秒[^；。]*最近半分钟')
_ALONE_1V = re.compile(r'(?<![0-9])1v[1-5][^；。]*一个人出去接枪')
_FINAL = re.compile(r'最终\s*(\d+):(\d+)(\s*(?:赢下|输掉|打平))?')


def _norm(s):
    return re.sub(r'\s+', '', re.sub(r'^还可以[：:，,\s]*', '', s or ''))


def _consistency(review, F):
    """同一页上能机械查出来的前后矛盾 → (错误, 警告)"""
    errs, warns = [], []
    tl = {r['round']: r for r in ((getattr(F, 'timeline', None) or {}).get('rounds') or [])}
    user = ((getattr(F, 'timeline', None) or {}).get('meta') or {}).get('user')
    for r in review.get('rounds', []):
        n = r.get('round')
        ms = r.get('moments') or []
        for i, m in enumerate(ms):
            w, ev = m.get('what') or '', m.get('evidence') or ''
            # “最近半分钟”只能在回合开打 30 秒以后写（e6b R6 22 秒、128 R15 16 秒）
            for x in _HALF_MIN.finditer(w):
                if float(x.group(1)) < 30:
                    errs.append((n, f'moments[{i}].what', f'第 {x.group(1)} 秒写了“最近半分钟”，回合还没过 30 秒', w))
            # 你是最后一个人（1vN）时不写“一个人出去接枪”（8d R10）
            if _ALONE_1V.search(w):
                errs.append((n, f'moments[{i}].what', '1vN 时写了“一个人出去接枪”', w))
            # 手册 UTIL-A：同一时刻既说“可以等 / 不用去接 / 先停下”，又说“先扔一颗……再出”，两个方向相反
            wb = w + '；' + (m.get('better') or '')
            if re.search(r'可以等|不用去接|先停下', wb) and re.search(r'先扔一颗[^；。]{0,12}再出', wb):
                errs.append((n, f'moments[{i}]', '同一时刻里既说“可以等 / 先停下”，又说“先扔一颗……再出”', wb))
            # “和对面打”和“一直没和人交过火”同一句（e6b R16）
            if '和对面打' in w and '一直没和人交过火' in w:
                errs.append((n, f'moments[{i}].what', '同一时刻里既写“和对面打”又写“一直没和人交过火”', w))
            # “停稳的那枪”（单数）旁边证据说“大多数 / 每一枪都是停稳开的”（e6b R8、8d R13）
            if '停稳的那枪' in w and ('大多数枪是停稳开的' in ev or '每一枪都是停稳开的' in ev):
                errs.append((n, f'moments[{i}].what', '“停稳的那枪”和证据里“大多数枪是停稳开的”说法不一', w))
            # “还可以”只多给一个思路：一条里用“；”塞了好几条只警告
            if m.get('blame') != '—' and (m.get('better') or '').count('；') >= 1:
                warns.append((n, f'moments[{i}].better', '“还可以”里写了不止一个思路', m.get('better')))
        # ideas 和时刻里的“还可以”重复（8d R12）
        bs = {_norm(m.get('better')) for m in ms if m.get('better')} | {_norm(m.get('also')) for m in ms if m.get('also')}
        for j, idea in enumerate(r.get('ideas') or []):
            if _norm(idea) in bs:
                errs.append((n, f'ideas[{j}]', 'ideas 和时刻里的“还可以”是同一句', idea))
        # 标题“队友先倒下”，但这回合我方第一个倒下的是你（e6b R19）
        R = tl.get(n)
        head = r.get('headline') or ''
        if R and user and '队友先倒下' in head:
            ours = [k for k in (R.get('kills') or []) if not k.get('post_round') and k.get('victim_side') == R.get('side')]
            ours = sorted(ours, key=lambda k: (k.get('tick') or 0, k.get('t') or 0))
            if ours and ours[0].get('victim') == user:
                errs.append((n, 'headline', '标题写“队友先倒下”，但这回合我方第一个倒下的是你', head))
        yp = (r.get('your_part') or {}).get('text') or ''
        if len(yp) > 220:
            warns.append((n, 'your_part.text', f'你的部分太长（{len(yp)} 字）', yp))
        errs += _page_checks(r, n)
    # 总结标题：“最终 a:b”后面要写赢下 / 输掉；赢了的比赛不写“经济崩盘”（128）
    sh = (review.get('summary') or {}).get('headline') or ''
    for x in _FINAL.finditer(sh):
        a, b = int(x.group(1)), int(x.group(2))
        if not x.group(3):
            errs.append((None, 'summary.headline', '“最终 a:b”后面没写赢下 / 输掉', sh))
        if a > b and '崩盘' in sh:
            errs.append((None, 'summary.headline', '比赛赢了，标题却写“经济崩盘”', sh))
    return errs, warns


_YP_LEAD = re.compile(r'^(?:另外|不过)?\s*第\s*(\d+(?:\.\d+)?)\s*秒')
_WAIT_THEM = re.compile(r'(?<![0-9])([0-5])v([0-5])[^；。]*可以等他们')
YP_MAX_SEGS = 3


def _page_checks(r, n):
    """一回合页面上能机械查出来的问题（第五轮审查：e6b / 128 / 8d）→ [(回合, 字段, 原因, 原文)]"""
    out = []
    ms = r.get('moments') or []
    yp = (r.get('your_part') or {}).get('text') or ''
    head = r.get('headline') or ''
    tags = r.get('tags') or []
    # 标签 / 标题里的公式写法（“eco 打全起输＝装备差距”）
    for t in tags:
        if '＝' in t or '=' in t:
            out.append((n, 'tags', '标签里有“＝”（公式写法）', t))
    # 标题 / 标签说“身后没人”，时刻里却写“比附近的队友先露头”（e6b R7/R9/R12、128 R1）
    if any('比附近的队友先露头' in (m.get('what') or '') for m in ms):
        for where, x in [('headline', head)] + [('tags', t) for t in tags]:
            if '身后没人' in x:
                out.append((n, where, '写“身后没人”，时刻里却说附近有队友（比附近的队友先露头）', x))
    # “还可以”页面自己加：时刻的还可以 / also 里再写“还可以”就成了“还可以：……还可以……”（e6b R17）
    for i, m in enumerate(ms):
        for k in ('better', 'also'):
            v = m.get(k) or ''
            if (k == 'also' or m.get('blame') != '—') and '还可以' in v:
                out.append((n, f'moments[{i}].{k}', '“还可以”里又写了一次“还可以”', v))
        w = m.get('what') or ''
        for x in _WAIT_THEM.finditer(w):
            if x.group(2) == '1':
                out.append((n, f'moments[{i}].what', '对面只剩一个人，写了“等他们”', w))
    # 你的部分：最多三句；按时间写（开头的“第 N 秒”不能往回跳）；同一句不写两遍（只差秒数）
    segs = [x for x in yp.split('；') if x]
    if len(segs) > YP_MAX_SEGS:
        out.append((n, 'your_part.text', f'你的部分超过 {YP_MAX_SEGS} 句（{len(segs)} 句）', yp))
    last = None
    # 功劳是你的回合降成小问题：先写打得好的那一面，再“不过……”写问题（COMPOSE_NOTES 等级表；8d R9），
    # 这一处“不过”回头写更早的问题不算往回跳，只放过一次
    good_first = ((r.get('main_cause') or {}).get('who') == '你' and (r.get('your_part') or {}).get('level') == '小问题')
    for x in segs:
        mm = _YP_LEAD.match(x)
        if not mm:
            continue
        v = float(mm.group(1))
        if good_first and x.startswith('不过'):
            good_first = False
            last = v
            continue
        if last is not None and v < last - 1e-6 and re.match(r'^(?:另外|不过)', x):
            # “另外 / 不过”开头的一定是另一件事的开头（句子内部回头说“第 N 秒时你们还在一起”不算）
            out.append((n, 'your_part.text', f'你的部分时间往回跳（第 {last:g} 秒之后写第 {mm.group(1)} 秒）', yp))
            break
        last = v
    bodies = [re.sub(r'^(?:另外|不过)?\s*第\s*\d+(?:\.\d+)?\s*秒', '', x) for x in segs]
    if len([b for b in bodies if len(b) >= 12]) != len({b for b in bodies if len(b) >= 12}):
        out.append((n, 'your_part.text', '你的部分同一句写了两遍（只差秒数）', yp))
    return out


_SUM_ONLY_HIT = re.compile(r'这\s*(\d+)\s*枪只中了\s*(\d+)\s*次')


def _summary_checks(review, names):
    """总结：没有具体对手的句子里不写“他”；“只中了”不用在打中一半以上的时候（128 SUM-01 6 枪中 5）"""
    out = []
    S = review.get('summary') or {}
    for k in ('weaknesses', 'strengths'):
        for i, w in enumerate(S.get(k) or []):
            t = w.get('text') or ''
            if '他' in t and not any(nm and nm in t for nm in names or ()):
                out.append((None, f'summary.{k}[{i}]', '总结里用了“他”，但没写是谁', t))
            for x in _SUM_ONLY_HIT.finditer(t):
                a, b = int(x.group(1)), int(x.group(2))
                if a and b * 2 >= a:
                    out.append((None, f'summary.{k}[{i}]', f'{a} 枪中了 {b} 次还写“只中了”', t))
    return out


YOUR_BLAMES = ('你的责任', '你的责任 + 队友或团队', '小问题')


def _page_rules(review, F):
    """页面层面的几条硬规则（第 3 场 / bd 复查意见）：
    - 你这回合死了（时间线 user_death），你的部分不能写“这回合你这边没有要改的”（bd R1）；
    - 你的部分是“小问题 / 主要问题”、页面上有判到你的时刻时，至少写出其中一个时刻的那一秒（8d R12：等级是 9.9 秒的 SHOT-04
      定的，你的部分只写了另一次合理代价的阵亡）；
    - 同一回合夸“拿到人后挪开了位置”（POS-G2），而它之前几秒有“狙空 / 杀完没收”的时刻（bd R22，矛盾并排）；
    - 标题写“一个人逐个打掉”，主要原因却是“冲出来几秒里连杀”（8d R16）；
    - 加时的回合，局面里要写“加时”（bd：12:12 进加时，页面上一个“加时”都没有）；
    - 总结：缺点“一串打空不调整”的回合全在“停稳开的第一枪命中低”里（同一件事写两条）；
      强项写“会挪开开枪的位置”，缺点里又有“杀完人没收”（bd 第 19、22 回合）"""
    out = []
    tl = {r['round']: r for r in ((F.timeline or {}).get('rounds') or [])}
    reg = 2 * int(config.HALF_ROUNDS)
    for r in review.get('rounds', []):
        n = r.get('round')
        yp = r.get('your_part') or {}
        t = tl.get(n) or {}
        ud = t.get('user_death') if isinstance(t.get('user_death'), dict) else None
        if ud and not ud.get('post_round') and yp.get('text') == '这回合你这边没有要改的':
            out.append((n, 'your_part', '你这回合死了，你的部分却写“这回合你这边没有要改的”', yp.get('text')))
        ms = r.get('moments') or []
        mine = [m for m in ms if m.get('blame') in YOUR_BLAMES]
        if yp.get('level') in ('小问题', '主要问题') and mine:
            txt = yp.get('text') or ''
            secs = set(SEC_RE.findall(txt))
            def _cut(w):
                return re.sub(r'^第\s*[−\-]?\d+(?:\.\d+)?\s*秒(?:起)?[，,]?\s*', '', w or '')[:6]
            if not any(str(m.get('t')) in secs or (_cut(m.get('what')) and _cut(m.get('what')) in txt) for m in mine):
                out.append((n, 'your_part', '等级是判到你的时刻定的，你的部分一个都没写', txt))
        for g in ms:
            if g.get('rule') != 'POS-G2':
                continue
            for b in ms:
                if b.get('rule') in ('POS-01', 'SHOT-04') and isinstance(b.get('t'), (int, float)) and isinstance(g.get('t'), (int, float)) \
                        and 0 <= g['t'] - b['t'] <= 10 and any(x in (b.get('what') or '') for x in ('没收', '没有往回收', '还站在原地')):
                    out.append((n, f"moments[t={g.get('t')}]", '夸“挪开了开枪的位置”，前几秒又写了没收、还在枪线上', g.get('what')))
        if '逐个打掉' in (r.get('headline') or '') and '冲出来几秒里连杀' in ((r.get('main_cause') or {}).get('text') or ''):
            out.append((n, 'headline', '标题写“逐个打掉”，主要原因是冲出来几秒里连杀', r.get('headline')))
        if isinstance(n, int) and n > reg and '加时' not in (r.get('situation') or ''):
            out.append((n, 'situation', '加时的回合没写“加时”', r.get('situation')))
        # 第六轮拍板 6：你主导赢下的回合，标题和你的部分先写好的一面，问题用“不过……”接在后面
        if t.get('result') == '赢' and (r.get('main_cause') or {}).get('who') == '你':
            out += _carried_checks(r, n)
    S = review.get('summary') or {}
    ws = S.get('weaknesses') or []
    s02 = next((w for w in ws if w.get('title') == '步枪停稳开的第一枪命中低'), None)
    s03 = next((w for w in ws if w.get('title') == '一串打空不调整'), None)
    if s02 and s03 and '一串打空没调整' in (s02.get('text') or '') and set(s03.get('rounds') or []) <= set(s02.get('rounds') or []):
        out.append((None, 'summary.weaknesses', '“一串打空不调整”的回合全在“停稳开的第一枪命中低”里，同一件事写了两条', s03.get('text')))
    if any(w.get('title') == '杀完人没收' for w in ws) and any('挪开开枪的位置' in (x.get('text') or '') for x in S.get('strengths') or []):
        out.append((None, 'summary.strengths', '强项写“会挪开开枪的位置”，缺点里又有“杀完人没收”', ''))
    return out


def _problem_phrases():
    """标题里“你 + 问题短语”用到的问题短语（compose.PHRASE 里不是好操作的那些 + 几个分支短语）"""
    from . import compose as CP              # 用到时才 import（compose → merge → validate）
    ph = {v for k, v in CP.PHRASE.items() if '-G' not in k}
    ph |= {'狙空后没收', '打人时站的位置两个人都打得到', '扔道具时侧着身被先打中', '听到枪声还抢在队友前面冲',
           '比附近的队友先露头、没人看得到对手', '对枪时被另一个人打死', '远处手枪打空', '停下重新对准后还是没中',
           '没等准星到身上就开枪', '进点后面的人没跟上', '队友进点你没跟上', '挪位置时走进了对面的枪线', '人多时跑出去补人',
           '去拉刚响过枪的角度', '比 VIP 烟先上中远'}
    ph |= set(CP.DUEL_PHRASE.values())
    return sorted(ph, key=len, reverse=True)


def _carried_checks(r, n):
    """你主导赢下的回合：标题第一段不能是问题；问题前面要有“不过”；你的部分不能先写问题"""
    out = []
    head = re.sub(r'^(?:我方赛点|对面赛点|双方赛点)，', '', r.get('headline') or '')
    head = re.sub(r'^[^：；，]{1,8}：', '', head)
    segs = [x for x in re.split(r'[；，]', head) if x]
    phs = _problem_phrases()
    for i, x in enumerate(segs):
        body = re.sub(r'^(?:不过)?你\s*', '', x)
        if any(body.startswith(p) for p in phs):
            if i == 0:
                out.append((n, 'headline', '你主导赢下的回合，标题先写了问题', r.get('headline')))
            elif not x.startswith('不过'):
                out.append((n, 'headline', '你主导赢下的回合，标题里的问题前面没有“不过”', r.get('headline')))
            break
    yp = (r.get('your_part') or {}).get('text') or ''
    first = yp.split('；')[0]
    for m in r.get('moments') or []:
        if m.get('blame') in YOUR_BLAMES and m.get('what') and first and first[:14] == m['what'][:14]:
            out.append((n, 'your_part', '你主导赢下的回合，你的部分先写了问题', yp))
            break
    return out


def _story_jumps(review):
    """时间线里相邻两条人数相差不止一个人（漏写了中间的击杀）→ 警告"""
    out = []
    for r in review.get('rounds', []):
        prev = None
        for s in r.get('story') or []:
            ms = list(NVM_RE.finditer(s.get('text') or ''))
            if not ms:
                continue
            cur = (int(ms[-1].group(1)), int(ms[-1].group(2)))
            if prev is not None and abs(cur[0] - prev[0]) + abs(cur[1] - prev[1]) > 1:
                out.append((r.get('round'), 'story', f'人数从 {prev[0]}v{prev[1]} 跳到 {cur[0]}v{cur[1]}，中间漏了击杀', s.get('text')))
            prev = cur
    return out


def check_review(review, F):
    """检查整份 review → (错误 [(回合, 字段, 原因, 原文)], 警告 [同上])"""
    errs, warns = [], []
    for n, path, why in _schema(review, F):
        errs.append((n, path, why, ''))
    errs += _round_logic(review)
    errs += _summary_checks(review, F.names)
    errs += _page_rules(review, F)
    e2, w2 = _consistency(review, F)
    errs += e2
    warns += w2
    warns += _story_jumps(review)
    names = F.names
    for n, path, s in _strings(review):
        if s is None or s == '':
            continue
        if not isinstance(s, str):
            errs.append((n, path, '不是字符串', repr(s)))
            continue
        for why in text_issues(s, names, F.enemies):
            errs.append((n, path, why, s))
        # 审核单第 12 条：“一直按着没松开”只给 20 米外一口气扫到底、几乎没中的那一串（规则 facts.long_spray）
        if F.has_hits and '一直按着没松开' in s and n not in F.long_spray:
            errs.append((n, path, '写了“一直按着没松开”，这回合没有一口气扫到底的那一串', s))
        for rx, why in WARN_RE:
            if rx.search(s):
                warns.append((n, path, why, s))
        if names:
            e, w = name_issues(s, names)
            errs += [(n, path, x, s) for x in e]
            warns += [(n, path, x, s) for x in w]
        # 3. 第 N 秒（只查回合里的字段；没有时间线就不查）
        if n is not None and F.secs.get(n):
            for m in SEC_RE.finditer(s):
                v = float(m.group(1).replace('−', '-'))
                if not any(abs(v - x) <= 0.5 for x in F.secs[n]):
                    errs.append((n, path, f'“第 {m.group(1)} 秒”在本回合的事实里找不到（±0.5 秒）', s))
        # 4. 钱
        if len(F.money) > len(PRICES):
            for m in MONEY_RE.finditer(s):
                v = float(m.group(1).replace(',', ''))
                if v not in F.money:
                    errs.append((n, path, f'钱数 ${m.group(1)} 在 facts / economy 里找不到', s))
        # 5. 人数
        pairs = F.pairs.get(n) if n is not None else F.all_pairs()
        if pairs and len(pairs) > 1:
            for m in NVM_RE.finditer(s):
                pr = (int(m.group(1)), int(m.group(2)))
                if pr not in pairs:
                    why = f'人数 {pr[0]}v{pr[1]} 在{"本回合" if n is not None else "全场"}没出现过'
                    # 假设句（“没让 4v5 拖成 3v5”“免得变成…”）里的人数本来就没发生过：只警告
                    clause = next((c for c in CLAUSE_RE.findall(s) if m.group(0) in c), '')
                    if re.search(COUNTERFACTUAL, clause):
                        warns.append((n, path, why + '（假设句）', s))
                    else:
                        errs.append((n, path, why, s))
    return errs, warns


def fmt_issue(x):
    n, path, why, s = x
    where = f'第 {n} 回合 {path}' if n is not None else path
    return f'{where}：{why}' + (f'｜原文：{s}' if s else '')


# ---------------------------------------------------------------- 读文件
def _load(p):
    p = Path(p)
    if not p.exists():
        return None
    with open(p, encoding='utf-8') as f:
        return json.load(f)


def rule_ids():
    """rules_v1 的规则编号 + config.EXTRA_RULES（审核单新增的，rules_v1.json 只读）"""
    d = _load(RULES_JSON) or {}
    return sorted({r['id'] for r in d.get('rules', [])} | {r['id'] for r in config.EXTRA_RULES})


def load_facts(work, hits_path=None):
    work = Path(work)
    hits = _load(hits_path or work / 'hits.json')
    if isinstance(hits, dict):
        hits = hits.get('hits')
    return Facts(_load(work / 'timeline.json'), _load(work / 'economy.json'), hits, rule_ids())


def report(errs, warns, out=print):
    """打印给人看的报告"""
    if warns:
        # 同一原因合并成一行：原因、次数、第一处位置
        groups = {}
        for x in warns:
            groups.setdefault(x[2], []).append(x)
        out(f'警告 {len(warns)} 处（不算不通过），{len(groups)} 类：')
        for why in sorted(groups, key=lambda k: (-len(groups[k]), k)):
            g = groups[why]
            first = f'第 {g[0][0]} 回合 {g[0][1]}' if g[0][0] is not None else g[0][1]
            out(f'  {why}：{len(g)} 处，例如 {first}')
    if errs:
        out(f'没通过：{len(errs)} 处')
        for x in errs:
            out('  ' + fmt_issue(x))
    else:
        out('检查文字：全部通过')


TEMPLATE = Path(__file__).resolve().parent / 'page_template.html'


def template_issues():
    """页面模板里不能再有用户拒掉的“复制 CS2 命令”按钮"""
    try:
        t = TEMPLATE.read_text(encoding='utf-8')
    except OSError:
        return []
    out = []
    if 'data-copy' in t or '复制' in t:
        out.append((None, 'page_template.html', '页面模板里还有“复制”按钮（用户已经拒掉这个功能）', ''))
    return out


def run(ctx):
    """pipeline 阶段：检查 work/<id>/review.json；返回问题列表（空 = 通过）"""
    review = _load(Path(ctx.work) / 'review.json')
    if review is None:
        raise FileNotFoundError(str(Path(ctx.work) / 'review.json'))
    F = load_facts(ctx.work)
    errs, warns = check_review(review, F)
    errs += template_issues()
    report(errs, warns)
    return [fmt_issue(x) for x in errs]


def main(argv):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if not argv:
        print('用法：py -I engine/validate.py <review.json> [--work <work 目录>] [--hits <hits.json>]')
        return 2
    rp = Path(argv[0])
    work = rp.parent
    hits = None
    if '--work' in argv:
        work = Path(argv[argv.index('--work') + 1])
    if '--hits' in argv:
        hits = Path(argv[argv.index('--hits') + 1])
    review = _load(rp)
    if review is None:
        print(f'找不到 {rp}')
        return 2
    F = load_facts(work, hits)
    errs, warns = check_review(review, F)
    errs += template_issues()
    report(errs, warns)
    return 1 if errs else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
