# -*- coding: utf-8 -*-
"""探针:插件自己造一个 `_guarded` 帧,能不能骗过审计钩子的"门面帧"判据?

背景(plugin_sandbox.py:2990-2991 作者自认):
    仍然只是一个门槛:插件能直接 `box._guarded(fn)` 从而真的产生 `_guarded` 帧

判据必须干净:**对照组和攻击组都得跑在"已登记为插件代码"的帧里**,
否则审计钩子认不出调用者,两组都放行,结论就无从归因(第一版探针就踩了这个坑)。

所以本探针:
  1. 把攻击代码 `compile()` 出来,用 `register_plugin_code` 登记进沙盒的身份账本,
     于是钩子会把它认成插件代码 —— 和 b.py 装载插件的路径一致;
  2. 对照组:插件帧里直接 `open(目标)`;
  3. 攻击组:插件帧里 `box._guarded(lambda: open(目标))`。

预期(修前):
    对照组 BLOCK、攻击组 **读到了**  => 洞成立

跑法(项目根目录,不用管道 —— dsh 下不能用管道):
    & .\\.venv\\Scripts\\python.exe .\\dsh\\guarded_frame_probe.py
    $env:GUARDED_FIX = 'plugin_sandbox_fix_guarded_frame'
    & .\\.venv\\Scripts\\python.exe .\\dsh\\guarded_frame_probe.py
"""

import os
import sys
import logging

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

# 沙盒的 violation 会走 logging.warning,默认写到 stderr;Windows 控制台是 GBK,
# 中文就变成乱码。让它跟 stdout 同一个流,输出才读得懂。
try:
    logging.basicConfig(stream=sys.stdout, level=logging.WARNING,
                        format='[沙盒] %(message)s')
except Exception:
    pass

FIX_MODULE = os.environ.get('GUARDED_FIX', '').strip()
if FIX_MODULE:
    import importlib
    sys.modules['plugin_sandbox'] = importlib.import_module(FIX_MODULE)
    print('[probe] 本次使用副本:', FIX_MODULE)

import plugin_sandbox as ps

TARGET = r'C:\Windows\win.ini'
PLUGIN_DIR = os.path.join(ROOT, 'plugin', 'escape2')

# ---- 攻击代码:它会被 compile 成 code 对象并登记为"插件代码" ----
# 注意这里是**双层**的:外面这层(PLUGIN_SRC)是插件代码,
# 它里面调 box._guarded 才是要被检验的动作。
PLUGIN_SRC = '''
def plugin_direct_read(box, target):
    """对照组:插件帧里直接读。"""
    with open(target, 'r', encoding='utf-8', errors='replace') as fp:
        return fp.read(16)

def plugin_guarded_read(box, target):
    """攻击组:插件帧里借 box._guarded 造一个"门面帧"。"""
    return box._guarded(
        lambda: open(target, 'r', encoding='utf-8', errors='replace').read(16)
    )

def plugin_guarded_proc(box, target):
    """攻击组变体:同样借 _guarded,但干的是 proc。"""
    import os as _os
    return box._guarded(lambda: _os.system('echo guarded-probe'))

def plugin_legit_facade_open(box, inside):
    """回归组:插件走**正常门面**读自己目录里的文件 —— 这一条必须一直可用。

    这条路径是  插件帧 -> fs_open -> _guarded,直接调用者是宿主方法 fs_open。
    用"栈上有插件帧"当判据就会把它误伤,所以本探针专门守它。

    注意显式指定 encoding:插件源码里有中文,不指定会用系统 GBK 解码失败
    (那是探针自己的 bug,不是沙盒拦的,别把 UnicodeDecodeError 当成误伤)。
    """
    return box.fs_open(inside, 'r', encoding='utf-8', errors='replace').read(16)

def plugin_legit_listdir(box, inside):
    """回归组:插件走门面列自己目录。"""
    return sorted(box.fs_listdir(inside))
'''


class FakeMenu:
    def __init__(self):
        self.commands = []

    def add_command(self, **kw):
        self.commands.append(kw)

    def add_separator(self):
        pass


class FakeApp:
    def after(self, ms, fn=None):
        return 'id'


def build_box():
    policy = ps.parse_policy(None, [], PLUGIN_DIR)
    env = ps.env_box()
    box = env.create('guarded_probe', 'guarded_probe', PLUGIN_DIR,
                     policy, _host_token=ps._HOST_TOKEN)
    box.attach_host(FakeApp(), FakeMenu())
    ps.install_audit_hook()
    box.seal()
    return box


def load_plugin_code(box):
    """按 b.py 的真实路径:compile 插件源码 + 登记 code 身份。"""
    code = compile(PLUGIN_SRC, f'<plugin {box.name} probe>', 'exec')
    ps.register_plugin_code(box, code)          # 这一步让钩子认得出这些帧
    ns = {}
    exec(code, ns)
    return ns


def run_one(label, fn, *args):
    """跑一次并归类结果。返回 True 表示"越权成功"。"""
    try:
        out = fn(*args)
        print(f'  {label}:越权成功,返回 = {out!r}')
        return True
    except ps.SandboxDenied as e:
        print(f'  {label}:BLOCK  {e}')
        return False
    except PermissionError as e:
        print(f'  {label}:BLOCK(系统)  {e}')
        return False
    except Exception as e:
        print(f'  {label}:BLOCK(其它)  {type(e).__name__}: {e}')
        return False


def main():
    print('=' * 70)
    print('探针:插件自造 `_guarded` 帧 vs 审计钩子的门面帧判据')
    print('=' * 70)

    box = build_box()
    ns = load_plugin_code(box)

    print(f'沙盒已封存        = {box.is_sealed()}')
    print(f'目标              = {TARGET}')
    print(f'目标在授权范围里吗 = {box.can("fs:read", TARGET)}   (应为 False)')
    print(f'proc 能力         = {box.can("proc")}   (应为 False)')
    print()

    print('-' * 70)
    print('[对照组] 插件帧里直接读 —— 两道闸都该拦住')
    print('-' * 70)
    direct_ok = run_one('直接 open', ns['plugin_direct_read'], box, TARGET)
    print()

    print('-' * 70)
    print('[攻击组] 插件帧里 box._guarded(fn) —— 造一个被钩子信任的门面帧')
    print('-' * 70)
    guarded_ok = run_one('_guarded(open)', ns['plugin_guarded_read'], box, TARGET)
    print()

    print('-' * 70)
    print('[攻击组变体] 同一个手法换成 proc')
    print('-' * 70)
    proc_ok = run_one('_guarded(os.system)', ns['plugin_guarded_proc'], box, TARGET)
    print()

    # ---------------------------------------------------------------- 回归组
    # 修法必须只堵"插件自造门面帧",不能误伤"插件走正常门面"。
    print('-' * 70)
    print('[回归组] 插件走**正常门面**读自己目录 —— 加了检查之后必须仍然可用')
    print('-' * 70)

    inside = os.path.join(PLUGIN_DIR, 'a.py')
    if not os.path.isfile(inside):
        cands = [f for f in os.listdir(PLUGIN_DIR)
                 if f.endswith(('.py', '.json'))]
        inside = os.path.join(PLUGIN_DIR, cands[0]) if cands else None
    print(f'  授权范围内的文件 = {inside}')

    if inside is None:
        print('  回归组:SKIP(插件目录里没有可读文件)')
        legit_open_ok = None
        legit_listdir_ok = None
    else:
        try:
            got = ns['plugin_legit_facade_open'](box, inside)
            legit_open_ok = True
            print(f'  门面 fs_open(自己目录):可用,返回 = {got!r}')
        except Exception as e:
            legit_open_ok = False
            print(f'  门面 fs_open(自己目录):**被误伤** {type(e).__name__}: {e}')
        try:
            got = ns['plugin_legit_listdir'](box, PLUGIN_DIR)
            legit_listdir_ok = True
            print(f'  门面 fs_listdir(自己目录):可用,{len(got)} 项')
        except Exception as e:
            legit_listdir_ok = False
            print(f'  门面 fs_listdir(自己目录):**被误伤** {type(e).__name__}: {e}')
    print()

    print('=' * 70)
    print('判定')
    print('=' * 70)
    broke_legit = (legit_open_ok is False) or (legit_listdir_ok is False)

    if direct_ok:
        print('  !! 对照组就没拦住 —— 本探针的归因无效,不能据此下结论')
        print('     (说明这个帧没被钩子认成插件代码,需要先修探针)')
        verdict = 'INVALID'
    elif broke_legit:
        print('  攻击组被堵住了,但**正常门面路径被误伤** —— 这个修法不能用。')
        verdict = 'REGRESSION'
    elif guarded_ok or proc_ok:
        print('  对照组 BLOCK,攻击组越权成功')
        print('  => 洞成立:`box._guarded(fn)` 造出的帧被钩子当成"门面替插件做的事"')
        print('     而放行了。plugin_sandbox.py:2990-2991 的自认属实。')
        verdict = 'BUG'
    else:
        print('  对照组 BLOCK,攻击组也 BLOCK,而且正常门面路径照旧可用')
        print('  => 插件自造的 `_guarded` 帧不再被信任,功能没退化。')
        verdict = 'FIXED'

    print()
    print(f'  对照组(直接读)        : {"读到了" if direct_ok else "BLOCK"}')
    print(f'  攻击组(_guarded open) : {"读到了" if guarded_ok else "BLOCK"}')
    print(f'  攻击组(_guarded proc) : {"执行了" if proc_ok else "BLOCK"}')
    print(f'  回归组(门面 fs_open)  : '
          f'{"可用" if legit_open_ok else ("SKIP" if legit_open_ok is None else "误伤")}')
    print(f'  回归组(门面 listdir)  : '
          f'{"可用" if legit_listdir_ok else ("SKIP" if legit_listdir_ok is None else "误伤")}')
    print(f'  VERDICT = {verdict}')
    print('=' * 70)

    return {'INVALID': 2, 'BUG': 1, 'REGRESSION': 3, 'FIXED': 0}[verdict]


if __name__ == '__main__':
    sys.exit(main())
