# -*- coding: utf-8 -*-
"""探针:用户点了"允许本次"之后,这一次操作到底能不能跑通。

用户实测到的现象(`plugin/sandbox_demo` 的"越界试试"):

    [沙盒] 插件 sandbox_demo:门面准备执行 proc 操作,但记账里并没有这份授权
    [sandbox_demo] 越界读取被拒(预期行为):...proc 操作没有授权

也就是:**用户已经同意**,门面也放行了,可执行前的复核又把这一次拦掉了。

期望:`ASK_YES`(允许本次)应当让**这一次**真的执行成功,而且不该产生
"判权入口可能被换过"这种听起来像被篡改的 violation。

覆盖:proc(有函数参数)/ net / 以及对照组 SESSION(应当落账、照常放行)。
另外每组都跑一个"没问过也不允许"的对照组,确认该拒的还在拒。

跑法(项目根目录,别用管道):
    .\\.venv\\Scripts\\python.exe .\\dsh\\ask_once_exec_probe.py
"""

import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import plugin_sandbox as ps

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

TMP = os.path.join(ROOT, '_t_askonce')
PLUG = os.path.join(TMP, 'p')
FAIL = []


def setup():
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(PLUG)
    with open(os.path.join(PLUG, 'a.py'), 'w', encoding='utf-8') as fp:
        fp.write('def noop():\n    return 1\n')


def make_box(ask_answer=None):
    pol = ps.parse_policy({'ask': True}, None, PLUG)
    box = ps.SandBox('askonce', 'askonce', PLUG, pol)
    if ask_answer is not None:
        box.set_ask(lambda *a: ask_answer)
    return box


def report(label, ok, detail=''):
    print(f'   {"ok  " if ok else "FAIL"} {label}{("  " + detail) if detail else ""}',
          flush=True)
    if not ok:
        FAIL.append(label)


def case_proc(answer, want_ran, label):
    box = make_box(answer)
    ran = []
    wrapper = box.proc_guard(lambda *a, **k: ran.append(1) or 'ok', 'os.system')
    print(f'\n=== {label} ===', flush=True)
    try:
        wrapper()
        err = None
    except Exception as e:
        err = f'{type(e).__name__}: {e}'
    got_ran = bool(ran)
    report(f'真实调用执行了 = {got_ran}(期望 {want_ran})',
           got_ran == want_ran, err or '')
    bad = [e for e in box.events
           if e['action'] == 'violation:guard' and '记账里并没有' in (e['detail'] or '')]
    if want_ran:
        report('没有出现"记账里并没有这份授权"的误报', not bad,
               f'{len(bad)} 条' if bad else '')
    return got_ran


def case_net(answer, want_ran, label):
    box = make_box(answer)
    ran = []
    wrapper = box.net_guard(lambda *a, **k: ran.append(1) or 'ok', 'socket')
    print(f'\n=== {label} ===', flush=True)
    try:
        wrapper()
        err = None
    except Exception as e:
        err = f'{type(e).__name__}: {e}'
    got_ran = bool(ran)
    report(f'真实调用执行了 = {got_ran}(期望 {want_ran})',
           got_ran == want_ran, err or '')
    bad = [e for e in box.events
           if e['action'] == 'violation:guard' and '记账里并没有' in (e['detail'] or '')]
    if want_ran:
        report('没有出现"记账里并没有这份授权"的误报', not bad,
               f'{len(bad)} 条' if bad else '')
    return got_ran


def case_fs(answer, want_ran, label):
    """对照组:fs 走 `_auth_pair`,本来就该对。"""
    box = make_box(answer)
    target = os.path.join(TMP, 'out.txt')
    print(f'\n=== {label} ===', flush=True)
    try:
        with box.fs_open(target, 'w') as fp:
            fp.write('x')
        ran, err = True, None
    except Exception as e:
        ran, err = False, f'{type(e).__name__}: {e}'
    report(f'真实调用执行了 = {ran}(期望 {want_ran})', ran == want_ran, err or '')
    report('文件真的被创建了' if want_ran else '文件没有被创建',
           os.path.exists(target) == want_ran)
    return ran


def main():
    setup()
    print(f'实现 = {ps.__name__}', flush=True)

    case_proc(ps.ASK_YES, True, 'A. proc + "允许本次":应当执行')
    case_proc(ps.ASK_SESSION, True, 'B. proc + "本次运行都允许":应当执行')
    case_proc(None, False, 'C. proc + 没授权也不问(pol.ask=False):应当拒')
    case_net(ps.ASK_YES, True, 'D. net + "允许本次":应当执行')
    case_fs(ps.ASK_YES, True, 'E. 对照组 fs + "允许本次":本来就该执行')

    print('\n--- 结论 ---', flush=True)
    print(f'   FAILED: {FAIL or "none"}', flush=True)
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == '__main__':
    raise SystemExit(main())
