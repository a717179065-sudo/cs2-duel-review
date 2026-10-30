"""
复盘流水线：固定 10 步，从 demo 到 report/<key>.html（key = config.output_key：默认用户就是 demo id，别的用户 <id>_<steamid>）。
第一步 base 只用这个 demo 算出这一场的底稿（work/<key>/base/），所以任何 Mirage demo 都能直接复盘，不用先"入库"。

run(ctx, progress) 依次跑 STAGES；每步对应一个模块的 run(ctx)，用到时才 import。
缓存：每步跑完在 work/<id>/_stamps/<步>.json 记一个指纹（demo 大小+修改时间、用户、这一步用到的引擎源码的哈希、上一步的指纹）。
下次指纹一样、产出文件都在，就跳过；工作目录是 work/<key>/；ctx.force 或拿不准时一律重跑，前面有一步重跑后面全部重跑。
progress(stage_key, label, i, n, status, message)：status 取 start / done / skip / fail。
出错时把中文说明交给 progress，然后抛 PipelineError 停下。
"""
import hashlib
import importlib
import json
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

STAGES = [('base', '读取比赛数据'), ('extract', '解析 demo'), ('cache', '准备帧数据'), ('economy', '经济'), ('shooting', '开枪'),
          ('rules', '跑规则'), ('compose', '写复盘'), ('validate', '检查文字'), ('clips', '切 2D 回放'), ('build', '生成页面')]

# 每步：模块、函数、产出文件（相对 work/<key>，"@report" 表示 report/<key>.html）、用到的源码（相对 engine/，目录 = 里面全部 .py/.json）
BASE_OUT = ['base/rounds.csv', 'base/positions.csv', 'base/kills.csv', 'base/util.csv', 'base/duels.csv', 'base/sit_econ.csv',
            'base/sit_hurt.csv', 'base/sit.csv', 'base/user_duels.csv']
SPEC = {
    'base':     ('engine.facts.base', 'run', BASE_OUT, ['facts']),
    'extract':  ('engine.facts.extract', 'run', ['timeline.json', 'match_overview.json'], ['facts']),
    'cache':    ('engine.facts.cache', 'run', ['cache/'], ['facts']),
    'economy':  ('engine.facts.economy', 'run', ['economy.json'], ['facts']),
    'shooting': ('engine.facts.shooting', 'run', ['shooting.json'], ['facts']),
    'rules':    ('engine.rules', 'run_all', ['hits.json'], ['rules', 'common.py', 'thresholds']),
    'compose':  ('engine.compose', 'run', ['review.json'], ['compose.py', 'merge.py', 'summary.py', 'common.py', 'validate.py',
                                                           'thresholds', '../rules/rules_v1.json']),
    'validate': ('engine.validate', 'run', [], ['validate.py', '../rules/rules_v1.json']),
    'clips':    ('engine.clips', 'run', ['clips.json'], ['clips.py']),
    'build':    ('engine.build', 'run', ['@report', 'meta.json'], ['build.py', 'page_template.html', 'assets']),
}
COMMON_SRC = ['config.py', 'ctx.py', 'pipeline.py']   # 每步都算进指纹


class PipelineError(Exception):
    """某一步失败；message 是给人看的中文；usage=True 表示是输入的问题（用户不在 demo 里、不是 Mirage……），
    run.py 用退出码 2"""
    def __init__(self, stage, message, usage=False):
        super().__init__(message)
        self.stage = stage
        self.message = message
        self.usage = usage


def _key(ctx):
    return getattr(ctx, 'key', None) or ctx.id


def report_file(ctx):
    return ROOT / 'report' / f'{_key(ctx)}.html'


def _out_paths(ctx, outs):
    out = []
    for o in outs:
        out.append(report_file(ctx) if o == '@report' else Path(ctx.work) / o.rstrip('/'))
    return out


def _mtimes(paths):
    """产出文件的修改时间（不存在记 None），用来判断失败的这一步有没有动过上次的结果"""
    res = []
    for p in paths:
        try:
            res.append(p.stat().st_mtime_ns)
        except OSError:
            res.append(None)
    return res


def _src_files(names):
    """把源码清单展开成排好序的文件列表（目录取里面的 .py/.json/.html，不进 __pycache__）"""
    files = []
    for n in names:
        p = (HERE / n).resolve()
        if p.is_dir():
            files += [f for f in p.rglob('*') if f.is_file() and f.suffix in ('.py', '.json', '.html') and '__pycache__' not in f.parts]
        elif p.is_file():
            files.append(p)
    return sorted(set(files))


def _src_hash(names):
    h = hashlib.sha1()
    for f in _src_files(COMMON_SRC + names):
        h.update(str(f.relative_to(HERE.parent)).replace('\\', '/').encode('utf-8'))
        h.update(b'\0')
        h.update(f.read_bytes())
        h.update(b'\0')
    return h.hexdigest()


def _outputs_ok(ctx, outs):
    """产出文件都在（目录要非空）"""
    for o in outs:
        if o == '@report':
            p = report_file(ctx)
        else:
            p = Path(ctx.work) / o.rstrip('/')
        if o.endswith('/'):
            if not p.is_dir() or not any(p.iterdir()):
                return False
        elif not p.is_file():
            return False
    return True


def _fingerprint(ctx, key, prev):
    st = Path(ctx.dem).stat()
    return {'stage': key, 'demo_size': st.st_size, 'demo_mtime_ns': st.st_mtime_ns, 'user': str(ctx.user),
            'call': list(SPEC[key][:2]), 'src': _src_hash(SPEC[key][3]), 'prev': prev}


def _fp_id(fp):
    return hashlib.sha1(json.dumps(fp, sort_keys=True).encode('utf-8')).hexdigest()


def _stamp_path(ctx, key):
    return Path(ctx.work) / '_stamps' / f'{key}.json'


def _read_stamp(ctx, key):
    try:
        return json.loads(_stamp_path(ctx, key).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _call(key, ctx):
    mod, fn, _, _ = SPEC[key]
    try:
        m = importlib.import_module(mod)
    except ImportError as e:
        raise PipelineError(key, f'找不到模块 {mod}（{e}）')
    f = getattr(m, fn, None)
    if f is None:
        raise PipelineError(key, f'模块 {mod} 里没有 {fn}()')
    hits_f = Path(ctx.work) / 'hits.json'
    if key == 'rules':
        hits_f.unlink(missing_ok=True)     # 不留上次的结果，避免规则没写时拿旧文件冒充
    try:
        r = f(ctx)
    except SystemExit as e:                # validate 之类按命令行习惯 sys.exit(非零) 表示没通过
        if e.code in (0, None):
            r = None
        else:
            raise PipelineError(key, f'{dict(STAGES)[key]}没通过' + (f'：{e.code}' if isinstance(e.code, str) else ''))
    if key == 'rules':
        # run_all 返回 hit 列表；规则模块自己没写 hits.json 的话这里写
        if isinstance(r, list) and not hits_f.exists():
            hits_f.write_text(json.dumps(r, ensure_ascii=False, indent=1, sort_keys=True), encoding='utf-8')
        return f'{len(r)} 条命中' if isinstance(r, list) else (r if isinstance(r, str) else '')
    if key == 'validate':
        # 返回问题列表（非空 = 没通过）或非零整数
        if isinstance(r, (list, tuple)) and r:
            head = '；'.join(str(x) for x in r[:3])
            raise PipelineError(key, f'检查文字没通过，{len(r)} 处：{head}' + ('……' if len(r) > 3 else ''))
        if isinstance(r, bool) and not r or (isinstance(r, int) and not isinstance(r, bool) and r != 0):
            raise PipelineError(key, '检查文字没通过')
        return r if isinstance(r, str) else '全部通过'
    return r if isinstance(r, str) else ''


def _err_text(e):
    """异常 → 给人看的中文一句话"""
    msg = str(e).strip()
    if isinstance(e, FileNotFoundError):
        return f'缺文件：{e.filename or msg}'
    if isinstance(e, MemoryError):
        return '内存不够'
    # 引擎自己抛的报错（中文）直接显示；其他异常带上类型名，方便查
    if msg and any('一' <= ch <= '鿿' for ch in msg):
        return msg
    return f'{type(e).__name__}: {msg}' if msg else type(e).__name__


USAGE_WORDS = ('目前只支持 Mirage', '这个 demo 里没有用户', '一个回合都没有上场', '这个文件不是 CS2 demo')


def _is_usage(text):
    """是输入不对（换 demo / 换用户就能解决），不是引擎出错"""
    return any(w in text for w in USAGE_WORDS)


def run(ctx, progress=None, stages=None):
    """跑整条流水线；stages 可以只跑其中几步（调试用）。返回 {'id', 'report', 'stages': [{key, status, sec, message}]}"""
    progress = progress or (lambda *a: None)
    Path(ctx.work).mkdir(parents=True, exist_ok=True)
    keys = [k for k, _ in STAGES if stages is None or k in stages]
    n = len(keys)
    labels = dict(STAGES)
    prev, rerun, log = None, bool(getattr(ctx, 'force', False)), []
    for i, key in enumerate(keys, 1):
        label = labels[key]
        fp = _fingerprint(ctx, key, prev)
        fid = _fp_id(fp)
        old = _read_stamp(ctx, key)
        if not rerun and old and old.get('id') == fid and _outputs_ok(ctx, SPEC[key][2]):
            progress(key, label, i, n, 'skip', old.get('message') or '没有变化，沿用上次结果')
            log.append({'key': key, 'status': 'skip', 'sec': 0.0, 'message': old.get('message', '')})
            prev = fid
            continue
        rerun = True                       # 这一步重跑，后面都重跑
        progress(key, label, i, n, 'start', '')
        t0 = time.perf_counter()
        # 先删掉旧指纹（跑到一半失败时不能拿旧指纹配新产出）；但失败时产出文件一个都没动（例如开跑前的检查没过），
        # 就把旧指纹放回去，下次正常跑还能沿用缓存
        sp_old = _stamp_path(ctx, key)
        old_raw = sp_old.read_bytes() if sp_old.is_file() else None
        before = _mtimes(_out_paths(ctx, SPEC[key][2]))

        def restore():
            if old_raw is not None and _mtimes(_out_paths(ctx, SPEC[key][2])) == before:
                sp_old.write_bytes(old_raw)
        try:
            sp_old.unlink(missing_ok=True)
            msg = _call(key, ctx)
        except PipelineError as e:
            restore()
            progress(key, label, i, n, 'fail', e.message)
            raise
        except Exception as e:             # noqa: BLE001  任何异常都转成中文报错停下
            restore()
            text = _err_text(e)
            usage = type(e).__name__ == 'FactsError'      # 事实层自己的中文报错：说明已经写清楚了，不打调用栈
            if not usage:
                traceback.print_exc()
            progress(key, label, i, n, 'fail', text)
            raise PipelineError(key, f'{label}出错：{text}', usage=usage and _is_usage(text)) from e
        sec = round(time.perf_counter() - t0, 1)
        if not _outputs_ok(ctx, SPEC[key][2]):
            text = f'{label}跑完了，但没有写出 {"、".join(SPEC[key][2])}'
            progress(key, label, i, n, 'fail', text)
            raise PipelineError(key, text)
        sp = _stamp_path(ctx, key)
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps({'id': fid, 'fp': fp, 'message': msg}, ensure_ascii=False, indent=1, sort_keys=True), encoding='utf-8')
        progress(key, label, i, n, 'done', msg)
        log.append({'key': key, 'status': 'done', 'sec': sec, 'message': msg})
        prev = fid
    report = report_file(ctx)
    return {'id': _key(ctx), 'demo': ctx.id, 'report': str(report) if report.exists() else None, 'stages': log}
