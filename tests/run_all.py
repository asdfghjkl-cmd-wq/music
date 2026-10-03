# -*- coding: utf-8 -*-
"""一次跑完插件沙盒自测。

用法(在项目根目录):
    python tests/run_all.py

四个套件按顺序在同一个进程里跑(不启子进程,免得受管道限制):
    1. test_sandbox_core.py         能力策略、路径判定、封存、门面、import
    2. test_sandbox_integration.py  用真实的 b.Plugin 走一遍装载流程
    3. test_sandbox_grants.py       运行期授权、持久化、unsafe 确认、注册表加固
    4. test_sandbox_facade.py       pro 门面白名单、审计钩子(会装钩子,放最后)
"""

import io
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CASES = (
    'test_sandbox_core.py',
    'test_sandbox_integration.py',
    'test_sandbox_grants.py',
    'test_sandbox_facade.py',
)


def main():
    os.chdir(ROOT)                 # 用例里的相对路径都以项目根目录为基准
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    failed = []
    for name in CASES:
        path = os.path.join('tests', name)
        print(f'=== {name} ===')
        sys.argv = [path]
        code = io.open(path, encoding='utf-8').read()
        glob = {'__name__': '__main__', '__file__': os.path.join(ROOT, 'tests', name)}
        try:
            exec(compile(code, path, 'exec'), glob)
        except SystemExit as e:
            if e.code:
                failed.append(name)
        except BaseException:
            traceback.print_exc()
            failed.append(name)
        print()
    if failed:
        print(f'失败:{failed}')
        return 1
    print('全部通过')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
