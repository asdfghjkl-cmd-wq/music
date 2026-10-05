# -*- coding: utf-8 -*-
"""dsh 临时运行器:在**不经过任何 OS 管道**的前提下跑 dsh/fix_regression.py。

为什么需要它:dsh 沙盒里"捕获另一个进程的 stdout"(Node 的 child_process 用
`stdio:'pipe'`,以及 PowerShell 的 `|` / `*>` / `2>&1`)一律 EPERM,表现为
`Program 'python.exe' failed to run: Access is denied` 或者一个 0 字节的日志。
所以这里让**同一个进程内**跑回归、把输出写进文件,再自己读回来。

跑法(项目根目录):
    & .\\.venv\\Scripts\\python.exe .\\dsh\\run_regression_capture.py [FIX_MODULE]
"""

import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
os.chdir(ROOT)

FIX = (sys.argv[1] if len(sys.argv) > 1 else 'plugin_sandbox').strip()
LOG = os.path.join(HERE, f'_regression_{FIX}.log')

# fix_regression.py 靠 sys.argv 里的 --facade-fixture 决定要不要换成 dsh/ 的夹具副本;
# 少了这个参数,integration 的插件清单与 facade 的手工 compile 夹具都会失真。
sys.argv = [os.path.join(HERE, 'fix_regression.py'), '--facade-fixture']

buf = io.StringIO()
old_stdout = sys.stdout
sys.stdout = buf

src = io.open(os.path.join(HERE, 'fix_regression.py'), encoding='utf-8').read()
g = {'__name__': '__main__', '__file__': os.path.join(HERE, 'fix_regression.py')}
thrown = None
try:
    exec(compile(src, 'dsh/fix_regression.py', 'exec'), g)
except SystemExit:
    pass
except BaseException as e:                       # noqa: BLE001 - 诊断用
    thrown = f'{type(e).__name__}: {e}'
finally:
    sys.stdout = old_stdout

lines = buf.getvalue().splitlines()
with io.open(LOG, 'w', encoding='utf-8') as fp:
    fp.write('\n'.join(lines))
    fp.write('\n')

print(f'[runner] FIX = {FIX}')
print(f'[runner] 全量日志 = {LOG}  ({len(lines)} 行)')
if thrown:
    print(f'[runner] 运行器捕获到异常: {thrown}')
print()

keep = [l for l in lines
        if l.startswith(('ok ', 'FAIL', '===', '[fix', '失败:'))
        or 'FAILED' in l]
for l in keep:
    print(l)

print()
fails = [l for l in lines if l.startswith('FAIL')]
print(f'[runner] 失败项合计 {len(fails)} 条')
