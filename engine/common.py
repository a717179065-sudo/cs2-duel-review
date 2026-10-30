# -*- coding: utf-8 -*-
"""C-* 口径的唯一实现（rules_v1 common：C-TIME、C-PLACE、C-ALIVE、C-STILL、C-HIT、C-TARGET、C-SEEN、C-RADAR、
C-SMOKE、C-HEAR、C-FIGHT、C-ECO、C-GUN），以及填句子用的小工具（秒数、报点、人数、武器中文名、钱数、知识库）。

用法：规则模块 `from engine import common as C`，所有函数第一个参数是 ctx（见 CONTRACT 的 Ctx），
数据只通过 ctx.cache(name) / ctx.json(name) 读，阈值只从 ctx.th['common'] 取（thresholds/common.json）。
原型（pro/analysis/match3/rules_proto/*.py）里各自重复的 idx/eye/sees/alive/速度/烟 等实现合并到这里，
和原型口径不同的地方写在 COMMON_API.md。

数据来源：
  帧：ctx.cache('ticks')（每 tick 一帧：X/Y/Z、pitch/yaw、duck、存活、血量、阵营、武器、开镜、报点、approximate_spotted_by
      + weapon_fire/player_hurt/player_death/player_blind 事件）；没有时依次退到 sight_s4 / sight_s8 / sight / pp_ticks / daoju。
  道具事件（烟的起爆/消散）：上面几个缓存里第一个带 smokegrenade_detonate 的。
  回合：ctx.json('timeline.json') 的 key_ticks（freeze_start / round_start=冻结结束 / round_end）。
  经济：ctx.json('economy.json')。
判断只用 3D 位置 + spotted；平面距离只给"站位远近"这类不涉及可见性的地方用。
"""
import json
import math
import re
from pathlib import Path

import numpy as np
import pandas as pd

try:                                    # 报点中文名、根目录以 config 为准（engine/config.py）
    from engine.config import PLACE_CN as _CFG_PLACE_CN, ROOT as _ROOT
except ImportError:                     # 单独测试时 config 不在包里
    try:
        from config import PLACE_CN as _CFG_PLACE_CN, ROOT as _ROOT
    except ImportError:
        _CFG_PLACE_CN, _ROOT = None, Path(__file__).resolve().parent.parent

TR = 64                                  # tickrate；ctx.th['common']['tickrate'] 应与此一致
KB_PATH = Path(_ROOT) / 'engine' / 'knowledge' / 'mirage_kb.json'   # 可选的报点知识库，不随仓库发布；没有时 kb_items() 返回 {}

FRAME_CACHES = ('ticks', 'sight_s4', 'sight_s8', 'sight', 'pp_ticks', 'daoju')    # 帧数据，按精度排
EVENT_CACHES = ('ticks', 'sight_s4', 'sight_s8', 'sight', 'pp_ticks', 'daoju')    # 事件，逐个找第一个有的

# ================================================================ C-PLACE 报点
# config.PLACE_CN 不在时用这份（内容与 config 相同：rules_v1 C-PLACE + PalaceInterior/SideAlley/Market/TSpawn）
_PLACE_CN_LOCAL = {
    'TopofMid': '中远', 'Connector': '拱门', 'SnipersNest': 'VIP', 'Apartments': 'B 二楼', 'Palace': 'A 二楼',
    'PalaceInterior': 'A 二楼', 'TRamp': 'A1', 'Jungle': 'Jungle', 'Shop': '超市', 'Market': '超市',
    'Underpass': '下水道', 'Truck': '小推车', 'CTSpawn': 'CT 出生点', 'Stairs': 'A 楼梯', 'BackAlley': 'B 后巷',
    'Middle': '中路', 'Scaffolding': '跳台', 'Catwalk': 'B 小', 'Ladder': '梯子间', 'BombsiteA': 'A 点',
    'BombsiteB': 'B 点', 'SideAlley': 'B 小巷', 'TSpawn': 'T 出生点', 'PalaceAlley': 'A 二楼外巷',
    'House': 'T 方房区',
}
PLACE_CN = dict(_CFG_PLACE_CN) if _CFG_PLACE_CN else dict(_PLACE_CN_LOCAL)


def place_cn(place, default='某处'):
    """C-PLACE：last_place_name → 中文报点。空值或没收录的名字返回 default（句子里绝不出现英文报点名）。"""
    if place is None or (isinstance(place, float) and place != place):
        return default
    p = str(place)
    if p in PLACE_CN:
        return PLACE_CN[p]
    if p in PLACE_CN.values():          # 已经是中文
        return p
    return default


# ================================================================ C-GUN 武器
_PISTOL = ('glock', 'hkp2000', 'usp_silencer', 'p250', 'fiveseven', 'tec9', 'cz75a', 'deagle', 'elite', 'revolver')
_SMG = ('mac10', 'mp9', 'mp7', 'mp5sd', 'ump45', 'p90', 'bizon')
_SHOTGUN = ('nova', 'xm1014', 'mag7', 'sawedoff')
_MG = ('m249', 'negev')
_RIFLE = ('ak47', 'm4a1', 'm4a1_silencer', 'galilar', 'famas', 'aug', 'sg556')
_SNIPER = ('awp', 'ssg08', 'g3sg1', 'scar20')
_NONGUN_PREFIX = ('knife', 'bayonet', 'hegrenade', 'flashbang', 'smokegrenade', 'molotov', 'incgrenade', 'inferno',
                  'decoy', 'c4', 'planted_c4', 'taser', 'world', 'healthshot', 'melee', 'fists')
# 显示名（active_weapon_name / item）→ 武器键（weapon_fire 的 weapon 去掉 weapon_）
_DISPLAY2KEY = {
    'AK-47': 'ak47', 'M4A4': 'm4a1', 'M4A1-S': 'm4a1_silencer', 'Galil AR': 'galilar', 'FAMAS': 'famas', 'AUG': 'aug',
    'SG 553': 'sg556', 'AWP': 'awp', 'SSG 08': 'ssg08', 'G3SG1': 'g3sg1', 'SCAR-20': 'scar20', 'MAC-10': 'mac10',
    'MP9': 'mp9', 'MP7': 'mp7', 'MP5-SD': 'mp5sd', 'UMP-45': 'ump45', 'P90': 'p90', 'PP-Bizon': 'bizon',
    'Glock-18': 'glock', 'P2000': 'hkp2000', 'USP-S': 'usp_silencer', 'P250': 'p250', 'Five-SeveN': 'fiveseven',
    'Tec-9': 'tec9', 'CZ75-Auto': 'cz75a', 'Desert Eagle': 'deagle', 'Dual Berettas': 'elite',
    'R8 Revolver': 'revolver', 'Nova': 'nova', 'XM1014': 'xm1014', 'MAG-7': 'mag7', 'Sawed-Off': 'sawedoff',
    'M249': 'm249', 'Negev': 'negev', 'Zeus x27': 'taser', 'High Explosive Grenade': 'hegrenade',
    'Flashbang': 'flashbang', 'Smoke Grenade': 'smokegrenade', 'Molotov': 'molotov',
    'Incendiary Grenade': 'incgrenade', 'Decoy Grenade': 'decoy', 'C4 Explosive': 'c4',
}
_WEAPON_CN = {
    'ak47': 'AK', 'm4a1': 'M4A4', 'm4a1_silencer': 'M4A1-S', 'galilar': 'Galil', 'famas': '法玛斯', 'aug': 'AUG',
    'sg556': 'SG553', 'awp': 'AWP', 'ssg08': '鸟狙', 'g3sg1': '连狙', 'scar20': '连狙',
    'mac10': '吹风机', 'mp9': 'MP9', 'mp7': 'MP7', 'mp5sd': 'MP5', 'ump45': 'UMP', 'p90': 'P90', 'bizon': '野牛',
    'glock': '格洛克', 'hkp2000': 'P2000', 'usp_silencer': 'USP', 'p250': 'P250', 'fiveseven': 'FN57',
    'tec9': 'Tec-9', 'cz75a': 'CZ75', 'deagle': '沙鹰', 'elite': '双枪', 'revolver': '左轮',
    'nova': '霰弹', 'xm1014': '霰弹', 'mag7': '霰弹', 'sawedoff': '霰弹', 'm249': '机枪', 'negev': '机枪',
    'taser': '电击枪', 'hegrenade': '雷', 'flashbang': '闪', 'smokegrenade': '烟', 'molotov': '火',
    'incgrenade': '火', 'inferno': '火', 'decoy': '诱饵弹', 'c4': 'C4', 'planted_c4': 'C4', 'knife': '刀',
}


# player_hurt 里不分版本的武器名 → 可能的真名（按开枪人最近一次 weapon_fire 认）；look 秒内的开枪才算
HURT_WK_FAMILY = {'m4a1': ('m4a1_silencer',), 'hkp2000': ('usp_silencer',)}
HURT_WK_LOOK_S = 3.0


def wkey(w):
    """武器名统一成键：'weapon_ak47' / 'ak47' / 'AK-47' → 'ak47'；刀一律 'knife'；空值 → None。"""
    if w is None or (isinstance(w, float) and w != w):
        return None
    s = str(w)
    if s in _DISPLAY2KEY:
        return _DISPLAY2KEY[s]
    s = s.lower()
    if s.startswith('weapon_'):
        s = s[7:]
    if s.endswith('_off'):              # m4a1_silencer_off / usp_silencer_off：拆了消音器还是同一把枪
        s = s[:-4]
    if any(x in s for x in ('knife', 'bayonet', 'karambit', 'dagger')):   # 各种刀皮肤的显示名也算刀
        return 'knife'
    return s


def is_gun(w):
    """是枪（不含刀、道具、C4、电击枪）"""
    k = wkey(w)
    return bool(k) and not any(k.startswith(p) for p in _NONGUN_PREFIX)


def gun_tier(w):
    """C-GUN 枪档：手枪 0 < 冲锋枪、霰弹（含机枪）1 < 步枪 2 < AWP/鸟狙/连狙 3；不是枪 -1。"""
    k = wkey(w)
    if k in _PISTOL:
        return 0
    if k in _SMG or k in _SHOTGUN or k in _MG:
        return 1
    if k in _RIFLE:
        return 2
    if k in _SNIPER:
        return 3
    return -1


def weapon_class(w):
    """武器大类：'手枪' / '冲锋枪' / '霰弹' / '机枪' / '步枪' / '狙'；不是枪 None。"""
    k = wkey(w)
    for grp, name in ((_PISTOL, '手枪'), (_SMG, '冲锋枪'), (_SHOTGUN, '霰弹'), (_MG, '机枪'), (_RIFLE, '步枪'),
                      (_SNIPER, '狙')):
        if k in grp:
            return name
    return None


def is_sniper(w):
    return wkey(w) in _SNIPER


def weapon_cn(w, default='枪'):
    """句子里的武器名：AK、M4A4、M4A1-S、AWP、沙鹰、USP、格洛克、吹风机（MAC-10）、鸟狙、连狙、霰弹……"""
    k = wkey(w)
    return _WEAPON_CN.get(k, default) if k else default


# ================================================================ 句子小工具
_NUM_CN = {1: '一', 2: '两', 3: '三', 4: '四', 5: '五', 6: '六', 7: '七', 8: '八', 9: '九', 10: '十'}


def _r1(t):
    """一位小数，四舍五入（不用 round 的半数取偶）"""
    return math.floor(float(t) * 10 + 0.5 + 1e-9) / 10 + 0.0


def fmt_sec(t):
    """C-TIME 句子里的秒数：保留一位小数（四舍五入），小数是 0 时取整。44.0 → '44'，39.25 → '39.3'，-3.0 → '-3'。"""
    if t is None:
        return ''
    v = _r1(t)
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f'{v:.1f}'


def sec(t):
    """facts 里存的秒数（一位小数的 float），和 fmt_sec 同一个取整"""
    return None if t is None else _r1(t)


def alive_str(us, them):
    """人数写法：'3v2'（我方在前）"""
    return f'{int(us)}v{int(them)}'


def num_cn(n):
    """1..10 → 一、两、三……；其他原样"""
    return _NUM_CN.get(int(n), str(n))


def money(v):
    """钱数写法：'$2500'"""
    return f'${int(round(v))}'


# ---------------------------------------------------------------- 排版 / 纯值（各规则模块原来各写一份，合并到这里）
_CJK_RE = '\u4e00-\u9fff'
_CJK_PUNCT = _CJK_RE + '，。；：、（）！？'
_NAME_MARK = '\x01'                     # 保护人名用的占位符（控制字符，句子里不会出现）
_TIDY_DEL = re.compile(f'(?<=[{_CJK_PUNCT}]) +(?=[{_CJK_PUNCT}])')
_TIDY_A = re.compile(rf'(?<=[{_CJK_RE}])(?=[A-Za-z0-9_$\x01])')
_TIDY_B = re.compile(rf'(?<=[A-Za-z0-9_\-\x01])(?=[{_CJK_RE}])')


def tidy(s, names=()):
    """句子排版（rules_v1 例句的写法）：中文和英文/数字/$ 之间补一个空格（“在A 二楼” → “在 A 二楼”），
    两个中文（含中文标点）之间的空格去掉（“你 开局” → “你开局”），连续空格合并，首尾空格去掉。
    names：玩家名整体当一个英文词，名字内部不加也不删空格（名字以 - 结尾、带中文都没问题）。"""
    if not s:
        return s
    keep = []
    for n in sorted({n for n in names if n}, key=lambda n: (-len(n), n)):
        if n in s:
            s = s.replace(n, f'{_NAME_MARK}{len(keep)}{_NAME_MARK}')
            keep.append(n)
    s = _TIDY_DEL.sub('', s)
    s = _TIDY_B.sub(' ', _TIDY_A.sub(' ', s))
    s = re.sub(' {2,}', ' ', s).strip()
    for i, n in enumerate(keep):
        s = s.replace(f'{_NAME_MARK}{i}{_NAME_MARK}', n)
    return s


def plain(o, sort_keys=False):
    """facts 里的 numpy / pandas 标量、tuple、set 换成 json 能写的 Python 值；NaN → None；dict 键转字符串。
    sort_keys=True 时 dict 按键排序（两次运行字节一样）"""
    if isinstance(o, dict):
        ks = sorted(o, key=str) if sort_keys else list(o)
        return {str(k): plain(o[k], sort_keys) for k in ks}
    if isinstance(o, (list, tuple)):
        return [plain(v, sort_keys) for v in o]
    if isinstance(o, (set, frozenset)):
        return [plain(v, sort_keys) for v in sorted(o, key=str)]
    if o is None or isinstance(o, (bool, str)):
        return o
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, (int, np.integer)):
        return int(o)
    if isinstance(o, (float, np.floating)):
        return None if math.isnan(float(o)) else float(o)
    if hasattr(o, 'item'):
        return plain(o.item(), sort_keys)
    if hasattr(o, 'tolist'):
        return plain(o.tolist(), sort_keys)
    return str(o)


def side_name(side):
    """阵营统一成 'T' / 'CT'（接受 2/3、'T'/'CT'、'TERRORIST'/'CT'）"""
    if side in (2, '2', 'T', 't', 'TERRORIST'):
        return 'T'
    if side in (3, '3', 'CT', 'ct'):
        return 'CT'
    return None


def other_side(side):
    s = side_name(side)
    return {'T': 'CT', 'CT': 'T'}.get(s)


def team_num(side):
    """'T' → 2，'CT' → 3"""
    return {'T': 2, 'CT': 3}.get(side_name(side))


# ================================================================ 知识库（只作背景，不引用原文）
_KB = {}


def kb_items(path=None):
    """mirage_kb.json：{id: item}（item 含 side/area/phase/when/tip/why）。只用来配"还可以"的思路，句子里不引用原文。"""
    p = str(path or KB_PATH)
    if p not in _KB:
        try:
            items = json.loads(Path(p).read_text(encoding='utf-8'))['items']
        except (OSError, ValueError, KeyError):
            items = []
        _KB[p] = {it['id']: it for it in sorted(items, key=lambda x: x['id'])}
    return _KB[p]


def kb_item(kb_id, path=None):
    """按 id 取一条知识库条目，没有返回 None"""
    return kb_items(path).get(kb_id)


def kb_find(side=None, area=None, path=None):
    """按阵营 / 区域（中文，如 '中路'、'A 点'）筛条目，返回按 id 排好的 [item]（M6：阵营或地点对不上就不挂）"""
    out = []
    for k in sorted(kb_items(path)):
        it = kb_items(path)[k]
        if side and it.get('side') not in (side, '通用', 'both', None):
            continue
        if area and area not in str(it.get('area', '')):
            continue
        out.append(it)
    return out


# ================================================================ 读数据（每个 ctx 只建一次）
def _th(ctx):
    return ctx.th['common']


def _json(ctx, name):
    """ctx.json 兼容 'timeline.json' 和 'timeline' 两种写法"""
    for n in (name if name.endswith('.json') else name + '.json', name):
        try:
            return ctx.json(n)
        except (FileNotFoundError, KeyError, OSError):
            continue
    raise FileNotFoundError(f'缺少 {name}')


def _try_cache(ctx, name):
    try:
        return ctx.cache(name)
    except (FileNotFoundError, KeyError, OSError):
        return None


def _ev_table(raw, key):
    """从一个缓存里取事件表：顶层（ticks 缓存：fire/hurt/death/blind）或 raw['ev']（sight 缓存）"""
    alias = {'weapon_fire': 'fire', 'player_hurt': 'hurt', 'player_death': 'death', 'player_blind': 'blind',
             'round_freeze_end': 'freeze_end', 'begin_new_match': 'begin'}
    for src in (raw, raw.get('ev') if isinstance(raw, dict) else None):
        if not isinstance(src, dict):
            continue
        for k in (key, alias.get(key)):
            v = src.get(k) if k else None
            if isinstance(v, pd.DataFrame) and len(v):
                return v
    return None


def _sid_str(s):
    """steamid 统一成字符串（int / float / str 都可能）"""
    if s is None or (isinstance(s, float) and s != s):
        return 'nan'
    if isinstance(s, float):
        return str(int(s))
    return str(s)


class _Data:
    """一场 demo 的帧（稠密矩阵：帧 × 玩家）、事件、回合，所有 C-* 函数共用"""

    def __init__(self, ctx):
        th = _th(ctx)
        self.th = th
        self.tr = int(th.get('tickrate', TR))
        self.memo = {}
        # ---- 回合（timeline.json）
        tl = _json(ctx, 'timeline')
        self.tl = tl
        self.rounds = []
        for r in sorted(tl['rounds'], key=lambda x: x['round']):
            k = r['key_ticks']
            self.rounds.append(dict(n=int(r['round']), freeze_start=int(k['freeze_start']), start=int(k['round_start']),
                                    end=int(k['round_end']), side=r.get('side')))
        self.rmap = {r['n']: r for r in self.rounds}
        self._fs = np.array([r['freeze_start'] for r in self.rounds], dtype=np.int64)
        t_first = int(self._fs[0]) if len(self._fs) else 0
        # ---- 帧
        raw, self.frame_src = None, None
        for name in FRAME_CACHES:
            raw = _try_cache(ctx, name)
            if raw is not None:
                self.frame_src = name
                break
        if raw is None:
            raise FileNotFoundError('缓存里没有帧数据（ticks / sight_s4 / sight_s8 ...）')
        self.raw = raw
        T = raw['ticks']
        T = T[T['team_num'].isin([2, 3])]
        sid_col = T['steamid'].map(_sid_str)
        self.sids = sorted(sid_col.unique())
        self.col = {s: i for i, s in enumerate(self.sids)}
        self.bit = {s: (1 << i) for i, s in enumerate(self.sids)}
        ticks = np.unique(T['tick'].to_numpy(np.int64))
        self.ticks = ticks
        ri = np.searchsorted(ticks, T['tick'].to_numpy(np.int64))
        ci = sid_col.map(self.col).to_numpy(np.int64)
        n, m = len(ticks), len(self.sids)

        def dense(series, fill, dtype):
            a = np.full((n, m), fill, dtype=dtype)
            a[ri, ci] = series.to_numpy(dtype=dtype, na_value=fill) if hasattr(series, 'to_numpy') else series
            return a

        self.X = dense(T['X'], np.nan, np.float64)
        self.Y = dense(T['Y'], np.nan, np.float64)
        self.Z = dense(T['Z'], np.nan, np.float64)
        self.yaw = dense(T['yaw'], np.nan, np.float64)
        self.pitch = dense(T['pitch'], np.nan, np.float64)
        self.duck = dense(T['duck_amount'], 0.0, np.float64) if 'duck_amount' in T else np.zeros((n, m))
        self.falive = dense(T['is_alive'].fillna(False).astype(bool), False, bool)
        self.hp = dense(T['health'].fillna(0), 0, np.float64) if 'health' in T else np.zeros((n, m))
        self.team = dense(T['team_num'].fillna(0).astype(np.int64), 0, np.int64)
        self.scoped = dense(T['is_scoped'].fillna(False).astype(bool), False, bool) if 'is_scoped' in T else np.zeros((n, m), bool)
        # 报点、武器：存编码（-1 = 空）
        codes, self.place_names = pd.factorize(T['last_place_name'], use_na_sentinel=True)
        self.place = dense(pd.Series(codes), -1, np.int32)
        codes, self.wpn_names = pd.factorize(T['active_weapon_name'], use_na_sentinel=True)
        self.wpn = dense(pd.Series(codes), -1, np.int32)
        # spotted：每帧每人一个位掩码（第 k 位 = self.sids[k] 看见了他）
        spcol = 'approximate_spotted_by' if 'approximate_spotted_by' in T else 'spotted_by'
        bit = {s: b for s, b in self.bit.items()}
        vals = T[spcol].to_numpy(object)

        def to_mask(v):
            if v is None or (isinstance(v, float) and v != v):
                return 0
            if isinstance(v, str):
                v = [x for x in v.split('|') if x]
            mm = 0
            for x in v:
                mm |= bit.get(_sid_str(x), 0)
            return mm
        mask = np.fromiter((to_mask(v) for v in vals), dtype=np.int64, count=len(vals))
        self.spot = np.zeros((n, m), dtype=np.int64)
        self.spot[ri, ci] = mask
        # 全服务器这一帧有没有任何 spotted 记录（活人）
        self.any_spot = ((self.spot != 0) & self.falive).any(axis=1)
        # 名字
        nm = pd.DataFrame({'sid': sid_col, 'name': T['name'].astype(str), 'tick': T['tick']}).sort_values(['sid', 'tick'])
        self.names = {s: g['name'].iloc[-1] for s, g in nm.groupby('sid', sort=True)}
        self.by_name = {v: k for k, v in sorted(self.names.items())}
        # ---- 事件
        self.fire = self._norm_fire(self._event(ctx, 'weapon_fire'), t_first)
        self.hurt = self._fix_hurt_wk(self._norm_hurt(self._event(ctx, 'player_hurt'), t_first))
        self.death = self._norm_death(self._event(ctx, 'player_death'), t_first)
        self.blind = self._norm_simple(self._event(ctx, 'player_blind'), t_first)
        # ---- 每回合名单（冻结结束那一帧的阵营）和阵亡时刻
        self.roster = {}
        for r in self.rounds:
            i = self.fi(r['start'] + self.tr)
            tm = {s: int(self.team[i, self.col[s]]) for s in self.sids if int(self.team[i, self.col[s]]) in (2, 3)}
            self.roster[r['n']] = tm
        self.death_tick = {}
        for row in self.death.itertuples():
            key = (int(row.round), row.vic)
            if row.round > 0 and key not in self.death_tick:
                self.death_tick[key] = int(row.tick)

    # ------------------------------------------------------------ 事件读取 / 规范化
    def _event(self, ctx, key):
        v = _ev_table(self.raw, key)
        if v is not None:
            return v
        for name in EVENT_CACHES:
            if name == self.frame_src:
                continue
            raw = _try_cache(ctx, name)
            if raw is None:
                continue
            v = _ev_table(raw, key)
            if v is not None:
                return v
        return pd.DataFrame(columns=['tick'])

    def round_idx(self, ticks):
        """tick（数组）→ 回合号（0 = 第一回合冻结前）"""
        i = np.searchsorted(self._fs, np.asarray(ticks, dtype=np.int64), side='right') - 1
        nums = np.array([r['n'] for r in self.rounds] + [0], dtype=np.int64)
        return np.where(i >= 0, nums[np.clip(i, 0, len(self.rounds) - 1)], 0)

    def _base(self, df, t_first):
        df = df.copy()
        if not len(df):
            df['round'] = pd.Series(dtype=np.int64)
            return df
        df['tick'] = df['tick'].astype(np.int64)
        df = df[df['tick'] >= t_first]
        df = df.sort_values('tick', kind='mergesort').reset_index(drop=True)
        df['round'] = self.round_idx(df['tick'].to_numpy())
        return df

    def _norm_simple(self, df, t_first):
        df = self._base(df, t_first)
        for c in ('user_steamid', 'attacker_steamid'):
            if c in df:
                df[c] = df[c].map(_sid_str)
        return df

    def _norm_fire(self, df, t_first):
        df = self._norm_simple(df, t_first)
        df['sid'] = df['user_steamid'] if 'user_steamid' in df else pd.Series(dtype=str)
        df['wk'] = df['weapon'].map(wkey) if 'weapon' in df else None
        df['gun'] = df['wk'].map(lambda k: is_gun(k)).astype(bool) if len(df) else pd.Series(dtype=bool)
        return df

    def _norm_hurt(self, df, t_first):
        df = self._norm_simple(df, t_first)
        df['att'] = df.get('attacker_steamid', pd.Series(['nan'] * len(df)))
        df['vic'] = df.get('user_steamid', pd.Series(['nan'] * len(df)))
        df['wk'] = df['weapon'].map(wkey) if 'weapon' in df else None
        df['dmg'] = pd.to_numeric(df.get('dmg_health', 0), errors='coerce').fillna(0)
        return df

    def _fix_hurt_wk(self, h):
        """player_hurt 的 weapon 不分消音版（M4A1-S 记成 m4a1、USP 记成 hkp2000）：按开枪人这一下之前最近一次
        weapon_fire 的武器认回真名（128 R22：高分玩家17 拿 M4A1-S，句子写成 M4A4）"""
        if not len(h) or 'wk' not in h or not len(self.fire):
            return h
        amb = h['wk'].isin(sorted(HURT_WK_FAMILY))
        if not bool(amb.any()):
            return h
        by = {s_: (g['tick'].to_numpy(), g['wk'].to_numpy()) for s_, g in self.fire.groupby('sid', sort=True)}
        lim = int(HURT_WK_LOOK_S * self.tr)
        new = h['wk'].to_numpy(dtype=object).copy()
        for i in np.flatnonzero(amb.to_numpy()):
            g = by.get(h['att'].iat[i])
            if g is None:
                continue
            tk = int(h['tick'].iat[i])
            j = int(np.searchsorted(g[0], tk, side='right')) - 1
            if j >= 0 and tk - int(g[0][j]) <= lim and g[1][j] in HURT_WK_FAMILY[new[i]]:
                new[i] = g[1][j]
        h = h.copy()
        h['wk'] = new
        return h

    def _norm_death(self, df, t_first):
        df = self._norm_simple(df, t_first)
        df['att'] = df.get('attacker_steamid', pd.Series(['nan'] * len(df)))
        df['vic'] = df.get('user_steamid', pd.Series(['nan'] * len(df)))
        df['wk'] = df['weapon'].map(wkey) if 'weapon' in df else None
        return df

    # ------------------------------------------------------------ 帧下标
    def fi(self, tick):
        """tick 之前（含）最近一帧的下标"""
        i = int(np.searchsorted(self.ticks, int(tick), side='right')) - 1
        return max(0, min(i, len(self.ticks) - 1))

    def frange(self, t0, t1):
        """[t0, t1] 内的帧下标区间 [i0, i1)"""
        return int(np.searchsorted(self.ticks, int(t0), side='left')), int(np.searchsorted(self.ticks, int(t1), side='right'))


def _data(ctx):
    d = getattr(ctx, '_common_data', None)
    if d is None:
        d = _Data(ctx)
        try:
            setattr(ctx, '_common_data', d)
        except AttributeError:
            pass
    return d


def user_sid(ctx):
    """用户 steamid（ctx.user 可以是 steamid 也可以是名字）"""
    d = _data(ctx)
    u = str(ctx.user)
    return u if u in d.col else d.by_name.get(u, u)


# ================================================================ C-TIME 时刻 / 回合
def rounds(ctx):
    """[{n, freeze_start, start(=冻结结束), end, side(用户阵营)}]，按回合号排"""
    return list(_data(ctx).rounds)


def round_info(ctx, rnd):
    return _data(ctx).rmap.get(int(rnd))


def round_of(ctx, tick):
    """tick 属于第几回合（从这回合冻结开始到下回合冻结开始）；第一回合之前返回 None"""
    n = int(_data(ctx).round_idx([tick])[0])
    return n or None


def t_of(ctx, tick, rnd=None):
    """C-TIME：从冻结结束算的秒数（买枪阶段为负数），不取整；句子里用 fmt_sec，facts 里用 sec"""
    d = _data(ctx)
    rnd = rnd or round_of(ctx, tick)
    if rnd is None:
        return None
    return (int(tick) - d.rmap[int(rnd)]['start']) / d.tr


def tick_of(ctx, rnd, t):
    """回合内第 t 秒对应的 tick"""
    d = _data(ctx)
    return int(round(d.rmap[int(rnd)]['start'] + float(t) * d.tr))


# ================================================================ 玩家 / 阵营
def players(ctx):
    """本场所有玩家 steamid（排好序）"""
    return list(_data(ctx).sids)


def name_of(ctx, sid):
    return _data(ctx).names.get(_sid_str(sid), str(sid))


def player_names(ctx):
    """本场所有玩家名（排好序；tidy 保护人名用）"""
    return sorted({name_of(ctx, s) for s in players(ctx)})


def tl_round(ctx, rnd):
    """timeline.json 里的这一回合（dict）；没有返回 None"""
    d = _data(ctx)
    m = getattr(d, '_tl_rounds', None)
    if m is None:
        m = {int(r['round']): r for r in ctx.json('timeline.json')['rounds']}
        d._tl_rounds = m
    return m.get(int(rnd))


def sid_of(ctx, name):
    return _data(ctx).by_name.get(str(name))


def team_of(ctx, sid, rnd):
    """这回合 sid 的阵营号 2(T) / 3(CT)；不在名单返回 0"""
    return _data(ctx).roster.get(int(rnd), {}).get(_sid_str(sid), 0)


def side_of(ctx, sid, rnd):
    """这回合 sid 的阵营 'T' / 'CT'"""
    return side_name(team_of(ctx, sid, rnd))


def half(ctx, rnd):
    """第几个半场（0 上半场、1 下半场、2 起是加时的每个半场）；按回合号和赛制切（thresholds/common.json 的
    half_rounds / ot_half_rounds），不按阵营变化——加时上半场和常规下半场同一边也是两个半场"""
    from . import config
    hr, ot = config.halves_th(ctx.th)
    return config.half_of(int(rnd), hr, ot)


def user_side(ctx, rnd):
    return side_of(ctx, user_sid(ctx), rnd)


def mates(ctx, rnd, sid=None, include_self=False):
    """这回合 sid（默认用户）的队友 steamid（排好序）"""
    sid = _sid_str(sid or user_sid(ctx))
    tm = team_of(ctx, sid, rnd)
    return [s for s, t in sorted(_data(ctx).roster.get(int(rnd), {}).items()) if t == tm and (include_self or s != sid)]


def enemies(ctx, rnd, sid=None):
    """这回合 sid（默认用户）的对手 steamid（排好序）"""
    sid = _sid_str(sid or user_sid(ctx))
    tm = team_of(ctx, sid, rnd)
    return [s for s, t in sorted(_data(ctx).roster.get(int(rnd), {}).items()) if t not in (tm, 0)]


def is_enemy(ctx, a, b, rnd):
    ta, tb = team_of(ctx, a, rnd), team_of(ctx, b, rnd)
    return ta in (2, 3) and tb in (2, 3) and ta != tb


# ================================================================ C-ALIVE 存活 / 人数
def death_tick(ctx, sid, rnd):
    """sid 这回合的阵亡 tick（player_death），没死返回 None"""
    return _data(ctx).death_tick.get((int(rnd), _sid_str(sid)))


def alive(ctx, sid, tick, rnd=None):
    """C-ALIVE：tick 时 sid 还活着（按击杀栏：这回合在名单里、阵亡 tick 晚于 tick）"""
    rnd = rnd or round_of(ctx, tick)
    if rnd is None or team_of(ctx, sid, rnd) == 0:
        return False
    dt = death_tick(ctx, sid, rnd)
    return dt is None or dt > int(tick)


def alive_counts(ctx, tick, sid=None, lag_s=None):
    """C-ALIVE：某一刻 (我方人数, 对方人数)，我方 = sid（默认用户）那一边。
    只数 player_death：死亡 tick 严格早于 tick、并且至少早 lag_s 秒（默认 alive_lag_s=0.25）的才算已死。
    要"这次击杀之后"的人数，传 tick=击杀 tick + 1、lag_s=0。"""
    d = _data(ctx)
    sid = _sid_str(sid or user_sid(ctx))
    rnd = round_of(ctx, tick)
    if rnd is None:
        return None
    lag = d.th['alive_lag_s'] if lag_s is None else lag_s
    lim = min(int(tick) - 1, int(math.floor(int(tick) - lag * d.tr)))
    tm = team_of(ctx, sid, rnd)
    us = them = 0
    for s, t in sorted(d.roster.get(rnd, {}).items()):
        dt = d.death_tick.get((rnd, s))
        if dt is not None and dt <= lim:
            continue
        if t == tm:
            us += 1
        else:
            them += 1
    return us, them


def alive_at_death(ctx, victim, rnd):
    """死者阵亡那一刻（按 C-ALIVE 口径，自己的死不算进去）的 (死者一方, 对方) 人数；没死返回 None"""
    dt = death_tick(ctx, victim, rnd)
    return None if dt is None else alive_counts(ctx, dt, sid=victim)


def alive_list(ctx, rnd, tick, side_sid=None, enemy=False):
    """tick 时还活着的 sid 一方（enemy=True 取对方）的 steamid（按击杀栏，排好序）"""
    group = enemies(ctx, rnd, side_sid) if enemy else mates(ctx, rnd, side_sid, include_self=True)
    return [s for s in group if alive(ctx, s, tick, rnd)]


# ================================================================ 帧查询（位置、视角、武器、报点）
def pos(ctx, sid, tick):
    """脚下 3D 位置 np.array([x, y, z])"""
    d = _data(ctx)
    i, c = d.fi(tick), d.col[_sid_str(sid)]
    return np.array([d.X[i, c], d.Y[i, c], d.Z[i, c]], dtype=float)


def eye(ctx, sid, tick):
    """眼睛 3D 位置（站 +64，蹲下按 duck_amount 降 18）"""
    d = _data(ctx)
    i, c = d.fi(tick), d.col[_sid_str(sid)]
    return np.array([d.X[i, c], d.Y[i, c], d.Z[i, c] + d.th['eye_height'] - d.th['crouch_drop'] * d.duck[i, c]], dtype=float)


def view(ctx, sid, tick):
    """(pitch, yaw)：准星方向"""
    d = _data(ctx)
    i, c = d.fi(tick), d.col[_sid_str(sid)]
    return float(d.pitch[i, c]), float(d.yaw[i, c])


def hp(ctx, sid, tick):
    """帧上的血量（只用来写后果或赛后推算，P1：不能用来说"你该知道"）"""
    d = _data(ctx)
    return float(d.hp[d.fi(tick), d.col[_sid_str(sid)]])


def weapon_at(ctx, sid, tick):
    """手上武器的显示名（'AK-47'），没有返回 None；句子里用 weapon_cn(weapon_at(...))"""
    d = _data(ctx)
    k = int(d.wpn[d.fi(tick), d.col[_sid_str(sid)]])
    return None if k < 0 else str(d.wpn_names[k])


def scoped_at(ctx, sid, tick):
    d = _data(ctx)
    return bool(d.scoped[d.fi(tick), d.col[_sid_str(sid)]])


def place_at(ctx, sid, tick):
    """last_place_name 原文（判断用）；句子里用 place_cn_at"""
    d = _data(ctx)
    k = int(d.place[d.fi(tick), d.col[_sid_str(sid)]])
    return None if k < 0 else str(d.place_names[k])


def place_cn_at(ctx, sid, tick, default='某处'):
    return place_cn(place_at(ctx, sid, tick), default)


def nearest_place(ctx, xyz, rnd=None):
    """某个 3D 点的报点：取（本回合）所有人帧位置里离它最近那一帧的 last_place_name（给道具落点等没有报点的位置用）"""
    d = _data(ctx)
    key = ('place_pts', rnd)
    if key not in d.memo:
        if rnd is not None:
            r = d.rmap[int(rnd)]
            i0, i1 = d.frange(r['freeze_start'], r['end'])
        else:
            i0, i1 = 0, len(d.ticks)
        sl = slice(i0, i1, 16)
        P = np.stack([d.X[sl].ravel(), d.Y[sl].ravel(), d.Z[sl].ravel()], axis=1)
        N = d.place[sl].ravel()
        ok = np.isfinite(P).all(axis=1) & (N >= 0)
        d.memo[key] = (P[ok], N[ok])
    P, N = d.memo[key]
    if not len(P):
        return None
    k = int(np.argmin(((P - np.asarray(xyz, float)[:3]) ** 2).sum(axis=1)))
    return str(d.place_names[int(N[k])])


# ================================================================ 几何（3D）
def dist3(a, b):
    """3D 距离（单位）。可见性、补枪、听声一律用这个"""
    return float(np.linalg.norm(np.asarray(a, float)[:3] - np.asarray(b, float)[:3]))


def dist2(a, b):
    """平面距离：只给"站位远近、走了多远"用，不能用来判看不看得见"""
    return float(math.hypot(a[0] - b[0], a[1] - b[1]))


def view_vec(pitch, yaw):
    """准星方向单位向量（pitch 正 = 往下看）"""
    p, y = math.radians(pitch), math.radians(yaw)
    return np.array([math.cos(p) * math.cos(y), math.cos(p) * math.sin(y), -math.sin(p)])


def aim_err(src, pitch, yaw, dst):
    """准星到 dst 的偏差：(总偏差°, 左右°(正=准星在他左边), 高低°(正=准星高于目标点), 3D 距离)"""
    dv = np.asarray(dst, float) - np.asarray(src, float)
    dist = float(np.linalg.norm(dv))
    if dist < 1 or not np.isfinite(dist) or not (np.isfinite(pitch) and np.isfinite(yaw)):
        return float('nan'), float('nan'), float('nan'), dist
    need_yaw = math.degrees(math.atan2(dv[1], dv[0]))
    need_pitch = -math.degrees(math.atan2(dv[2], math.hypot(dv[0], dv[1])))
    tot = math.degrees(math.acos(float(np.clip(np.dot(view_vec(pitch, yaw), dv / dist), -1, 1))))
    lr = (yaw - need_yaw + 180) % 360 - 180
    ud = need_pitch - pitch
    return tot, lr, ud, dist


def half_body_deg(ctx, dist):
    """半身宽对应的角度：atan(16 / 3D 距离)"""
    return math.degrees(math.atan2(_th(ctx)['half_body_units'], max(float(dist), 1.0)))


def body_err(ctx, src, pitch, yaw, sid, tick):
    """准星到 sid 身体（脚上 10 到眼睛这根竖线）的最小偏差（度）。认"在打谁/朝谁开枪"用"""
    d = _data(ctx)
    th = d.th
    i, c = d.fi(tick), d.col[_sid_str(sid)]
    x, y, z = d.X[i, c], d.Y[i, c], d.Z[i, c]
    if not (np.isfinite(x) and np.isfinite(pitch) and np.isfinite(yaw)):
        return float('nan')
    top = z + th['eye_height'] - th['crouch_drop'] * d.duck[i, c]
    zs = np.linspace(z + th['body_bottom'], top, int(th['body_samples']))
    P = np.stack([np.full_like(zs, x), np.full_like(zs, y), zs], axis=1) - np.asarray(src, float)
    n = np.linalg.norm(P, axis=1)
    n[n < 1] = 1
    cosv = np.clip((P @ view_vec(pitch, yaw)) / n, -1, 1)
    return float(np.degrees(np.arccos(cosv)).min())


def facing_deg(ctx, sid, tick, dst):
    """sid 水平朝向和指向 dst 的方向的夹角（度）"""
    pitch, yaw = view(ctx, sid, tick)
    p = pos(ctx, sid, tick)
    need = math.degrees(math.atan2(dst[1] - p[1], dst[0] - p[0]))
    return abs((yaw - need + 180) % 360 - 180)


def path_len(ctx, sid, t0, t1):
    """[t0, t1] 内走过的平面路程（每 move_window_ticks 采一点，瞬移段不算）"""
    d = _data(ctx)
    c = d.col[_sid_str(sid)]
    w = int(d.th['move_window_ticks'])
    tk = list(range(int(t0), int(t1) + 1, w))
    if len(tk) < 2:
        return 0.0
    idx = [d.fi(t) for t in tk]
    xs, ys = d.X[idx, c], d.Y[idx, c]
    seg = np.hypot(np.diff(xs), np.diff(ys))
    seg = seg[np.isfinite(seg) & (seg <= d.th['move_teleport'] * w / d.tr)]
    return float(seg.sum())


# ================================================================ C-STILL 停稳
def max_speed(ctx, w, scoped=False):
    """这把枪的最大移动速度（开镜的狙用开镜速度）"""
    th = _th(ctx)
    k = wkey(w)
    if scoped and k in th['max_speed_scoped']:
        return float(th['max_speed_scoped'][k])
    return float(th['max_speed'].get(k, th['max_speed_default']))


def still_limit(ctx, w, scoped=False):
    """停稳上限 = 最大速度 × 0.34（AK 约 73、M4 约 76、沙鹰约 78、USP 约 82、开镜 AWP 约 34）"""
    return _th(ctx)['still_frac'] * max_speed(ctx, w, scoped)


def move_word(ctx, spd, w, scoped=False):
    """C-STILL 三档：'停稳' / '还没停稳' / '跑着'（> 180）"""
    if spd is None or not np.isfinite(spd):
        return '停稳'
    if spd <= still_limit(ctx, w, scoped):
        return '停稳'
    return '跑着' if spd > _th(ctx)['run_speed'] else '还没停稳'


def move_speed(ctx, sid, tick):
    """不开枪时的水平移动速度：tick 和 tick - move_window_ticks 两帧的平面位移 / 时间（单位/秒）；瞬移或缺帧返回 None"""
    d = _data(ctx)
    c = d.col[_sid_str(sid)]
    i1, i0 = d.fi(tick), d.fi(int(tick) - int(d.th['move_window_ticks']))
    if i1 <= i0:
        return 0.0
    dt = (d.ticks[i1] - d.ticks[i0]) / d.tr
    v = math.hypot(d.X[i1, c] - d.X[i0, c], d.Y[i1, c] - d.Y[i0, c]) / dt
    if not np.isfinite(v) or v > d.th['move_teleport']:
        return None
    return float(v)


def fire_speed(ctx, row):
    """开枪那一刻的水平速度：weapon_fire 的 velocity_X/Y；没有这两列时用 move_speed"""
    vx, vy = getattr(row, 'user_velocity_X', None), getattr(row, 'user_velocity_Y', None)
    if vx is not None and vy is not None:
        v = math.hypot(float(vx), float(vy))
        if np.isfinite(v):
            return v
    return move_speed(ctx, row.user_steamid, int(row.tick))


# ================================================================ 事件查询
def deaths(ctx, rnd=None):
    """player_death（列：tick, round, att, vic, wk, headshot, thrusmoke, ...），按 tick 排"""
    d = _data(ctx).death
    return d if rnd is None else d[d['round'] == int(rnd)]


def hurts(ctx, rnd=None):
    """player_hurt（列：tick, round, att, vic, wk, dmg, health, hitgroup）"""
    h = _data(ctx).hurt
    return h if rnd is None else h[h['round'] == int(rnd)]


def fires(ctx, rnd=None, sid=None, guns_only=True):
    """weapon_fire（列：tick, round, sid, wk, gun, user_X/Y/Z, user_pitch/yaw, user_velocity_X/Y, user_is_scoped）"""
    f = _data(ctx).fire
    if guns_only:
        f = f[f['gun']]
    if rnd is not None:
        f = f[f['round'] == int(rnd)]
    if sid is not None:
        f = f[f['sid'] == _sid_str(sid)]
    return f


def blind_left(ctx, sid, tick):
    """tick 时 sid 还剩多少秒白屏（player_blind 起点 + 时长），没被闪返回 0。阈值由规则自己定"""
    b = _data(ctx).blind
    if not len(b) or 'blind_duration' not in b:
        return 0.0
    w = b[(b['user_steamid'] == _sid_str(sid)) & (b['tick'] <= int(tick)) & (b['tick'] >= int(tick) - 10 * TR)]
    left = 0.0
    for r in w.itertuples():
        left = max(left, float(r.blind_duration) - (int(tick) - int(r.tick)) / TR)
    return max(0.0, left)


# ================================================================ C-HIT 命中
def shot_hits(ctx, shooter, tick, victim=None, rnd=None):
    """C-HIT：开枪后 hit_ticks（3）个 tick 内这名开枪人打敌人的 player_hurt 行（victim 给了就只看他）"""
    d = _data(ctx)
    h = d.hurt
    lo, hi = int(tick), int(tick) + int(d.th['hit_ticks'])
    i0, i1 = np.searchsorted(h['tick'].to_numpy(), [lo, hi + 1])
    w = h.iloc[i0:i1]
    w = w[w['att'] == _sid_str(shooter)]
    rnd = rnd or round_of(ctx, tick)
    w = w[[is_enemy(ctx, shooter, v, rnd) for v in w['vic']]] if len(w) else w
    if victim is not None:
        w = w[w['vic'] == _sid_str(victim)]
    return w


# ================================================================ spotted / C-RADAR
def spots(ctx, a, b, t0, t1=None):
    """a 看见了 b：[t0, t1] 内有一帧 b 的 approximate_spotted_by 里有 a（两人都还活着）。返回第一帧 tick 或 None"""
    d = _data(ctx)
    a, b = _sid_str(a), _sid_str(b)
    if a not in d.col or b not in d.col:
        return None
    t1 = t0 if t1 is None else t1
    rnd = round_of(ctx, t1)
    end = int(t1)
    for s in (a, b):
        dt = death_tick(ctx, s, rnd) if rnd else None
        if dt is not None:
            end = min(end, dt - 1)
    i0, i1 = d.frange(t0, end)
    if i1 <= i0:
        return None
    hit = np.nonzero(d.spot[i0:i1, d.col[b]] & d.bit[a])[0]
    return int(d.ticks[i0 + hit[0]]) if len(hit) else None


def server_spotted(ctx, t0, t1):
    """[t0, t1] 内全服务器有没有任何活人的 spotted 记录（spotted 整段缺失时 C-SEEN f 才生效）"""
    d = _data(ctx)
    i0, i1 = d.frange(t0, t1)
    return bool(d.any_spot[i0:i1].any()) if i1 > i0 else False


def _radar_hits(ctx, enemy, t0, t1, team_sid):
    """[t0, t1]（截到本回合冻结结束）内敌人 enemy 在 team_sid 那一队雷达上的帧 tick 数组"""
    d = _data(ctx)
    enemy = _sid_str(enemy)
    team_sid = _sid_str(team_sid or user_sid(ctx))
    rnd = round_of(ctx, t1)
    if rnd is None or enemy not in d.col:
        return np.array([], dtype=np.int64)
    t0 = max(int(t0), d.rmap[rnd]['start'])
    end = int(t1)
    dte = death_tick(ctx, enemy, rnd)
    if dte is not None:
        end = min(end, dte - 1)
    i0, i1 = d.frange(t0, end)
    if i1 <= i0:
        return np.array([], dtype=np.int64)
    tk = d.ticks[i0:i1]
    M = d.spot[i0:i1, d.col[enemy]]
    ok = np.zeros(len(tk), dtype=bool)
    for m in mates(ctx, rnd, team_sid, include_self=True):
        dtm = death_tick(ctx, m, rnd)
        ok |= ((M & d.bit[m]) != 0) & ((tk < dtm) if dtm is not None else True)
    return tk[ok]


def on_radar(ctx, enemy, t0, t1, team_sid=None):
    """C-RADAR：[t0, t1] 内敌人上过 team_sid（默认用户）一队的雷达（我方任一活人 spotted 到他）。回看截到本回合冻结结束"""
    return len(_radar_hits(ctx, enemy, t0, t1, team_sid)) > 0


def last_on_radar(ctx, enemy, tick, look_s=None, team_sid=None):
    """C-RADAR：tick 之前（含）look_s 秒内（默认 radar_look_s，截到冻结结束）敌人最后一次在我方雷达上的 tick，没有 None"""
    look = _th(ctx)['radar_look_s'] if look_s is None else look_s
    h = _radar_hits(ctx, enemy, int(tick) - int(look * TR), int(tick), team_sid)
    return int(h[-1]) if len(h) else None


SILENCED = ('m4a1_silencer', 'usp_silencer')     # 消音枪：枪声听不出位置，不算“露面”


def _reveal(ctx, enemy, tick, team_sid=None):
    """本回合冻结结束到 tick（不含）之间，敌人最后一次“露面”：(tick, 方式, 报点原文) 或 (None, None, None)。
    方式：'radar'（上过我方雷达）/ 'kill'（击杀栏里杀过人）/ 'shot'（你听得到他开枪，消音枪不算）——都是玩家能拿到的信息"""
    rnd = round_of(ctx, tick)
    if rnd is None:
        return None, None, None
    t0 = _data(ctx).rmap[rnd]['start']
    best = (None, None, None)
    h = _radar_hits(ctx, enemy, t0, int(tick) - 1, team_sid)
    if len(h):
        best = (int(h[-1]), 'radar', place_at(ctx, enemy, int(h[-1])))
    D = deaths(ctx, rnd)
    ks = D[(D['att'] == _sid_str(enemy)) & (D['tick'] >= t0) & (D['tick'] < int(tick))]
    if len(ks):
        kt = int(ks['tick'].max())
        if best[0] is None or kt > best[0]:
            best = (kt, 'kill', place_at(ctx, enemy, kt))
    # 枪声只算你自己听到的（没有语音数据，队友听到的你拿不到）
    u = user_sid(ctx)
    if team_sid is None or team_of(ctx, _sid_str(team_sid), rnd) == team_of(ctx, u, rnd):
        hs = heard_shots(ctx, u, t0, int(tick) - 1, shooters=[enemy])
        hs = hs[~hs['wk'].isin(SILENCED)] if len(hs) else hs
        if len(hs):
            st = int(hs['tick'].max())
            if best[0] is None or st > best[0]:
                best = (st, 'shot', place_at(ctx, enemy, st))
    return best


def last_revealed(ctx, enemy, tick, team_sid=None):
    """P1：本回合冻结结束到 tick（不含）之间，敌人最后一次“露面”的 tick：上过我方雷达、在击杀栏里杀过人，
    或者你听得到他开枪（玩家都能拿到的信息）。从没露过面返回 None。“一直没露过面”这种话要用它判断，不能只看用户自己看没看到"""
    return _reveal(ctx, enemy, tick, team_sid)[0]


def hidden_phrase(ctx, enemy, tick, team_sid=None):
    """“没露过面”的说法：(短语, 最后露面 tick 或 None)。
    整回合没露过面 → '之前一直没露过面'；露过面 → '第 N 秒露过面之后就没再出现'（N 是最后一次露面）；
    最后一次是你听到他开枪 → '第 N 秒在 X 一带开过枪之后就没再露面'（8d R14：对手1 在拱门开的枪你听得到）"""
    lt, how, pl = _reveal(ctx, enemy, tick, team_sid)
    if lt is None:
        return '之前一直没露过面', None
    ts = fmt_sec(t_of(ctx, lt))
    if int(tick) - lt <= int(_th(ctx)['reveal_recent_s'] * TR):
        if how == 'shot':
            return f'第 {ts} 秒刚在{place_cn(pl)}一带开过枪', lt
        return f'第 {ts} 秒刚露过面', lt          # 刚露过面：不能说“之后没再出现”
    if how == 'shot':
        return f'第 {ts} 秒在{place_cn(pl)}一带开过枪之后就没再露面', lt
    return f'第 {ts} 秒露过面之后就没再出现', lt


def radar_enemies(ctx, tick, look_s=0.0, team_sid=None):
    """tick 前 look_s 秒内上过我方雷达的敌人：[(sid, 最后一次 tick, 当时报点原文)]，按 sid 排"""
    rnd = round_of(ctx, tick)
    out = []
    for e in enemies(ctx, rnd, team_sid):
        lt = last_on_radar(ctx, e, tick, look_s, team_sid)
        if lt is not None:
            out.append((e, lt, place_at(ctx, e, lt)))
    return out


# ================================================================ C-SMOKE 隔烟
def smokes(ctx, rnd=None):
    """生效的烟：[{t0, t1, x, y, z, by, entityid, round}]（起爆→消散，按 entityid 配对，配不上按 20 秒），按起爆排"""
    d = _data(ctx)
    if 'smokes' not in d.memo:
        det = d._norm_simple(d._event(ctx, 'smokegrenade_detonate'), int(d._fs[0]) if len(d._fs) else 0)
        exp = d._norm_simple(d._event(ctx, 'smokegrenade_expired'), int(d._fs[0]) if len(d._fs) else 0)
        ex = {}
        if len(exp) and 'entityid' in exp:
            for r in exp.itertuples():
                ex.setdefault(int(r.entityid), []).append(int(r.tick))
        out = []
        dflt = int(d.th['smoke_default_s'] * d.tr)
        for r in det.itertuples() if len(det) else []:
            e = sorted(t for t in ex.get(int(getattr(r, 'entityid', -1)), []) if t > r.tick)
            out.append(dict(t0=int(r.tick), t1=e[0] if e else int(r.tick) + dflt, x=float(r.x), y=float(r.y),
                            z=float(r.z), by=_sid_str(getattr(r, 'user_steamid', None)),
                            entityid=int(getattr(r, 'entityid', -1)), round=int(r.round)))
        d.memo['smokes'] = sorted(out, key=lambda s: (s['t0'], s['entityid']))
    s = d.memo['smokes']
    return s if rnd is None else [x for x in s if x['round'] == int(rnd)]


def _seg_hits_cyl(a, b, c, r, z0, z1):
    """3D 线段 ab 是否穿过竖直圆柱（圆心 c 的 xy、半径 r、高度 z0..z1）"""
    a, b = np.asarray(a, float), np.asarray(b, float)
    dx, dy = b[0] - a[0], b[1] - a[1]
    fx, fy = a[0] - c[0], a[1] - c[1]
    A = dx * dx + dy * dy
    B = 2 * (fx * dx + fy * dy)
    Cc = fx * fx + fy * fy - r * r
    if A < 1e-9:                        # 竖直线段
        if Cc > 0:
            return False
        lo, hi = 0.0, 1.0
    else:
        disc = B * B - 4 * A * Cc
        if disc < 0:
            return False
        sq = math.sqrt(disc)
        lo, hi = max(0.0, (-B - sq) / (2 * A)), min(1.0, (-B + sq) / (2 * A))
        if lo > hi:
            return False
    za, zb = a[2] + lo * (b[2] - a[2]), a[2] + hi * (b[2] - a[2])
    return max(za, zb) >= z0 and min(za, zb) <= z1


def smoke_between(ctx, a, b, tick):
    """C-SMOKE：3D 点 a → b 的视线线段在 tick 时穿过一颗生效中的烟，返回那颗烟（dict），没有 None"""
    th = _th(ctx)
    for s in smokes(ctx, round_of(ctx, tick)):
        if s['t0'] <= int(tick) <= s['t1'] and _seg_hits_cyl(a, b, (s['x'], s['y']), th['smoke_radius'],
                                                              s['z'] + th['smoke_z_low'], s['z'] + th['smoke_height']):
            return s
    return None


def smoked(ctx, sid_a, sid_b, tick):
    """两个人眼睛之间隔着烟（C-SMOKE）"""
    return smoke_between(ctx, eye(ctx, sid_a, tick), eye(ctx, sid_b, tick), tick)


# ================================================================ C-HEAR 听得到
def hear_limit(ctx, near=False):
    th = _th(ctx)
    return float(th['hear_near'] if near else th['hear_shot'])


def can_hear(ctx, listener, src_xyz, tick, near=False):
    """C-HEAR：tick 时 listener 离声源 3D ≤ 2000（near=True：≤ 600，"就在旁边"）。只是距离近似"""
    return dist3(pos(ctx, listener, tick), src_xyz) <= hear_limit(ctx, near)


def heard_shots(ctx, listener, t0, t1, shooters=None, near=False):
    """C-HEAR：[t0, t1] 内 listener 活着时听得到的枪声（weapon_fire 枪械，开枪人离他 3D 在线内），返回 fire 行"""
    f = fires(ctx)
    i0, i1 = np.searchsorted(f['tick'].to_numpy(), [int(t0), int(t1) + 1])
    w = f.iloc[i0:i1]
    listener = _sid_str(listener)
    w = w[w['sid'] != listener]
    if shooters is not None:
        w = w[w['sid'].isin([_sid_str(s) for s in shooters])]
    lim = hear_limit(ctx, near)
    keep = []
    for r in w.itertuples():
        if not alive(ctx, listener, int(r.tick)):
            keep.append(False)
            continue
        keep.append(dist3(pos(ctx, listener, int(r.tick)), (r.user_X, r.user_Y, r.user_Z)) <= lim)
    return w[np.array(keep, dtype=bool)] if len(w) else w


# ================================================================ C-SEEN 看得见
def _aim_lim(ctx, dist, min_deg, n_half):
    return max(float(min_deg), float(n_half) * half_body_deg(ctx, dist))


def aim_limit(ctx, dist, min_deg, n_half):
    """角度门槛 max(min_deg, n_half 个半身宽)：规则要用自己的口径（例如 SHOT-04 狙 3°）时用这个"""
    return _aim_lim(ctx, dist, min_deg, n_half)


def _fire_aims(ctx, shooter):
    """shooter 每一枪（枪械）对对面每个活人身体的偏差：(tick 数组, [{sid: (偏差°, 3D 距离)}])，C-SEEN d 用"""
    d = _data(ctx)
    shooter = _sid_str(shooter)
    key = ('aims', shooter)
    if key in d.memo:
        return d.memo[key]
    tks, ms = [], []
    for r in fires(ctx, sid=shooter).itertuples():
        tick, rnd = int(r.tick), int(r.round)
        if rnd <= 0:
            continue
        src = _shot_src(ctx, shooter, r)
        pit, yw = _shot_view(ctx, shooter, r)
        m = {}
        for q in enemies(ctx, rnd, shooter):
            if not alive(ctx, q, tick, rnd):
                continue
            m[q] = (body_err(ctx, src, pit, yw, q, tick), dist3(eye(ctx, q, tick), src))
        tks.append(tick)
        ms.append(m)
    d.memo[key] = (np.array(tks, dtype=np.int64), ms)
    return d.memo[key]


def _fire_place(ctx, sid, row):
    """开枪时的报点原文：weapon_fire 上的 last_place_name，没有时取帧上的"""
    v = getattr(row, 'user_last_place_name', None)
    return v if isinstance(v, str) and v else place_at(ctx, sid, int(row.tick))


def _shot_src(ctx, sid, row):
    """开枪人眼睛位置：weapon_fire 上的脚下位置 + 眼高（蹲取帧上的 duck）"""
    d = _data(ctx)
    i, c = d.fi(int(row.tick)), d.col[_sid_str(sid)]
    x, y, z = getattr(row, 'user_X', np.nan), getattr(row, 'user_Y', np.nan), getattr(row, 'user_Z', np.nan)
    if not np.all(np.isfinite([x, y, z])):
        return eye(ctx, sid, int(row.tick))
    return np.array([x, y, z + d.th['eye_height'] - d.th['crouch_drop'] * d.duck[i, c]], dtype=float)


def _shot_view(ctx, sid, row):
    """开枪方向：weapon_fire 上的 pitch/yaw；没有时用帧上的"""
    p, y = getattr(row, 'user_pitch', np.nan), getattr(row, 'user_yaw', np.nan)
    if p is None or y is None or not (np.isfinite(p) and np.isfinite(y)):
        return view(ctx, sid, int(row.tick))
    return float(p), float(y)


def seen(ctx, viewer, target, tick, shot=None, f_min_deg=None, f_need_radar=True):
    """C-SEEN：tick 时 viewer 看得见 target，返回满足的第一条 'a'..'f'，都不满足返回 None。
    a spotted；b 这一枪打中他（要 shot）；c 他 ±1 秒内打中过 viewer；d 他 ±1 秒内朝 viewer 开枪（准星离 viewer 身体
    < max(1.5°, 2 半身宽)，并且 viewer 是我方活人里离枪线最近的、或他那一刻 spotted 了 viewer）；
    e 他 ±1 秒内 spotted 了 viewer，并且 viewer 这一枪离他身体 ≤ max(5°, 3 半身宽)（要 shot）；
    f 全服务器 ±16 tick 没有 spotted 记录，这一枪离他身体 < max(2°, 1.5 半身宽)，并且他 ±1.5 秒内上过我方雷达（要 shot）。
    shot = shots() 里的一枪（dict，含 tick/src/pitch/yaw/hit_vics），或 None（只看 a/c/d）。
    f_min_deg / f_need_radar：规则自己的 f 口径（例如 SHOT-04 狙：不到 3°、不要雷达条件），默认按 common。"""
    d = _data(ctx)
    th = d.th
    viewer, target = _sid_str(viewer), _sid_str(target)
    tick = int(tick)
    tr = d.tr
    # a
    if spots(ctx, viewer, target, tick - int(th['seen_a_back_ticks']), tick + int(th['seen_a_fwd_ticks'])) is not None:
        return 'a'
    # b
    if shot is not None and target in shot.get('hit_vics', ()):
        return 'b'
    # c
    h = d.hurt
    lo, hi = tick - int(th['seen_c_s'] * tr), tick + int(th['seen_c_s'] * tr)
    i0, i1 = np.searchsorted(h['tick'].to_numpy(), [lo, hi + 1])
    w = h.iloc[i0:i1]
    if len(w) and ((w['att'] == target) & (w['vic'] == viewer)).any():
        return 'c'
    # d
    lo, hi = tick - int(th['seen_d_s'] * tr), tick + int(th['seen_d_s'] * tr)
    ftk, fms = _fire_aims(ctx, target)
    j0, j1 = np.searchsorted(ftk, [lo, hi + 1])
    for j in range(int(j0), int(j1)):
        ft, m = int(ftk[j]), fms[j]
        if viewer not in m:
            continue
        err, dist = m[viewer]
        if not np.isfinite(err) or err >= _aim_lim(ctx, dist, th['seen_d_min_deg'], th['seen_d_half_bodies']):
            continue
        closest = min(sorted(m.items()), key=lambda kv: (kv[1][0] if np.isfinite(kv[1][0]) else 999, kv[0]))[0]
        if closest == viewer or spots(ctx, target, viewer, ft - int(th['seen_d_spot_ticks']), ft) is not None:
            return 'd'
    if shot is None:
        return None
    err = body_err(ctx, shot['src'], shot['pitch'], shot['yaw'], target, tick)
    if not np.isfinite(err):
        return None
    dist = dist3(eye(ctx, target, tick), shot['src'])
    # e（seen_e_mode：he_sees = 他 spotted 了 viewer；viewer_sees = viewer spotted 了他；either = 两种都算）
    if err <= _aim_lim(ctx, dist, th['seen_e_min_deg'], th['seen_e_half_bodies']):
        lo, hi = tick - int(th['seen_e_s'] * tr), tick + int(th['seen_e_s'] * tr)
        mode = th.get('seen_e_mode', 'he_sees')
        if mode in ('he_sees', 'either') and spots(ctx, target, viewer, lo, hi) is not None:
            return 'e'
        if mode in ('viewer_sees', 'either') and spots(ctx, viewer, target, lo, hi) is not None:
            return 'e'
    # f（rules_v1：必须带雷达条件；seen_f_blackout_ok=true 时退回原型做法：±1.5 秒全服务器都没有 spotted 也算）
    g = int(th['seen_f_gap_ticks'])
    fmin = th['seen_f_min_deg'] if f_min_deg is None else f_min_deg
    if not server_spotted(ctx, tick - g, tick + g) and err < _aim_lim(ctx, dist, fmin, th['seen_f_half_bodies']):
        lo, hi = tick - int(th['seen_f_radar_s'] * tr), tick + int(th['seen_f_radar_s'] * tr)
        if not f_need_radar or on_radar(ctx, target, lo, hi, viewer):
            return 'f'
        if th.get('seen_f_blackout_ok', False) and not server_spotted(ctx, lo, hi):
            return 'f'
    return None


def seen_window(ctx, viewer, target, t0, t1):
    """[t0, t1] 内 viewer 第一次看得见 target：(tick, 条件字母) 或 None。
    帧上的 a/c/d 每 4 tick 查一次，再加上 viewer 在这段时间里对着 target 的开枪（b/e/f，来自 shots()）。补枪、"能不能补"用这个"""
    t0, t1 = int(t0), int(t1)
    best = None
    for s in shots(ctx, viewer):
        if t0 <= s['tick'] <= t1 and s['target'] == _sid_str(target) and s['seen'] in ('a', 'b', 'c', 'd', 'e', 'f'):
            best = (s['tick'], s['seen'])
            break
    for tk in range(t0, t1 + 1, 4):
        if best is not None and tk >= best[0]:
            break
        if not alive(ctx, viewer, tk) or not alive(ctx, target, tk):
            continue
        code = seen(ctx, viewer, target, tk)
        if code:
            return tk, code
    return best


# ================================================================ C-TARGET / C-FIGHT：用户（或任何人）的每一枪
def shots(ctx, sid=None):
    """sid（默认用户）每一枪枪械的认定结果，按 tick 排（同一 ctx 缓存）。每枪 dict：
      i, tick, round, t, sid, wk, weapon(中文), wcls, tier, sniper, scoped, spd, move('停稳'/'还没停稳'/'跑着'), still,
      pitch, yaw, src(眼睛 3D), place(报点原文),
      hit_vics(这一枪 C-HIT 打中的敌人), target(sid 或 None), seen('a'..'f' / 'chain' / None=对着人但看不见),
      hit, hitgroup, dmg, err_body(°), err_head(总°, 左右°, 高低°), dist(3D), hb(半身宽°),
      engage_first(C-FIGHT 新交火的第一枪), burst(所在串的编号)。"""
    d = _data(ctx)
    sid = _sid_str(sid or user_sid(ctx))
    key = ('shots', sid)
    if key in d.memo:
        return d.memo[key]
    th = d.th
    out = []
    for r in fires(ctx, sid=sid).itertuples():
        tick, rnd = int(r.tick), int(r.round)
        if rnd <= 0 or not alive(ctx, sid, tick, rnd):
            continue
        wk = r.wk
        sc = bool(getattr(r, 'user_is_scoped', False)) if getattr(r, 'user_is_scoped', None) is not None else scoped_at(ctx, sid, tick)
        spd = fire_speed(ctx, r)
        pit, yw = _shot_view(ctx, sid, r)
        src = _shot_src(ctx, sid, r)
        hv = shot_hits(ctx, sid, tick, rnd=rnd)
        s = dict(i=len(out), tick=tick, round=rnd, t=t_of(ctx, tick, rnd), sid=sid, wk=wk, weapon=weapon_cn(wk),
                 wcls=weapon_class(wk), tier=gun_tier(wk), sniper=is_sniper(wk), scoped=sc,
                 spd=None if spd is None else float(spd), pitch=pit, yaw=yw, src=src,
                 place=_fire_place(ctx, sid, r),
                 hit_vics=tuple(dict.fromkeys(hv['vic'])), target=None, seen=None, hit=False, hitgroup=None, dmg=0,
                 cands={})
        s['move'] = move_word(ctx, s['spd'], wk, sc)
        s['still'] = s['move'] == '停稳'
        # 候选：活着的敌人，准星离身体的偏差
        cands = {}
        for e in enemies(ctx, rnd, sid):
            if not alive(ctx, e, tick, rnd):
                continue
            be = body_err(ctx, src, pit, yw, e, tick)
            dist = dist3(eye(ctx, e, tick), src)
            cands[e] = (be, dist)
        s['cands'] = cands
        if s['hit_vics']:                                   # C-HIT：打中了谁就是在打谁
            tgt = s['hit_vics'][0]
            s['target'], s['seen'], s['hit'] = tgt, 'b', True
            row = hv[hv['vic'] == tgt].iloc[0]
            s['hitgroup'] = row.get('hitgroup')
            s['dmg'] = int(hv[hv['vic'] == tgt]['dmg'].sum())
        else:
            inlim = sorted((be, e) for e, (be, dist) in cands.items()
                           if np.isfinite(be) and be <= _aim_lim(ctx, dist, th['target_min_deg'], th['target_half_bodies']))
            vis = [(be, e, seen(ctx, sid, e, tick, s)) for be, e in inlim]
            vis = [v for v in vis if v[2]]
            if vis:                                         # C-TARGET + C-SEEN
                s['target'], s['seen'] = vis[0][1], vis[0][2]
            else:
                win = (th['chain_sniper_s'] if s['sniper'] else th['chain_s']) * d.tr
                prev = next((p for p in reversed(out) if p['round'] == rnd and p['target'] and p['seen']
                             and tick - p['tick'] <= win), None)
                lim = th['chain_deg_sniper'] if s['sniper'] else th['chain_deg']
                if prev is not None and prev['target'] in cands and np.isfinite(cands[prev['target']][0]) \
                        and cands[prev['target']][0] <= lim:
                    s['target'], s['seen'] = prev['target'], 'chain'   # 同一串 1 秒内的后续枪按 20°（狙 8°）接上
                elif inlim:
                    s['target'] = inlim[0][1]                         # 对着人，但看不见（seen=None）
        if s['target']:
            be, dist = cands.get(s['target'], (float('nan'), float('nan')))
            s['err_body'], s['dist'] = be, dist
            s['err_head'] = aim_err(src, pit, yw, eye(ctx, s['target'], tick))[:3]
            s['hb'] = half_body_deg(ctx, dist) if np.isfinite(dist) else float('nan')
        else:
            s['err_body'] = s['dist'] = s['hb'] = float('nan')
            s['err_head'] = (float('nan'),) * 3
        out.append(s)
    # C-FIGHT：新交火（只有对着看得见的人的枪才能开始一次交火，也只有它们刷新"上一次对着看得见的人开枪"）
    prev_tick, prev_tgt = None, None
    for s in out:
        on_seen = bool(s['target'] and s['seen'])
        new = prev_tick is None or (s['tick'] - prev_tick) > th['fight_gap_s'] * d.tr
        if (not new and s['target'] and prev_tgt and s['target'] != prev_tgt and s['seen'] not in (None, 'chain')
                and not alive(ctx, prev_tgt, s['tick'], s['round'])):
            new = True                                  # 上一个人已被打死，这一枪真的对着下一个人
        s['engage_first'] = bool(new and on_seen and s['seen'] != 'chain')   # 接上的枪不另开交火
        if on_seen:
            prev_tick, prev_tgt = s['tick'], s['target']
    # 分串：同一目标、同一回合、两枪间隔 ≤ 1 秒（狙 3 秒）就是同一串（SHOT-03 的口径）；串从第一枪算起，
    # 前面对着同一个人、还没有可见证据的枪也在串里
    b = -1
    for k, s in enumerate(out):
        p = out[k - 1] if k else None
        gap = (th['burst_gap_sniper_s'] if s['sniper'] else th['burst_gap_s']) * d.tr
        if p is None or s['target'] != p['target'] or s['round'] != p['round'] or s['tick'] - p['tick'] > gap:
            b += 1
        s['burst'] = b
    d.memo[key] = out
    return out


def bursts(ctx, sid=None):
    """按 C-FIGHT 分好的串：[{id, round, t, tick（都取串的第一枪）, sid, target, seen(串里有没有对着看得见的人的枪),
    engage_first(串里第一枪对着看得见的人的枪是新交火 = 第一串), engage_ticks,
    n, hits, wk, weapon, wcls, moves, shots(该串的枪 dict 列表)}]"""
    d = _data(ctx)
    sid = _sid_str(sid or user_sid(ctx))
    key = ('bursts', sid)
    if key in d.memo:
        return d.memo[key]
    out = []
    for s in shots(ctx, sid):
        if not out or out[-1]['id'] != s['burst']:
            out.append(dict(id=s['burst'], round=s['round'], t=s['t'], tick=s['tick'], sid=sid, target=s['target'],
                            wk=s['wk'], weapon=s['weapon'], wcls=s['wcls'], shots=[]))
        out[-1]['shots'].append(s)
    for bb in out:
        S = bb['shots']
        fs = next((x for x in S if x['target'] and x['seen']), None)
        bb['engage_first'] = bool(fs and fs['engage_first'])          # 串里第一枪对着看得见的人的枪是新交火 = 第一串
        bb['engage_ticks'] = [x['tick'] for x in S if x['engage_first']]   # 串里所有新交火起点（同一串中途再开交火的也列出）
        bb['n'] = len(S)
        bb['hits'] = sum(1 for x in S if x['hit'])
        bb['moves'] = [x['move'] for x in S]
        bb['seen'] = any(x['seen'] for x in S)
    d.memo[key] = out
    return out


def first_bursts(ctx, sid=None):
    """C-FIGHT 第一串（新交火的第一组连续开枪），只要对着看得见的人的"""
    return [b for b in bursts(ctx, sid) if b['engage_first'] and b['target'] and b['seen']]


# ================================================================ C-ECO 买法
def econ_round(ctx, rnd):
    """economy.json 这一回合（含 us / them / side / loss_counter ...）"""
    e = _json(ctx, 'economy')
    for r in e['rounds']:
        if int(r['round']) == int(rnd):
            return r
    return None


def buy(ctx, rnd, who='us'):
    """C-ECO：economy.json 的买法：'eco' / '强起' / '半起' / '全起' / '手枪局'；who = 'us' 或 'them'"""
    r = econ_round(ctx, rnd)
    return None if r is None else r[who].get('buy')


def econ_side(ctx, rnd, who='us'):
    """who 这一边这回合是 T 还是 CT"""
    r = econ_round(ctx, rnd)
    s = side_name(r['side']) if r else None
    return s if who == 'us' else other_side(s)


def full_buy_line(ctx, side):
    """全起线：T $3900、CT $4400"""
    return int(_th(ctx)['full_buy'][side_name(side)])


def force_line(ctx, side):
    """强起最低线：T $2050（吹风机 + 头盔甲）、CT $2250（MP9 + 头盔甲）"""
    return int(_th(ctx)['force_min'][side_name(side)])


def awp_line(ctx):
    """起狙线 $5750"""
    return int(_th(ctx)['awp_need'])


def can_force(ctx, rnd, who='us'):
    """C-ECO can_force：人均开局钱 ≥ $2500，并且 ≥ 4 人开局钱够强起最低线"""
    r = econ_round(ctx, rnd)
    if r is None:
        return False
    th = _th(ctx)
    blk = r[who]
    side = econ_side(ctx, rnd, who)
    starts = [int(p['start']) for p in blk.get('players', [])]
    if not starts:
        return False
    avg = blk.get('avg_start', sum(starts) / len(starts))
    n_ok = sum(1 for v in starts if v >= force_line(ctx, side))
    return avg >= th['force_avg'] and n_ok >= th['force_n']
