# -*- coding: utf-8 -*-
"""写复盘：hits.json + timeline.json + economy.json → work/<id>/review.json（字段和 pro/analysis/match3/review.json 一样，
match_template.html / engine/page_template.html 不改就能渲染；每个 moment 多一个 rule）。

所有文字都由规则句子（hit.text / alt / evidence）和下面的固定模板拼出来，不调用 AI：
  headline   回合标题：按“标题优先级表”取最重要的一条 hit 的短语（见 docs/dev/COMPOSE_NOTES.md）
  situation  买法一句：开局比分、我方谁拿什么、对面拿什么（不写 K/D/A、ADR）
  economy    idea 只来自 ECO-01..04，swing 只来自 ECO-06/07/08；买法正常时都为空
  main_cause / your_part / moments / ideas   merge.py（M1–M7）
  story      时间线：你的击杀、阵亡、道具，队友阵亡，首杀、补枪，下包、拆包（不写坐标、速度，不写对方血量）
  team       TEAM-01/02/03 的句子（没用作主要原因的）；没有时列队友的击杀
  rating     固定映射：你的部分 + 主要原因
  tags       回合类型 + 用到的规则名
summary 交给 summary.py。

pipeline 里调用 run(ctx)；调试：build_review(ctx, hits) 返回 dict，不写文件。
"""
import json
import re
from pathlib import Path

from . import common as C
from . import config
from . import merge as M
from . import summary as SUM

# ---------------------------------------------------------------- 标题短语（用户这一面前面加“你”）
PHRASE = {
    'SHOT-01': '第一枪没停稳就开', 'SHOT-02': '准星没放到位', 'SHOT-03': '一串打空没调整', 'SHOT-04': '狙空枪',
    'SHOT-05': '没停住就被先打中',
    'POS-01': '杀完人没收', 'POS-02': '同一条线重复拉', 'POS-03': '同一个点和好枪硬拼',
    'POS-04': '暴露后换位，被没露面的人打死', 'POS-05': '人多时主动去接五五开的枪', 'POS-06': '队友倒下没人补得上',
    'POS-07': '一块地方被两个方向连杀', 'POS-08': '贴着烟站被隔烟打死',
    'TEAM-04': '一个人先接触、身后没人', 'TEAM-05': '能补枪没补', 'TEAM-06': '和队友走散补不上',
    'UTIL-01': '道具留在身上就出去接枪', 'UTIL-02': '闪白队友没闪到人', 'UTIL-03': '被队友闪白后阵亡',
    'UTIL-05': '唯一一颗闪白扔了', 'UTIL-06': '狙击线没补烟', 'UTIL-07': '中路清空后没帮两边',
    'TIME-01': '转点路上开打了还静步', 'TIME-02': '火还在烧就一个人早到 A1', 'TIME-03': '下水道有动静还原地架',
    'TIME-05': '听到枪声还一个人冲', 'TIME-06': '被闪白了还往前冲', 'TIME-07': '烟快散了还慢慢横穿',
    'TIME-08': '开局一个人上已知的狙位', 'TIME-04': '进点太晚', 'SHOT-06': '对枪动作有一步没做好',
    'POST-01': '人数劣势没保枪', 'POST-02': '回防时身后的人补不上', 'POST-03': '挪位置时走进了对面的枪线',
    'POST-04': '残局守包边走边开枪', 'COST-01': '装备差距下输掉对枪',
    'SHOT-G1': '停稳先手拿下', 'SHOT-G2': '停稳开镜先手', 'SHOT-G3': '先手打没看你的人', 'SHOT-G4': '人数劣势里拿下击杀',
    'POS-G1': '拿到击杀后马上收', 'POS-G2': '狙到人后换了位置', 'TEAM-G1': '在队友倒下后及时补枪',
    'TEAM-G2': '和队友一起打同一个人', 'TEAM-G3': '反 eco 不单走', 'UTIL-G1': '开局往 A1 扔火',
    'UTIL-G2': '用道具打掉血后拿下击杀', 'UTIL-G3': '占优后用道具拖时间', 'TIME-G1': '看到动静及时转点',
    'POST-G1': '回防时和队友一起进点', 'ECO-G1': '捡枪升级',
}
# 回合结束方式（timeline 的 reason_cn 缺翻译时兜底；还是英文就不写）
REASON_CN = {'bomb_exploded': '炸弹爆炸', 'bomb_defused': '拆包', 't_killed': 'T 全灭', 'ct_killed': 'CT 全灭',
             'target_saved': '时间到', 'target_bombed': '炸弹爆炸'}
THEM_BUY_WORD = {'全起': '长枪', '强起': '强起', '半起': '半起', 'eco': 'eco', '手枪局': '手枪局'}


_ECO_TH = {}


def _pistols_only_is_eco():
    if 'v' not in _ECO_TH:
        try:
            _ECO_TH['v'] = bool(json.loads((config.THRESHOLDS / 'eco.json').read_text(encoding='utf-8')).get('pistols_only_is_eco'))
        except (OSError, ValueError):
            _ECO_TH['v'] = False
    return _ECO_TH['v']


def _anti_eco_buy(e):
    """我方钱数在强起 / 半起这一档，但对面 eco 或全员只拿手枪、我方大多数人起了冲锋枪 / 步枪 / 狙：这是反 eco，
    不叫“强起”（bd R2：赢了手枪局后 MP7、吹风机、AK、Galil 打对面全员手枪；用户口径里“强起”是手枪局输了的一方）"""
    us, them = (e or {}).get('us') or {}, (e or {}).get('them') or {}
    if us.get('buy') not in ('强起', '半起'):
        return False
    n_them = int(them.get('n') or 0)
    # 复核修正（Q21 对面半起不算反 eco）：“全员只拿手枪”只在 eco.json pistols_only_is_eco 打开时也算 eco，和 eco / team 同一口径
    pistols = them.get('buy') == 'eco' or (_pistols_only_is_eco() and n_them and int(them.get('n_pistol_only') or 0) >= n_them)
    n_us = int(us.get('n') or 0)
    longs = sum(int(us.get(k) or 0) for k in ('n_rifle', 'n_awp', 'n_auto', 'n_scout', 'n_smg', 'n_heavy'))
    return bool(pistols and n_us and longs * 2 > n_us)


def econ_label(e):
    """回合类型：标题前缀、标签、TEAM-02 的 eco 短语都只用 economy.json 的 buy（和买法一句同一个来源）。
    我方全起时按对面的买法：对面 eco → 反 eco；对面强起 / 半起 → 打对面强起 / 半起；双方全起 → 长枪局"""
    if not e:
        return ''
    us, th = (e.get('us') or {}).get('buy'), (e.get('them') or {}).get('buy')
    if _anti_eco_buy(e):
        return '反 eco'
    if us in ('手枪局', 'eco', '强起', '半起'):
        return us
    if us == '全起':
        if th == 'eco':
            return '反 eco'
        if th in ('强起', '半起'):
            return f'打对面{th}'
        return '长枪局'
    return ''


HEAD_LABELS = ('手枪局', 'eco', '强起', '半起', '反 eco', '打对面强起', '打对面半起')   # 标题前面加的回合类型（长枪局不加）
NADE_CN = {'smoke': '烟', 'flash': '闪', 'he': '雷', 'molotov': '火', 'incendiary': '火', 'decoy': '诱饵'}
RATING_OF_LEVEL = {'打得好': '打得好', '主要问题': '有问题'}


sp = M.sp      # 中文和英文/数字之间补空格


def reason_cn(tl_r):
    r = tl_r.get('reason_cn') or ''
    r = REASON_CN.get(r, r)
    return '' if r.isascii() else r


# SHOT-06（对枪动作判定，审核单第 2 条）动作有错的几种：标题 / 标签按错在哪一步写
DUEL_PHRASE = {'peek_into_holder': '拉出去撞上架着的人', 'aim_off': '准星没放到位', 'no_stop': '第一枪没停稳就开',
               'rush': '没等准星到身上就开枪', 'long_spray': '一口气扫到底没松开', 'fast_tap': '手枪点得太快',
               'no_retreat': '挨了第一下没退出视线', 'awp_run': 'AWP 跑着拉'}


def phrase(rid, h=None):
    """标题短语；POS-06 的弱守分支（decisions #23）单独一句"""
    br = ((h or {}).get('facts') or {}).get('branch')
    f0 = (h or {}).get('facts') or {}
    if rid == 'SHOT-06':
        return DUEL_PHRASE.get(str((f0.get('duel') or {}).get('key') or ''), PHRASE['SHOT-06'])
    if rid == 'SHOT-02' and f0.get('variant') == '抢枪':
        return '没等准星到身上就开枪'               # 审核单第 4 条
    if rid == 'TEAM-04' and f0.get('variant') == 'entry':
        return '进点后面的人没跟上'                 # 审核单第 28 条（团队）
    if rid == 'TEAM-05' and f0.get('variant') == 'entry_follow':
        return '队友进点你没跟上'
    if rid == 'POS-05' and f0.get('variant') == 'line':
        return '挪位置时走进了对面的枪线'           # 审核单第 36 条
    if rid == 'POS-05' and f0.get('variant') == 'chase':
        return '人多时跑出去补人'                   # Q13
    if rid == 'POST-01' and br == '拉响枪角度':
        return '去拉刚响过枪的角度'                 # Q11
    if rid == 'POST-03' and (br == '拦拆' or M.blame_parts((h or {}).get('blame'))[0] == '合理代价'):
        return '下包后去拦拆包'                     # Q18：按意图算合理
    if rid == 'TIME-08' and f0.get('variant') == 'vip_wait':
        return '比 VIP 烟先上中远'                  # Q16
    if rid == 'UTIL-G3' and f0.get('trigger') == 'retake':
        return '回防先用道具清点'                   # 审核单第 27 条
    if rid == 'POS-06' and br == '弱守':
        return '一个人守包点'
    if rid == 'POS-04' and ((h or {}).get('facts') or {}).get('still_dueling'):
        return '对枪时被另一个人打死'
    if rid == 'SHOT-G1':
        v = str(((h or {}).get('facts') or {}).get('variant') or '')
        if v and v != '首枪中':
            return '被先打中后站住反杀' if '反杀' in (h or {}).get('text', '') else '停住修准星拿下'
    f = (h or {}).get('facts') or {}
    if rid == 'POS-01' and f.get('event') == 'miss':
        return '狙空后没收'                       # 狙空不是杀人（8d R8、635 R25）；站位句式的狙空版也是这一件事
    if rid == 'POS-01' and br == 'spot':
        return '打人时站的位置两个人都打得到'     # decisions #10：杀完不到 0.8 秒，不算“没收”
    if rid == 'SHOT-02' and f.get('variant') == '侧身挨枪':
        return '扔道具时侧着身被先打中'           # bd R9：输的是侧着身扔烟，不是准星高低
    if rid == 'TIME-05' and f.get('near'):
        return '听到枪声还抢在队友前面冲'             # 附近有队友时不写“一个人冲”（8d R18）
    if rid == 'TEAM-04' and f.get('near'):
        return '比附近的队友先露头、没人看得到对手'   # 附近其实有队友时不写“身后没人”（e6b R7/R9/R12、128 R1/R4/R23）
    if rid == 'POS-01' and f.get('event') == 'miss':
        return '狙空后没收'                       # 狙空不是杀人（8d R8、635 R25）
    if rid == 'SHOT-G2' and f.get('variant') == '先挨枪反杀':
        return '先挨一枪后停住开镜反杀'           # 先挨了枪就不是“先手”（8d R12）
    if rid == 'SHOT-G2' and f.get('variant') == '空枪后没走开':
        # 不夸“空了一枪还站着”：和“狙空枪”的“一枪不中先收回掩体”并排读着矛盾（8d R9 35.2）；他照样是先打中的一方
        return 'AWP 停稳开镜先手'
    if rid == 'POS-G1':
        # 两个分支说的不是一件事：被打残后退出视线 ≠ 拿到击杀后马上收（635 高分玩家45 R4 标题写成“拿到击杀后马上收”）
        return '被打残后马上退出视线' if f.get('sub') == '残血' else '残局拿到击杀后马上收'
    if rid == 'UTIL-G3':
        # 触发是掉血还是拿到人：两种局面说法不一样（e6b R18：被雷炸掉一截血，标题写成“占优后”）
        if f.get('trigger') == 'hurt':
            return '掉血后用道具拖时间'
        return '拿到人后先用道具拖住' if '再去打下一个' in ((h or {}).get('text') or '') else '占优后用道具拖时间'
    if rid == 'POST-01' and M.is_good(h or {}):
        return '人数劣势保下了枪'                 # 好的分支（635 R5：AK 保了下来）
    if rid == 'SHOT-03' and f.get('far'):
        return '远处手枪打空'
    if rid == 'SHOT-03' and f.get('paused'):
        return '停下重新对准后还是没中'           # 句子写了“中间停下来重新对准过”（8d R1 31.1）
    return PHRASE.get(rid) or M.rule_name(rid)


# 标签：规则名里带判语（该……、却……）的换成中性说法（用户规则：买法不说对错，只给另一种思路）
TAG_NAME = {'ECO-05': '装备差距输掉', 'TIME-04': '进点太晚', 'SHOT-05': '没停住就被先打中', 'ECO-02': '这回合也能强起', 'UTIL-01': '道具留在身上就出去接枪', 'TIME-05': '听到枪声还一个人冲', 'ECO-03': '强起输后的买法', 'POST-01': '人数劣势没保枪', 'POS-01': '杀完人没收',
            'POST-02': '回防时身后的人补不上', 'POST-03': '挪位置时走进了对面的枪线'}   # 第 36 条改名（复核修正：标签原来还用 rules_v1 旧名）


def _you(p):
    """“你”接短语：短语以英文开头时加空格"""
    return ('你 ' if p[:1].isascii() else '你') + p


def _place(raw):
    """报点原文 → 中文；没收录的返回空（句子里就不写地点）"""
    cn = C.place_cn(raw, default='')
    return cn or ''


def _at(raw):
    p = _place(raw)
    return f'在{p}' if p else ''


def _of(raw):
    p = _place(raw)
    return f'{p}的 ' if p else ''


def _gun(w):
    return C.weapon_cn(w, default=str(w or ''))


# ---------------------------------------------------------------- 标题
def team02_phrase(h, e=None):
    """TEAM-02（输的回合人数先丢在谁身上）→ 标题短语；人数取句子里第一个 NvM"""
    why = str((h.get('facts') or {}).get('why') or '')
    t = h.get('text') or ''
    m = re.search(r'([0-5])v([0-5])', t)
    if why == 'clutch' and m:
        return f'队友先后倒下，留你打 {m.group(0)}'
    if why == 'close':
        return '人数没拉开，残局输了'
    if why == 'eco':
        tb = THEM_BUY_WORD.get(((e or {}).get('them') or {}).get('buy'), '长枪')
        return f'eco 打对面{tb}，装备差太多'
    if why == 'deficit' and m:
        f = h.get('facts') or {}
        if f.get('user_died_before_segment'):
            n = len(f.get('lost') or [])
            nw = {1: '一', 2: '两', 3: '三', 4: '四'}.get(n, str(n))
            nv = f"{f.get('us')}v{f.get('them')}"
            return f'扳平后又连丢{nw}人，打成 {nv}' if n > 1 else f'扳平后又丢一人，打成 {nv}'
        who = '队友' if not (h.get('blame') or '').startswith('你') else ''
        return f'{who}先倒下，打成 {m.group(0)}' if who else f'先丢人打成 {m.group(0)}'
    return ''


def eco07_title(h):
    """ECO-07 的标题：和总结里的同一个写法"""
    return SUM.eco07_title(h)


def eco08_title(h):
    return '满起下了包被拆' if '被拆' in (h.get('text') or '') else '满起下了包被打光'


def _awp_carry(h):
    """ECO-06 的“AWP 被对面捡走”一支：只进经济栏，不是“反 eco 被翻”"""
    return ((h or {}).get('facts') or {}).get('branch') == 'awp_carry'


def _swing_only(h):
    """只进这一回合经济栏的势头：AWP 被对面捡走（awp_carry）、跨回合经济链（chain，第六轮拍板 5）；
    不进标题、走势、标签"""
    return ((h or {}).get('facts') or {}).get('branch') in ('awp_carry', 'chain')


def round_headline(rn, mg, by, tl_r, e=None):
    """标题优先级（第一条命中的取来）：
    团队/经济一面：ECO-06 > ECO-08 > ECO-07 > TEAM-07 > TEAM-03 > TEAM-01（你）> ECO-05 > TEAM-02 > TEAM-01（队友）
    你这一面：阵亡主因 > 你的责任的问题 > 关键的好操作 > 第一条好操作 > 回合级的好操作；
             你的部分是“打得好”时好的一面排在问题前面
    都没有：阵营 + 结果；前面加回合类型（手枪局 / eco / 强起 / 反 eco）"""
    team = ''
    e07 = [h for h in by.get('ECO-07', []) if not _swing_only(h)]
    if any(not _swing_only(h) for h in by.get('ECO-06', [])):
        team = '反 eco 被翻'
    elif 'ECO-08' in by:
        team = eco08_title(by['ECO-08'][0])
    elif e07:
        team = eco07_title(e07[0])
    elif 'TEAM-07' in by:
        h = by['TEAM-07'][0]
        team = '你反 eco 单走送枪' if h.get('is_user') else f"{h.get('subject') or '队友'} 反 eco 单走送枪"
    elif 'TIME-04' in by and tl_r.get('result') != '赢':
        team = phrase('TIME-04', by['TIME-04'][0])        # 主要原因是全队拖到太晚才打（8d R17）
    elif 'TEAM-03' in by and tl_r.get('result') != '赢' and any(h.get('kind') != '思路' for h in by['TEAM-03']):
        f3 = next(h for h in by['TEAM-03'] if h.get('kind') != '思路').get('facts') or {}
        k = f3.get('killer')
        if f3.get('burst') and isinstance(k, str) and k and f3.get('n'):
            team = f"被 {k} 冲出来连杀 {f3['n']} 个"      # 几秒里冲出来连杀，不是“逐个打掉”（8d R16 对手4 3.6 秒连杀 3 个）
        else:
            # 这回合不止一个人连杀（bd R13：对手2（第4场） 打掉 2 个、对手3（第4场） 打掉 3 个）：不写“一个人”，两个人都写上
            ks = sorted({(h.get('facts') or {}).get('killer') for h in by['TEAM-03'] if h.get('kind') != '思路'}
                        - {None, ''}, key=lambda x: (x != k, x))
            if len(ks) > 1:
                team = f"被 {'、'.join(ks)} 各自逐个打掉"
            else:
                team = f'被 {k} 一个人逐个打掉' if isinstance(k, str) and k else '被同一个人逐个打掉'
    elif 'TEAM-01' in by and M.team01_who(by['TEAM-01'][0]) == '你':
        team = '这回合是你打出来的'
    elif 'TEAM-01' in by and M.team01_who(by['TEAM-01'][0]) == '你和队友' and tl_r.get('result') == '赢':
        # 赢下的回合、功劳是你和队友：标题先写这一面，不然只剩“你狙空枪”这种问题短语（128 R4/R11/R22）
        f01 = by['TEAM-01'][0].get('facts') or {}
        ms = [m for m in (f01.get('ties') or f01.get('mates') or []) if isinstance(m, str) and m] or ([f01['mate']] if f01.get('mate') else [])
        team = f"你和 {'、'.join(ms)} 一起打出人数" if ms else '你和队友一起打出人数'
    elif 'ECO-05' in by:
        team = 'eco 打全起，输在装备差距'
    elif 'TEAM-02' in by:
        team = team02_phrase(by['TEAM-02'][0], e)
    elif 'TEAM-01' in by:
        f01 = by['TEAM-01'][0].get('facts') or {}
        mate = f01.get('mate')
        ms = [m for m in (f01.get('mates') or []) if isinstance(m, str) and m] or ([mate] if mate else [])
        # 标题和主要原因写同一批人（8d R1：主要原因“队友B、队友A”，标题只写了 队友B）
        team = f"主要靠 {'、'.join(ms)} 打出人数" if M.team01_who(by['TEAM-01'][0]) == '队友' and ms else ''
    units = mg['units']
    mine = ''
    # 这次死判了合理代价：标题“你”这一面不拿它当你的问题写（e6b R21、8d R15/R16：主句判合理代价，标题却写成你的缺点）
    cost_death = [u for u in units if u['death'] and u['mblame'] == '合理代价']
    bad = ([u for u in units if u['death'] and u['mblame'] != '合理代价']
           + [u for u in units if not u['death'] and not M.is_good(u['hit']) and u['hit'].get('is_user') and u['contrib'] == 'major'])
    # 只是记一笔的好操作（ECO-G1 捡了枪没用它打出击杀）不上标题
    good = ([u for u in units if M.is_good(u['hit']) and u['key'] and u['hit'].get('is_user') and not M.is_neutral(u['hit'])]
            + [u for u in units if M.is_good(u['hit']) and u['hit'].get('is_user') and not M.is_neutral(u['hit'])])
    # “停稳先手”这类夸奖和同一回合“第一枪没停稳 / 没停住就被先打中”同一类枪并排读着矛盾：标题不用这条夸奖（635 R1）
    vis = visible_hits(mg)
    good = [u for u in good if _still_conflict(u['hit'], vis) != 'drop']
    # 你的部分是“打得好”时先写好的一面，否则先写问题
    order = good + bad if mg['your_part']['level'] == '打得好' else bad + good
    rgood = [h for h in mg['round_hits'] if h.get('is_user') and M.is_good(h) and not M.is_neutral(h)]
    # 团队那一面已经用了的规则（例如 TEAM-03）不在“你”这一面再说一遍
    order = [u for u in order if not (team and u['hit']['rule'] in ('TEAM-03', 'TEAM-07', 'TEAM-01', 'TEAM-02'))]
    if cost_death and mg['your_part']['level'] == '小问题' and not (order and any(order[0] is b for b in bad)):
        # 你的部分是小问题：标题不写“你的阵亡算合理代价”，写那个小问题（8d R15）
        minor = [u for u in units if not u['death'] and not M.is_good(u['hit']) and u['hit'].get('is_user')
                 and u['contrib'] == 'minor']
        order = minor + order
    if cost_death and mg['your_part']['level'] not in ('打得好', '小问题', '主要问题')             and not (order and any(order[0] is b for b in bad)):
        mine = '你的阵亡算合理代价'
    elif order:
        oh = order[0]['hit']
        ph = phrase(oh['rule'], oh)
        if _still_conflict(oh, vis) == 'prefix':
            ph = _with_gun([str((oh.get('facts') or {}).get('weapon') or '')], ph)
        mine = _you(ph)
    elif rgood:
        mine = _you(phrase(rgood[0]['rule'], rgood[0]))
    # 第六轮拍板 6：你主导赢下的回合（TEAM-01 功劳是你）标题先写好的一面，问题用“不过……”接在后面
    carried = tl_r.get('result') == '赢' and 'TEAM-01' in by and M.team01_who(by['TEAM-01'][0]) == '你'
    if carried:
        minor_u = [u for u in units if not u['death'] and not M.is_good(u['hit']) and u['hit'].get('is_user')
                   and u['mblame'] in M.YOUR_BLAMES]
        bads = [u for u in bad + minor_u if not (team and u['hit']['rule'] in ('TEAM-03', 'TEAM-07', 'TEAM-01', 'TEAM-02'))]
        # 好的一面取关键的、夸击杀本身的那一条（R5：先写补枪那一杀，不写捡枪）
        g = next(iter(sorted(good, key=lambda u: (not u['key'], u['hit']['rule'] not in M.KILL_GOOD, M.hit_key(u['hit'])))), None)
        good_ph = _you(phrase(g['hit']['rule'], g['hit'])) if g else ''
        if not good_ph and team != '这回合是你打出来的':
            good_ph = '这回合是你打出来的'
        bad_ph = ''
        if bads:
            bh = bads[0]['hit']
            bad_ph = phrase(bh['rule'], bh)
            if _still_conflict(bh, vis) == 'prefix':
                bad_ph = _with_gun([str((bh.get('facts') or {}).get('weapon') or '')], bad_ph)
            bad_ph = '不过' + (bad_ph if good_ph.startswith('你') else _you(bad_ph))
        if good_ph:
            mine = good_ph + ('，' + bad_ph if bad_ph else '')
        elif bad_ph:
            team, mine = (team + '，' + bad_ph) if team else bad_ph, ''
        else:
            mine = ''
    parts = [p for p in (team, mine) if p]
    if not parts:
        res = '赢下' if tl_r['result'] == '赢' else '输掉'
        rc = reason_cn(tl_r)
        parts = [f"{tl_r['side']} 方{res}" + (f"（{rc}）" if rc else '')]
    lab = econ_label(e)
    lab = lab if lab in HEAD_LABELS else ''
    head = '；'.join(parts)
    if lab and head.startswith(lab):
        lab = ''                                       # “eco：eco 赢下……”不重复回合类型
    return sp((f'{lab}：' if lab else '') + head)


# ---------------------------------------------------------------- 买法一句
def _team_kit(side, user):
    """economy 一方 → '谁拿什么'：同一把枪合在一起，你写“你”"""
    groups = {}
    for p in sorted(side.get('players') or [], key=lambda p: (not p.get('user'), p['name'])):
        g = _gun(p.get('primary') or p.get('pistol') or '')
        who = '你' if p.get('user') or p['name'] == user else p['name']
        groups.setdefault(g, []).append(who)
    keys = sorted(groups, key=lambda g: (-len(groups[g]), '你' not in groups[g], g))
    return '、'.join(f"{g}（{'、'.join(groups[g])}）" for g in keys if g)


def _guns_cn(g):
    """economy.json 对面的枪（'AK-47×3 + AWP + 只拿手枪的：Dual Berettas'）→ 句子里的武器名
    （AK×3 + AWP + 只有手枪（双枪））；换名后同名的合在一起"""
    def one(parts):
        cnt, order = {}, []
        for x in parts:
            m = re.match(r'^(.+?)(?:×(\d+))?$', x.strip())
            if not m:
                continue
            n = _gun(m.group(1)) if C.is_gun(m.group(1)) else m.group(1)
            if n not in cnt:
                order.append(n)
                cnt[n] = 0
            cnt[n] += int(m.group(2) or 1)
        return [f'{n}×{cnt[n]}' if cnt[n] > 1 else n for n in order]
    g = g or ''
    pistols = ''
    if '只拿手枪的：' in g:
        g, pistols = g.split('只拿手枪的：', 1)
    main = one([x for x in g.split(' + ') if x.strip()])
    if pistols:
        # 全员都只拿手枪时写“全员手枪”，不然“对面半起：只有手枪（……）”读着像没起（e6b R16）
        main.append(('全员手枪（' if not main else '只有手枪（') + '、'.join(one(pistols.split('、'))) + '）')
    return ' + '.join(main)


def situation(e, tl_r, user):
    if not e:
        return f"开局比分 {tl_r.get('score_before')}。"
    us, th = e['us'], e['them']
    s = f"开局比分 {tl_r.get('score_before')}。我方{'反 eco' if _anti_eco_buy(e) else us.get('buy')}：{_team_kit(us, user)}"
    if us.get('n_nades'):
        s += f"，道具 {us['n_nades']} 颗"
    g0 = th.get('guns') or ''
    guns = _guns_cn(g0) if g0 and g0 != '全员默认手枪' else g0
    s += f"；对面{th.get('buy')}：{guns}"
    if th.get('n_nades'):
        s += f"，道具 {th['n_nades']} 颗"
    return sp(s + '。')


# ---------------------------------------------------------------- 时间线
def story(tl_r, user, names, blind_th=None, tickrate=64):
    """按时间排的一句一事（t 是数字，页面显示 “t s”）。
    blind_th = (队友白屏秒数, 敌人白屏秒数)：闪的“白了队友 / 闪到”只写达到这个秒数的人，和 UTIL-02 同一口径
    （否则时间线写“白了 4 个队友”、UTIL-02 的时刻只写 1 个，128 R18）"""
    ev = []    # (t, 顺序, 文字)
    alive = {}
    for a in tl_r.get('alive') or []:
        alive.setdefault(a.get('event'), (a['us'], a['them']))
    # 只写两个人都是本场玩家的击杀（炸弹、摔死这类 killer=world 的不写）
    kills = [k for k in tl_r.get('kills') or [] if not k.get('post_round') and k.get('killer') in names
             and k.get('victim') in names]
    # 击杀的秒数按 tick 和规则句子用同一个取整（C.sec 四舍五入），不然同一次击杀时间线写 16.2、主要原因写 16.3（8d R6）
    rs = (tl_r.get('key_ticks') or {}).get('round_start')
    if isinstance(rs, int):
        kills = [dict(k, t=C.sec((k['tick'] - rs) / tickrate)) if isinstance(k.get('tick'), int) else k for k in kills]
    first = next((k for k in kills if not k.get('teamkill') and k['killer'] != k['victim']), None)
    kidx = {id(k): i for i, k in enumerate(kills)}

    def _ord(k):
        # 同一秒、同一 tick 的两次击杀按击杀栏的先后（人数尾巴也是按这个顺序算的，8d R3 36.1）
        return k['tick'] * 1000 + kidx[id(k)] if isinstance(k.get('tick'), int) else None

    for k in kills:
        if k.get('teamkill'):
            continue
        st = alive.get(f"{k['killer']} → {k['victim']}")
        tail = f'，{C.alive_str(*st)}' if st else ''
        w = _gun(k.get('weapon'))
        smoke = '隔着烟' if k.get('thrusmoke') else ''
        if k['killer'] == k['victim']:
            # 自己死的（摔死 / 自杀，击杀栏 killer 是他自己）：不写成“被超市的 X 打死”（10ce R4、R10）
            ev.append((k['t'], 1 if k['victim'] == user else 2,
                       f"{'' if k['victim'] == user else k['victim']}{_at(k.get('victim_place'))}自己死了" + tail, _ord(k)))
        elif k['killer'] == user:
            ev.append((k['t'], 1, f"{_at(k.get('killer_place'))}{smoke}用{w}打死{_of(k.get('victim_place'))}{k['victim']}"
                       + ('，爆头' if k.get('headshot') else '') + tail, _ord(k)))
        elif k['victim'] == user:
            ev.append((k['t'], 1, f"{_at(k.get('victim_place'))}被{_of(k.get('killer_place'))}{k['killer']}{smoke}用{w}打死" + tail,
                       _ord(k)))
        elif k.get('victim_is_mate'):
            ev.append((k['t'], 2, f"{k['victim']}{_at(k.get('victim_place'))}被{_of(k.get('killer_place'))}{k['killer']}{smoke}打死" + tail,
                       _ord(k)))
        elif k is first or k.get('trade_kill'):
            what = '首杀' if k is first else '补枪'
            ev.append((k['t'], 3, f"{k['killer']}{_at(k.get('killer_place'))}{what}，打死{_of(k.get('victim_place'))}{k['victim']}" + tail,
                       _ord(k)))
        elif k.get('killer_side') == tl_r.get('side'):
            # 队友的其他击杀也写上，不然时间线里的人数会跳（635 R1：3v2 直接到 3v0）
            ev.append((k['t'], 3, f"{k['killer']}{_at(k.get('killer_place'))}打死{_of(k.get('victim_place'))}{k['victim']}" + tail,
                       _ord(k)))
    for u in tl_r.get('user_util') or []:
        kind = NADE_CN.get(u.get('kind'), '道具')
        tp, lp = _place(u.get('throw_place')), _place(u.get('land_place'))
        # 扔的地方和落点是同一个报点：“在 B 小巷往 B 小巷扔闪”读着重复，只写一次
        s = f"{_at(u.get('throw_place'))}扔{kind}" if tp and tp == lp else f"{_at(u.get('throw_place'))}往{lp or '前面'}扔{kind}"
        hurt = [d['name'] for d in u.get('damage') or [] if d.get('enemy')]
        if hurt:
            s += ('，烧到 ' if kind == '火' else '，炸到 ') + '、'.join(hurt)
        bl = u.get('blinded') or []
        mate_s, enemy_s = blind_th if blind_th else (0.0, 0.0)

        def _long(b, need):
            d = b.get('dur_raw', b.get('dur'))         # 和 UTIL-02 一样用没取整的白屏秒数
            return not need or (isinstance(d, (int, float)) and d >= need)
        be = [b['name'] for b in bl if b.get('enemy') and _long(b, enemy_s)]
        bm = [b['name'] for b in bl if not b.get('enemy') and b.get('name') != user and _long(b, mate_s)]
        if be:
            s += '，闪到 ' + '、'.join(be)
        if bm:
            s += '，白了队友 ' + '、'.join(bm)
        ev.append((u['t'], 4, s))
    pl = tl_r.get('plant')
    if pl:
        who = '你' if pl.get('by_user') or pl.get('planter') == user else pl.get('planter')
        site = pl.get('site') if pl.get('site') in ('A', 'B') else ''
        ev.append((pl['t'], 5, f"{who}在 {site} 点下包" if site else f"{who}下了包"))
    df = tl_r.get('defuse')
    if df:
        who = '你' if df.get('by_user') or df.get('defuser') == user else df.get('defuser')
        ev.append((df['t'], 5, f"{who}拆掉了包"))
    # 同一秒里按 tick 排（击杀先后），没有 tick 的事件排在同一秒的击杀后面
    ev = [x if len(x) == 4 else x + (None,) for x in ev]
    ev.sort(key=lambda x: (x[0], x[3] if isinstance(x[3], int) else 10 ** 12, x[1], x[2]))
    return [{'t': M._sec(t), 'text': sp(s)} for t, _, s, _ in ev]


def team_text(tl_r, mg, by, user, tickrate=64):
    """TEAM-01/02/03 的句子（主要原因已经写了的不再重复）；没有时列队友的击杀"""
    used = mg['main_cause']['text']
    parts = []
    killers = set()
    ch = mg.get('cause_hit')
    if ch is not None and (ch.get('facts') or {}).get('killer'):
        killers.add(ch['facts']['killer'])
    for rid in ('TEAM-01', 'TEAM-02', 'TEAM-03'):
        for h in by.get(rid, []):
            t = sp(M._part(h['text']))
            k = (h.get('facts') or {}).get('killer') if rid in ('TEAM-02', 'TEAM-03') else None
            if k and k in killers:
                continue                               # 同一个人的同一串连杀只写一次（8d R16 对手4 写了两遍）
            if rid == 'TEAM-02' and mg['main_cause'].get('who') == '经济/装备差距' and (
                    M.blame_parts(h.get('blame'))[0] == '合理代价' or str((h.get('facts') or {}).get('why')) == 'eco'):
                continue                               # 主要原因已经写了装备差距，“eco 打对面长枪”再写一遍（8d R18）
            if t and t not in used and t not in parts:      # 主要原因里已经整句写了（TIME-04 后面接的 TEAM-02，8d R17）
                parts.append(t)
                if k:
                    killers.add(k)
    if parts:
        return '；'.join(parts) + '。'
    ks = [k for k in tl_r.get('kills') or [] if not k.get('post_round') and not k.get('teamkill')
          and k['killer'] != user and k['killer'] != k['victim'] and k.get('killer_side') == tl_r['side']]
    if not ks:
        return '队友这回合没有击杀。'
    # 秒数按 tick 取整，和时间线、规则句子同一个秒（8d R6：队友栏 13.2、时间线 13.3）
    rs = (tl_r.get('key_ticks') or {}).get('round_start')

    def ksec(k):
        if isinstance(rs, int) and isinstance(k.get('tick'), int):
            return M._sec((k['tick'] - rs) / tickrate)
        return M._sec(k['t'])
    return sp('；'.join(f"{ksec(k)} 秒 {k['killer']}{_at(k.get('killer_place'))}打死 {k['victim']}" for k in ks[:5]) + '。')


def economy(by):
    """经济栏：idea 只来自 ECO-01..04（另一种思路），swing 只来自 ECO-06/07/08（对后面回合的影响）"""
    ideas, swings = [], []
    for rid in ('ECO-01', 'ECO-02', 'ECO-03', 'ECO-04'):
        hs = by.get(rid, [])
        # 有专门给经济栏的那一条（facts.slot = economy.idea）时只用它：时刻那条的“还可以”已经写在时刻里，不再抄一遍（8d R16）
        slot = [h for h in hs if (h.get('facts') or {}).get('slot') == 'economy.idea']
        n0 = len(ideas)
        # 专门给经济栏的那条先用；给总结用的（facts.slot = summary.*，“长枪局常拿冲锋枪”）不进这一回合的经济栏
        hs = sorted(hs, key=lambda h: (((h.get('facts') or {}).get('slot') != 'economy.idea'), M.hit_key(h)))
        for h in hs:
            if len(ideas) > n0:
                break           # 同一条买法规则一回合只写一个思路（8d R4：“能多 1 把步枪”后面又一句“钱够的都起步枪”）
            f = h.get('facts') or {}
            if str(f.get('slot') or '').startswith('summary'):
                continue
            if isinstance(f.get('idea'), str) and f['idea']:
                s = f['idea']
            elif re.match(r'^(?:买法)?另一种思路', h.get('text') or ''):
                s = M._part(h['text'])
            elif slot and h.get('scope') == 'moment':
                continue        # 这条时刻的“还可以”已经写在时刻里；经济栏用专门那条（facts.slot = economy.idea）
            else:
                s = M.better_text(h.get('alt'))
            # 每条只要内容，开头的“买法另一种思路：/另一种思路是/还可以”去掉，最后统一加一次
            s = re.sub(r'^(?:买法)?另一种思路(?:是|：|:)?|^还可以[：:，,]?', '', sp(M._part(s))).strip()
            if s and s not in ideas:
                ideas.append(s)
    # 跨回合经济链（上回合怎么打完 → 这回合的局面）先写，再写这回合对后面的影响（第六轮拍板 5）
    chain = [h for rid in ('ECO-06', 'ECO-07') for h in by.get(rid, []) if ((h.get('facts') or {}).get('branch')) == 'chain']
    for h in sorted(chain, key=M.hit_key) + [h for rid in ('ECO-06', 'ECO-07', 'ECO-08') for h in by.get(rid, [])
                                              if ((h.get('facts') or {}).get('branch')) != 'chain']:
        s = sp(M._part(h.get('text')))
        if s and s not in swings:
            swings.append(s)
    idea = ('买法另一种思路：' + '；也可以'.join(ideas)) if ideas else ''
    return {'idea': sp(idea), 'swing': '；'.join(swings)}


def rating(mg, won):
    """固定映射：你的部分主要问题 → 有问题；打得好，或赢了且主要原因是你 → 打得好；
    输了且主要原因是装备差距/合理代价 → 合理代价；其余 → 正常"""
    lv = mg['your_part']['level']
    if lv in RATING_OF_LEVEL:
        return RATING_OF_LEVEL[lv]
    if won and mg['main_cause']['who'] == '你' and lv not in ('小问题', '主要问题'):
        return '打得好'
    ch = mg.get('cause_hit')
    if not won and (mg['main_cause']['who'] == '经济/装备差距'
                    or (ch is not None and M.blame_parts(ch.get('blame'))[0] == '合理代价')):
        return '合理代价'
    return '正常'


def visible_hits(mg):
    """页面上真的写出来的 hit：时刻的主句、从句（没被跳过的）和并进来的小问题；不含只给问题句子提供事实的好操作（facts_from）"""
    out = []
    for u in mg['units']:
        skip = u.get('skip') or ()
        out.append(u['hit'])
        # 被主句整条盖掉、页面上一个字没写的从句不算（8d R21：POS-05 并进 POST-03 后没写出来，标签还有“人多时主动接五五开的枪”）
        # UTIL-01 写了“出去接枪”时 TEAM-04 的从句不写（merge._subs），也不做标签（8d R3 / R19 / R22）
        drop04 = M.u01_out(u)
        out += [c for c in u['clauses'] if not any(c is x for x in skip) and M._covered(c, u['hit']) != ''
                and not (drop04 and c['rule'] == 'TEAM-04' and not M.is_entry(c))]
        out += u['annot']
    return out


def tag_name(rid, h):
    """一条 hit 的标签；None = 这条不出标签"""
    f = (h or {}).get('facts') or {}
    if rid in ('ECO-06', 'ECO-07') and ((h or {}).get('facts') or {}).get('branch') == 'chain':
        return None                                    # 跨回合经济链只写在经济栏
    if rid == 'ECO-06' and _awp_carry(h):
        return 'AWP 被对面捡走'
    if rid == 'POST-01' and not M.is_good(h or {}) and M.is_post01_background(h or {}):
        return None                                    # “半场最后一回合，去打是对的”这类豁免不是“没保枪”（8d R12）
    if rid == 'TEAM-06' and M.blame_parts(h.get('blame'))[0] == '队友或团队':
        return None                                    # 是队友走开的，不放进你的标签（8d R15）
    if rid == 'SHOT-06' and (f.get('duel') or {}).get('ok') is not False:
        return None                                    # 动作对 / 不判动作（两个人都打得到、被闪、隔烟）：不是你的问题标签
    if rid == 'POST-03' and M.blame_parts(h.get('blame'))[0] == '合理代价':
        return None                                    # 下包后去拦拆包：合理（Q18），不做问题标签
    if rid == 'TIME-08' and (h or {}).get('kind') == '思路':
        return None                                    # 只是另一种思路（Q16 等 VIP 烟），不做“开局一个人上已知的狙位”标签
    n = phrase(rid, h)
    if n != (PHRASE.get(rid) or M.rule_name(rid)):
        return n                                       # 特殊分支（弱守、站位、狙空、好的保枪……）
    return TAG_NAME.get(rid) or M.rule_name(rid)


# 两个人一起的局面（不是 subject 一个人的打法），标签不加名字
JOINT_TAG_RULES = ('POS-07', 'TEAM-03')


def mate_tag(n, h, mates):
    """标签说的是队友自己的打法（队友C 一个人守包点、队友B 同一条线重复拉）：前面加上他的名字，
    不然读着像是你的（8d R12）。标签里已经写了“队友”（被队友闪白、队友倒下没人补得上）、两个人一起的局面不加"""
    if not n or (h or {}).get('is_user'):
        return n
    who = (h or {}).get('subject')
    if not isinstance(who, str) or who not in mates or '队友' in n or (h or {}).get('rule') in JOINT_TAG_RULES:
        return n
    return sp(f'{who} {n}')


def _norm_page(s):
    return re.sub(r'\s+', '', s or '')


def round_hit_shown(h, mg, page):
    """回合层面的 hit（没有时刻）只有在页面上真写出来时才做标签（635 高分玩家45 R6：TIME-01 只剩 ideas 里的一条“还可以”，
    标签却写“转点路上开打了还静步”）。写出来 = 它是主要原因；买法（ECO-01..04）经济栏有“另一种思路”；
    ECO-05..08 进了经济栏 / 标题 / 主要原因；或者它的句子 / 标题短语出现在标题、主要原因、你的部分、队友栏、经济栏里。
    ideas 只写“还可以”，不算写出了这件事"""
    if page is None:
        return True
    if mg.get('cause_hit') is not None and h is mg['cause_hit']:
        return True
    eco = page.get('economy') or {}
    rid = h.get('rule') or ''
    if rid in M.BUY_RULES:
        return bool(eco.get('idea'))
    texts = [page.get('headline'), (page.get('main_cause') or {}).get('text'), (page.get('your_part') or {}).get('text'),
             page.get('team'), eco.get('idea'), eco.get('swing')]
    texts = [_norm_page(x) for x in texts if x]
    t = _norm_page(sp(M._part(h.get('text'))))
    if rid in ('ECO-06', 'ECO-07', 'ECO-08') and eco.get('swing'):
        return True
    if rid == 'ECO-05' and '装备差距' in (page.get('headline') or '') + ((page.get('main_cause') or {}).get('text') or ''):
        return True
    if t and any(t in x for x in texts):
        return True
    ph = _norm_page(phrase(rid, h))
    return bool(ph) and ph in _norm_page(page.get('headline'))


def tags(tl_r, mg, e=None, page=None, mates=()):
    """page：这一回合已经拼好的字段（标题、主要原因、你的部分、经济栏、队友栏），回合层面的 hit 要在这里面写出来才做标签"""
    out = []
    lab = econ_label(e)
    if lab:
        out.append(lab)
    vis = visible_hits(mg)
    rh_ids = {h['rule'] for h in mg['round_hits']}
    cand = []                                          # (先后, 序号, 名字)：你的责任的问题在前，其次别的问题，夸奖最后
    for rid in mg['used']:
        if rid in ('TEAM-01', 'TEAM-02', 'ECO-09', 'SUM-01', 'SUM-02', 'SUM-03', 'TIME-09') or rid not in M.rules()['by_id']:
            continue                                   # 归属类、全场类、rules_v1 里没有的编号都不做标签
        rh = [h for h in vis if h['rule'] == rid]
        if not rh:
            rh = [h for h in mg['round_hits'] if h['rule'] == rid and round_hit_shown(h, mg, page)] if rid in rh_ids else []
        if not rh:
            continue                                   # 只给别的句子提供事实、页面上没写出来的不做标签（8d R14）
        for h in rh:
            if rid == 'TEAM-04' and M.blame_parts(h.get('blame'))[0] == '合理代价' and not M.is_entry(h):
                continue                               # 人数已经差两个时一个人先接触算合理代价：不做成“你的问题”标签（8d R16）
            n = mate_tag(tag_name(rid, h), h, mates)
            if not n:
                continue
            if M.is_good(h):
                pr = 2
            else:
                pr = 0 if M.blame_parts(h.get('blame'))[0] in M.YOUR_BLAMES else 1
            cand.append((pr, len(cand), n))
    first_t = {}
    for h in vis + list(mg['round_hits']):
        n = mate_tag(tag_name(h['rule'], h), h, mates)
        if n and isinstance(h.get('t'), (int, float)):
            first_t.setdefault((h['rule'], n), h['t'])
    for _, _, n in sorted(cand):
        if n not in out:
            out.append(n)
    # “转点路上还静步”和“听到枪声还一个人冲”一慢一快，并排读着矛盾：都带上是第几秒（635 高分玩家50 R22）
    for pair in TAG_TIME_PAIRS:
        tn = {r: next((n for (rr, n), _ in sorted(first_t.items()) if rr == r and n in out), None) for r in pair}
        if all(tn.values()):
            for r in pair:
                t = next(v for (rr, n), v in sorted(first_t.items()) if rr == r and n == tn[r])
                out[out.index(tn[r])] = sp(f'第 {C.fmt_sec(t)} 秒' + tn[r])
    return _split_still_tags(out, vis)[:6]


TAG_TIME_PAIRS = [('TIME-01', 'TIME-05')]


def _wcls_cn(w):
    """句子里的武器名（AK、M4A1-S、P90…）→ 大类（步枪 / 冲锋枪 / 手枪 / 狙）"""
    for k, v in sorted(C._WEAPON_CN.items()):
        if v == w:
            return C.weapon_class(k)
    return C.weapon_class(w)


STILL_BAD = ('SHOT-01', 'SHOT-05', 'SHOT-03')        # “没停稳 / 没停住 / 一串打空”
STILL_GOOD = ('SHOT-G1', 'SHOT-G2')
DUEL_STILL_KEYS = ('no_stop', 'peek_into_holder', 'awp', 'awp_run')   # 复核修正：SHOT-06 自己成时刻的“没停稳 / 没停住”也算                 # “停稳先手 / 停住修准星 / 停稳开镜”


def _wcls_of(h):
    return _wcls_cn(str((h.get('facts') or {}).get('weapon') or ''))


def _still_conflict(h, hs):
    """一条“停稳……拿下”的夸奖和同一回合页面上的“没停稳 / 没停住 / 一串打空”放一起：
    同一类枪 → 'drop'（只留问题）；不同类枪 → 'prefix'（两边都带枪名）；不冲突 → None"""
    if h.get('rule') not in STILL_GOOD:
        return None
    bad = [x for x in hs if (x['rule'] in STILL_BAD[:2] or (x['rule'] == 'SHOT-06' and str(((x.get('facts') or {}).get('duel') or {})
                                                                                      .get('key')) in DUEL_STILL_KEYS)) and x.get('is_user')]
    if not bad:
        return None
    cg, cb = _wcls_of(h), {_wcls_of(x) for x in bad}
    if cg is None or None in cb or cg in cb:
        return 'drop'
    return 'prefix'


def _split_still_tags(out, hs):
    """用户规则：“第一枪没停稳就开 / 没停住就被先打中”和“停稳……拿下”不能并排。
    同一类枪：只留问题那一条；不同类枪：两边都带上枪名分开写（例如 AK 第一枪没停稳就开、P90 停住修准星拿下）"""
    bad_h = [h for h in hs if h['rule'] in STILL_BAD
             or (h['rule'] == 'SHOT-06' and str(((h.get('facts') or {}).get('duel') or {}).get('key')) in DUEL_STILL_KEYS)]
    good_h = [h for h in hs if h['rule'] in STILL_GOOD]
    if not bad_h or not good_h:
        return out
    bad_names = {tag_name(h['rule'], h) for h in bad_h} - {None}
    good_names = {tag_name(h['rule'], h) for h in good_h} | {M.rule_name(r) for r in STILL_GOOD}
    gi = [i for i, n in enumerate(out) if n in good_names]
    bi = [i for i, n in enumerate(out) if n in bad_names]
    if not gi or not bi:
        return out
    w_bad = sorted({str((h.get('facts') or {}).get('weapon') or '') for h in bad_h} - {''})
    w_good = sorted({str((h.get('facts') or {}).get('weapon') or '') for h in good_h} - {''})
    c_bad, c_good = {_wcls_cn(w) for w in w_bad}, {_wcls_cn(w) for w in w_good}
    if not w_bad or not w_good or c_bad & c_good or None in c_bad | c_good:
        return [n for i, n in enumerate(out) if i not in gi]
    res = list(out)
    for i in bi:
        res[i] = _with_gun(w_bad, out[i])
    for i in gi:
        res[i] = _with_gun(w_good, out[i])
    return res


def _with_gun(ws, name):
    """标签前面加枪名；已经以这把枪开头（“AWP 停稳开镜先手”）的不再加"""
    ws = [w for w in ws if not name.startswith(w)]
    return sp('、'.join(ws) + ' ' + name) if ws else name


# ---------------------------------------------------------------- 整份
def ot_prefix(rn, hr_ot):
    """加时的回合在“局面”前面写“加时第 N 回合，”（bd：12:12 进加时，原来页面上一个“加时”都没有）"""
    hr, _ = hr_ot
    return f'加时第 {rn - 2 * hr} 回合，' if rn > 2 * hr else ''


def match_point(rn, tl_r, hr_ot):
    """赛点回合：标题前面写“我方赛点 / 对面赛点 / 双方赛点，”（常规先到 hr+1，加时每一轮先到 hr + ohr×k + 1）"""
    hr, ohr = hr_ot
    try:
        a, b = (int(x) for x in str(tl_r.get('score_before')).split(':'))
    except ValueError:
        return ''
    if rn <= 2 * hr:
        target = hr + 1
    else:
        k = (rn - 2 * hr - 1) // (2 * ohr)
        target = hr + ohr * (k + 1) + 1
    mine, theirs = a == target - 1, b == target - 1
    if mine and theirs:
        return '双方赛点，'
    return '我方赛点，' if mine else ('对面赛点，' if theirs else '')


def load_hits(work):
    p = Path(work) / 'hits.json'
    if not p.exists():
        return []
    with open(p, encoding='utf-8') as f:
        d = json.load(f)
    return d.get('hits', []) if isinstance(d, dict) else d


def build_review(ctx, hits):
    """hits（规则命中列表）→ review dict。只读 ctx，不写文件"""
    tl = ctx.json('timeline.json')
    econ = ctx.json('economy.json')
    meta = tl['meta']
    user = meta['user']
    names = sorted({user} | set(meta.get('teammates') or []) | set(meta.get('enemies') or []))
    enemies = sorted(meta.get('enemies') or [])
    erounds = {e['round']: e for e in econ.get('rounds', [])}
    hits = sorted((M.norm_hit(h) for h in hits), key=M.hit_key)
    by_round = {}
    for h in hits:
        # scope=match 但带回合号的（ECO-06/07/08 势头）也给那一回合的经济栏和标题用
        if isinstance(h.get('round'), int) and h['round'] >= 1:
            by_round.setdefault(h['round'], []).append(h)
    rounds, merged, issues = [], {}, []
    hr_ot = config.halves_th(getattr(ctx, 'th', None))
    uth = (ctx.th or {}).get('util', {}) if hasattr(ctx, 'th') else {}
    blind_th = (float(uth.get('u02_mate_blind_s', 0) or 0), float(uth.get('u02_enemy_blind_s', 0) or 0))
    sal = dict((getattr(ctx, 'th', None) or {}).get('salience') or {})
    tr = meta.get('tickrate', 64)

    def _info(tl_r, salience):
        rn = tl_r['round']
        rs = (tl_r.get('key_ticks') or {}).get('round_start')
        kills = [dict(k, t=C.sec((k['tick'] - rs) / tr)) if isinstance(rs, int) and isinstance(k.get('tick'), int) else k
                 for k in tl_r.get('kills') or []]
        return {'round': rn, 'won': tl_r['result'] == '赢', 'user': user, 'names': names, 'enemies': enemies,
                'reason_cn': reason_cn(tl_r), 'round_start_tick': rs,
                'tickrate': tr, 'user_died': bool(tl_r.get('user_death')),
                'user_death': tl_r.get('user_death') if isinstance(tl_r.get('user_death'), dict) else None,
                'kills': kills, 'alive': tl_r.get('alive') or [],
                'buy': ((erounds.get(rn) or {}).get('us') or {}).get('buy'),
                'salience': salience}
    # 第六轮拍板 2：时刻只写值得分析的点。总结（缺点 / 强项）用没筛之前的单元算（它读的是全部命中），
    # 再把总结引用到的回合 / 规则交给筛选保住，页面上点进去一定看得到
    merged0 = {}
    for tl_r in sorted(tl['rounds'], key=lambda r: r['round']):
        merged0[tl_r['round']] = M.merge_round(by_round.get(tl_r['round'], []), _info(tl_r, dict(sal, enabled=False)))
    cited = {}
    summary = SUM.build(ctx, hits, None, merged0, cited=cited)
    for tl_r in sorted(tl['rounds'], key=lambda r: r['round']):
        rn = tl_r['round']
        rh = by_round.get(rn, [])
        info = _info(tl_r, sal)
        info['protect'] = sorted(cited.get(rn) or ())
        mg = M.merge_round(rh, info)
        mg['n_before'] = len(merged0[rn]['moments'])
        merged[rn] = mg
        issues += mg['issues']
        by = {}
        for h in rh:
            by.setdefault(h['rule'], []).append(h)
        rounds.append({
            'round': rn,
            'side': tl_r['side'],
            'headline': match_point(rn, tl_r, hr_ot) + round_headline(rn, mg, by, tl_r, erounds.get(rn)),
            'situation': ot_prefix(rn, hr_ot) + situation(erounds.get(rn), tl_r, user),
            'economy': economy(by),
            'main_cause': mg['main_cause'],
            'your_part': mg['your_part'],
            'story': story(tl_r, user, set(names), blind_th, meta.get('tickrate', 64)),
            'moments': mg['moments'],
            'team': team_text(tl_r, mg, by, user, meta.get('tickrate', 64)),
            'rating': rating(mg, info['won']),
            'tags': None,
            'ideas': mg['ideas'],
        })
        # 标签最后拼：回合层面的 hit 要在上面这些字段里写出来了才做标签
        rounds[-1]['tags'] = tags(tl_r, mg, erounds.get(rn), page=rounds[-1], mates=set(meta.get('teammates') or []))
    return {'summary': summary, 'rounds': rounds}, issues


def run(ctx):
    """pipeline 阶段：写 work/<id>/review.json"""
    hits = load_hits(ctx.work)
    review, issues = build_review(ctx, hits)
    out = Path(ctx.work) / 'review.json'
    out.write_text(json.dumps(review, ensure_ascii=False, indent=1), encoding='utf-8')
    n_m = sum(len(r['moments']) for r in review['rounds'])
    msg = f"{len(review['rounds'])} 个回合、{n_m} 个时刻（{len(hits)} 条命中）"
    if issues:
        print(f'M7 句子检查：{len(issues)} 处')
        for x in issues:
            print('  ' + x)
        msg += f'，M7 提示 {len(issues)} 处'
    return msg
