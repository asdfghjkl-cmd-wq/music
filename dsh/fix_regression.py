# -*- coding: utf-8 -*-
"""dsh 临时脚本:照 run_all.py 的方式把四个套件跑一遍,结论另存一份。

和 `tests/run_all.py` 唯一的区别:可以用 `ESCAPE2_FIX` 把
`sys.modules['plugin_sandbox']` 换成别的实现(比如加固前的旧版本)来对比,
不设就是当前的 `plugin_sandbox.py` —— 四个测试文件本身跑的是同一份。

跑法(项目根目录):
    & .\\.venv\\Scripts\\python.exe .\\dsh\\fix_regression.py
"""

import importlib
import io
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
os.chdir(ROOT)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FIX = os.environ.get('ESCAPE2_FIX', 'plugin_sandbox').strip()
sys.modules['plugin_sandbox'] = importlib.import_module(FIX)
if FIX == 'plugin_sandbox':
    print('[fix-regression] 直接跑当前的 plugin_sandbox.py')
else:
    print(f'[fix-regression] 用 {FIX} 顶替 plugin_sandbox')

CASES = (
    'test_sandbox_core.py',
    'test_sandbox_integration.py',
    'test_sandbox_grants.py',
    'test_sandbox_facade.py',
)

# 第六批(fake_filename):帧身份改成 code 对象身份之后,`tests/test_sandbox_facade.py`
# 里"手工 compile 出插件帧 + 裸 exec"的夹具会失真(它的 code 不是宿主登记的,
# 于是被正确判成宿主代码)。带 `--facade-fixture` 跑时,用 dsh/ 里那份**只改了
# 夹具登记**的副本顶替它,好在修复副本上仍然覆盖同一条用例(原文件不动)。
# 同一开关顺带把 integration 也换成 dsh 的副本:那份只多了"plugin/test-1 已经
# 加进 plugin/ 目录"这一处与第六批无关的清单更新(原文件那句注释本来就这么要求)。
FACADE_FIXTURE = os.path.join(HERE, 'fake_filename_facade_test.py')
INTEGRATION_FIXTURE = os.path.join(HERE, 'fake_filename_integration_test.py')


def main():
    failed = []
    use_fixture = '--facade-fixture' in sys.argv
    for name in CASES:
        path = os.path.join('tests', name)
        if use_fixture and name == 'test_sandbox_facade.py':
            path = FACADE_FIXTURE
            print(f'=== {name} (用 dsh/fake_filename_facade_test.py 顶替) ===')
        elif use_fixture and name == 'test_sandbox_integration.py':
            path = INTEGRATION_FIXTURE
            print(f'=== {name} (用 dsh/fake_filename_integration_test.py 顶替) ===')
        else:
            print(f'=== {name} (修复副本) ===')
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
    verdict = f'失败:{failed}' if failed else '全部通过'
    print(verdict)
    # 控制台输出容易被 harness 截断,结论另存一份,方便直接读
    with io.open(os.path.join(HERE,'_last_result.txt'),'w',encoding='utf-8') as fp:
        fp.write(f'{FIX}\n{verdict}\n')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
