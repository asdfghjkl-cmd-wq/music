# -*- coding: utf-8 -*-
"""性能基准(诊断版):逐项跑、每项都有时间上限,卡在哪一项立刻能看出来。

为什么这么写:上一版一次性 `timeit.repeat(number=2000)`,只要有一项跑不出来,
中间结果就全看不到。这版每个场景单独计时、单独设上限,超时标 SKIP 继续下一项,
每行都 flush(重定向到文件时也能实时看进度)。

跑法(项目根目录,别用管道):
    .\\.venv\\Scripts\\python.exe -X faulthandler -u .\\dsh\\fake_filename_bench.py
    # 对比别的实现:
    $env:ESCAPE2_FIX = 'plugin_sandbox_fix_environ'; .\\.venv\\Scripts\\python.exe .\\dsh\\fake_filename_bench.py
"""

import gc
import importlib
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

FIX = os.environ.get('ESCAPE2_FIX', 'plugin_sandbox').strip()
sys.modules['plugin_sandbox'] = importlib.import_module(FIX)
import b
import plugin_sandbox as ps

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

TMP = os.path.join(ROOT, '_t_bench')
PLUG = os.path.join(TMP, 'p')
LIMIT = float(os.environ.get('BENCH_LIMIT', '6'))      # 单项时间上限(秒)

PLUGIN_SRC = '''
def noop():
    return 1

for _i in range(20):
    noop()
'''


def say(msg):
    print(msg, flush=True)


def setup_plugin():
    if os.path.isdir(TMP):
        shutil.rmtree(TMP)
    os.makedirs(PLUG)
    with open(os.path.join(PLUG, 'a.py'), 'w', encoding='utf-8') as fp:
        fp.write(PLUGIN_SRC)
    with open(os.path.join(PLUG, 'plugin.json'), 'w', encoding='utf-8') as fp:
        fp.write('{"name":"benchplug","can_exec":true,"init_file":"a.py",'
                 '"sandbox":{"fs_read":["."],"fs_write":[],"net":false,'
                 '"proc":false,"ask":false,"unsafe":false}}')


class FakeApp:
    def after(self, ms, fn=None, *a):
        return 'id'                    # 不执行:只量装载路径


class Host:
    def __init__(self, reg):
        self.menu = None
        self.app = FakeApp()
        self.plugin_list = []
        self.music_dict = {}
        self.env_dict = reg


def one_load():
    reg = b.env_box()
    host = Host(reg)
    n = b.Plugin(PLUG, reg, set())
    n.init_env()
    n.init_i(host)
    return reg


def one_native_audit():
    fp = open(__file__, 'rb')
    fp.close()


def deep_call(fn, depth):
    if depth <= 0:
        return fn()
    return deep_call(fn, depth - 1)


def bench(label, fn, number, repeat=5):
    """跑一项;超过 LIMIT 秒就放弃并标 SKIP(不拖住整个基准)。"""
    try:
        fn()                                   # 预热一次(也顺手暴露异常)
    except Exception as e:
        say(f'   {label:<44} ERR  {e!r}')
        return None
    gc.collect()
    deadline = time.perf_counter() + LIMIT
    best = None
    for _ in range(repeat):
        if time.perf_counter() > deadline:
            break
        t0 = time.perf_counter()
        for _ in range(number):
            fn()
        dt = (time.perf_counter() - t0) / number
        best = dt if best is None else min(best, dt)
    if best is None:
        say(f'   {label:<44} SKIP 超过 {LIMIT}s 上限')
        return None
    say(f'   {label:<44} {best * 1e6:9.2f} us/次')
    return best


def main():
    say(f'[bench] 实现 = {FIX}')
    say(f'[bench] 有 register_plugin_code = {hasattr(ps, "register_plugin_code")}')
    setup_plugin()
    results = {}

    say('--- 1. 装载 ---')
    results['load'] = bench('装载(create+__init__+code 登记)', one_load, 50)

    say('--- 2. 宿主原生活动(栈上没有插件帧) ---')
    results['native'] = bench('宿主 open() 一次', one_native_audit, 300)
    results['native_d20'] = bench('宿主 open(),栈深 20',
                                  lambda: deep_call(one_native_audit, 20), 100)
    results['native_d120'] = bench('宿主 open(),栈深 120',
                                   lambda: deep_call(one_native_audit, 120), 30)

    say('--- 3. 插件路径 ---')
    reg = one_load()
    box = next(iter(reg._boxes.values()))
    say(f'   盒子 = {box.name} | namespace 名字数 = {len(box.namespace)}')
    say(f'   有没有 box.os      = {hasattr(box, "os")}')
    say(f'   有没有 box.set_ask = {hasattr(box, "set_ask")}')
    say(f'   有没有 box.read_text = {hasattr(box, "read_text")}')

    if hasattr(box, 'set_ask'):
        box.set_ask(lambda *a: 'no')

    src = os.path.join(PLUG, 'a.py')
    if hasattr(box, 'read_text'):
        results['facade'] = bench('门面读文件(判权+钩子)',
                                  lambda: box.read_text(src), 300)
    else:
        say('   (没有 read_text,门面这一项跳过)')

    ns = box.namespace
    real_os = ns.get('os')
    say(f'   namespace 里的 os = {type(real_os).__name__ if real_os is not None else None}')
    if real_os is not None:
        def evil():
            try:
                real_os.listdir(os.path.join(ROOT, 'dsh'))
            except Exception:
                pass
        results['evil'] = bench('插件帧真 os 越权(被拒+记 violation)', evil, 100)

    say('--- 汇总(us/次)---')
    for k, v in results.items():
        say(f'   {k:<12} {"SKIP" if v is None else f"{v * 1e6:9.2f}"}')
    shutil.rmtree(TMP, ignore_errors=True)
    say('[bench] done')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
