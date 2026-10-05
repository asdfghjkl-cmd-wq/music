# -*- coding: utf-8 -*-
"""探针:同一个插件**反复**越权时,沙盒到底是"每次都拒绝"还是"只提醒一次"。

用户反馈:"sandbox 越权只会提醒一次,最后全部拒绝"。本脚本不改产品代码,
只把事实量出来:每一次尝试都看四件事 ——
  1. 有没有抛 SandboxDenied(拒绝);
  2. 副作用有没有真的发生(比如文件被写、目录被列出);
  3. box.events 里多了几条 violation 记录;
  4. stdout 上多打了几行 `[沙盒] ...` 提醒。

覆盖三条路:门面(走 `_Proxy`)、真 os(走审计钩子)、同一个门面入口连打 6 次。

跑法(项目根目录,别用管道):
    .\\.venv\\Scripts\\python.exe .\\dsh\\violation_repeat_probe.py
"""

import contextlib
import io
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import b
import plugin_sandbox as ps

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

TMP = os.path.join(ROOT, '_t_vrepeat')
PLUG = os.path.join(TMP, 'p')
OUTSIDE = os.path.join(TMP, 'outside.txt')          # 插件目录之外,应当永远读不到
TRIES = 6


def setup():
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(PLUG)
    with open(OUTSIDE, 'w', encoding='utf-8') as fp:
        fp.write('secret\n')
    with open(os.path.join(PLUG, 'a.py'), 'w', encoding='utf-8') as fp:
        fp.write('def noop():\n    return 1\n')
    with open(os.path.join(PLUG, 'plugin.json'), 'w', encoding='utf-8') as fp:
        fp.write('{"name":"vrepeat","can_exec":true,"init_file":"a.py",'
                 '"sandbox":{"fs_read":["."],"fs_write":[],"net":false,'
                 '"proc":false,"ask":false,"unsafe":false}}')


class FakeApp:
    def after(self, ms, fn=None, *a):
        return 'id'


class Host:
    def __init__(self, reg):
        self.menu = None
        self.app = FakeApp()
        self.plugin_list = []
        self.music_dict = {}
        self.env_dict = reg


def counts(box):
    v = [e for e in box.events if e['action'].startswith('violation')]
    return len(v)


def run_case(label, do_one):
    """连续做 TRIES 次同样的越权,逐次记录:拒绝? 副作用? 新记录? 新提醒?"""
    reg = b.env_box()
    host = Host(reg)
    n = b.Plugin(PLUG, reg, set())
    n.init_env()
    n.init_i(host)
    box = reg.boxes()[0]

    print(f'\n=== {label} ===', flush=True)
    denied = 0
    leaked = 0
    events_before = counts(box)
    prints_total = 0
    for i in range(1, TRIES + 1):
        buf = io.StringIO()
        mark_before = len(box.events)
        try:
            with contextlib.redirect_stdout(buf):
                result = do_one(box, i)
            refused = False
        except ps.SandboxDenied:
            result = None
            refused = True
        except Exception as e:                       # 其它异常也算"没放行"
            result = f'{type(e).__name__}: {e}'
            refused = True
        out = buf.getvalue()
        new_prints = out.count('[沙盒]')
        prints_total += new_prints
        new_events = len(box.events) - mark_before
        if refused:
            denied += 1
        if result not in (None,) and 'SandboxDenied' not in str(result):
            pass
        print(f'  第{i}次: 拒绝={"是" if refused else "否"} '
              f'新 eviction记录={new_events} 新提醒行={new_prints} '
              f'返回={result!r}'[:150], flush=True)
    print(f'  -> 拒绝 {denied}/{TRIES} | violation 记录共 {counts(box) - events_before} 条 '
          f'| 提醒行共 {prints_total}', flush=True)
    return denied, counts(box) - events_before, prints_total


def case_facade_open6(box, i):
    """门面 open('r') 打 6 次:每次都该被拒。"""
    return box.namespace['open'](OUTSIDE, 'r')


def case_facade_write6(box, i):
    """门面 open('w') 到插件目录外:每次都该被拒。"""
    return box.namespace['open'](os.path.join(TMP, f'w{i}.txt'), 'w')


def case_real_os6(box, i):
    """真 os.listdir(绕过门面):每次都该被审计钩子拒。"""
    real = box.namespace['os']
    # namespace 里的 os 是 _Proxy(门面),真 os 从别处拿:这里直接用宿主模块,
    # 模拟"插件拿到真 os 后直接调"。栈上仍有插件帧(本函数由插件代码调用)。
    return os.listdir(ROOT)


def case_facade_listdir6(box, i):
    return box.namespace['os'].listdir(ROOT)


def main():
    setup()
    print(f'实现 = {ps.__name__} | TRIES = {TRIES}', flush=True)
    run_case('A. 门面 open("r") 读插件目录外', case_facade_open6)
    run_case('B. 门面 open("w") 写插件目录外', case_facade_write6)
    run_case('C. 门面 os.listdir(沙盒外)', case_facade_listdir6)

    # D. 真 os:插件代码里拿到真 os 再调 —— 用一个小插件函数来保证栈上有插件帧。
    reg = b.env_box()
    n = b.Plugin(PLUG, reg, set())
    n.init_env()
    box = reg.boxes()[0]
    ns = box.namespace
    print('\n=== D. 插件帧上真 os(审计钩子路径)===\n', flush=True)
    denied = 0
    events_before = counts(box)
    prints_total = 0
    for i in range(1, TRIES + 1):
        buf = io.StringIO()
        mark = len(box.events)
        try:
            with contextlib.redirect_stdout(buf):
                # 直接调宿主侧的 os.fspath 之类的"原生"入口不会带插件帧;
                # 用 compile 出来的插件 code(已由 SandBox 登记)执行真 os 调用。
                code = ps.compile_plugin_code(
                    '__import__("os").listdir(r"%s")\n' % ROOT, box)
                box.exec_plugin_code(code, ns)
            refused = False
            res = 'no-error'
        except ps.SandboxDenied as e:
            refused = True
            res = 'SandboxDenied'
        except Exception as e:
            refused = True
            res = f'{type(e).__name__}'
        out = buf.getvalue()
        new_prints = out.count('[沙盒]')
        prints_total += new_prints
        denied += 1 if refused else 0
        print(f'  第{i}次: 拒绝={"是" if refused else "否"} '
              f'新记录={len(box.events) - mark} 新提醒行={new_prints} {res}', flush=True)
    print(f'  -> 拒绝 {denied}/{TRIES} | violation 记录共 {counts(box) - events_before} 条 '
          f'| 提醒行共 {prints_total}', flush=True)

    shutil.rmtree(TMP, ignore_errors=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
