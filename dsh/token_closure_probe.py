# -*- coding: utf-8 -*-
"""探针:把秘密从"模块 globals"搬进"闭包 cell",是不是真的更难拿到?

背景:AGENTS.md 的 bug 清单里,第二项是"从函数 __globals__ 偷 _HOST_TOKEN"。
想当然的修法是"别把 token 放模块级,放进工厂函数的闭包里"。
本脚本只回答一个问题:**同进程内省下,闭包 cell 比 globals 难拿到吗?**

跑法(项目根目录,不用管道 —— dsh 下不能用管道):
    & .\\.venv\\Scripts\\python.exe .\\dsh\\token_closure_probe.py
"""

import os
import sys

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

SECRET = object()                       # 方案一:模块 globals 里的秘密


def _make_guard():
    """方案二:秘密只在闭包里,工厂函数不把它留在任何模块级名字上。"""
    secret = object()

    def check(token=None):
        return token is secret

    return check


check = _make_guard()                   # 只暴露 check,secret 不在 globals 里


# ------------------------------------------------------------------ 攻击者视角
def via_globals_of_check():
    """从 check 自己的 __globals__ 里翻 secret —— 期望:翻不到(它确实不在那)。"""
    g = check.__globals__
    return [name for name in g if 'secret' in name.lower()]


def via_closure_cell():
    """从 check.__closure__ 里把 cell 内容直接读出来 —— 关键问题。"""
    cells = check.__closure__
    if not cells:
        return None
    return cells[0].cell_contents


def via_code_consts():
    """code 对象的 co_freevars 会**写下闭包变量的名字**,名字本身就泄露了。"""
    return check.__code__.co_freevars


def via_defaults_scan():
    """折中写法:秘密放默认参数。函数 __defaults__ 直接列出来。"""
    def check2(token=None, _secret=SECRET):
        return token is _secret
    return check2.__defaults__


print('=' * 66)
print('探针:闭包 cell vs 模块 globals,能不能挡住同进程内省')
print('=' * 66)
print()

print('[1] check.__globals__ 里有 secret 吗?')
print('    带 secret 字样的名字:', via_globals_of_check() or '(空 —— 确实没留在 globals)')
print()

print('[2] check.__closure__ 能不能直接读出 secret?')
got = via_closure_cell()
if got is None:
    print('    拿到:None —— 闭包这条路是封的')
else:
    print('    拿到:一个对象 id=%#x' % id(got))
    print('    它就是那个秘密吗?', got is _make_guard.__closure__ if False else '(见下断言)')
print()

print('[3] check.__code__.co_freevars 泄露了什么?')
print('    闭包变量名:', via_code_consts())
print()

print('[4] 换成默认参数写法,__defaults__ 泄不泄露?')
print('    __defaults__ =', via_defaults_scan())
print()

# ---- 断言区:把"搬进闭包到底有没有用"变成机器可判 ----
print('-' * 66)
print('判定')
print('-' * 66)

# 真值:我们自己的 check 能不能证明"读出来的 cell 内容就是那个 secret"?
real_secret = _make_guard()             # 另一个独立实例,拿它自己的 cell 做对照
cell_secret = real_secret.__closure__[0].cell_contents
print('  对照:独立实例的 cell 内容 id=%#x' % id(cell_secret))
print('  这个 cell 内容能被 check 自己认作 secret 吗?',
      real_secret(cell_secret))
print()

conclusions = []
if via_globals_of_check():
    conclusions.append('globals 里仍留有秘密名')
else:
    conclusions.append('globals 里确实没有秘密名(方案二在这点上有效)')

if via_closure_cell() is not None:
    conclusions.append('但 __closure__[0].cell_contents 一行就读出来了')
else:
    conclusions.append('__closure__ 也读不出来')

conclusions.append('co_freevars 无论如何会泄露闭包变量名: %r' % (via_code_consts(),))

for one in conclusions:
    print('  *', one)

print()
print('=' * 66)
