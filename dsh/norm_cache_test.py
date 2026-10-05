# -*- coding: utf-8 -*-
"""验证 `_norm` 缓存:cwd 变了不能复用旧结果(相对路径的坑)。"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import plugin_sandbox as ps

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

FAIL = []
CWD1 = os.path.join(ROOT, '_t_normcache', 'd1')      # 留在工作区内,沙箱只允许写这里
CWD2 = os.path.join(ROOT, '_t_normcache', 'd2')


def check(cond, label):
    print(f'   {"ok  " if cond else "FAIL"} {label}', flush=True)
    if not cond:
        FAIL.append(label)


def main():
    import shutil
    shutil.rmtree(os.path.dirname(CWD1), ignore_errors=True)
    for d in (CWD1, CWD2):
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'same.txt'), 'w', encoding='utf-8') as fp:
            fp.write('x')
    d1, d2 = CWD1, CWD2

    old = os.getcwd()
    try:
        os.chdir(d1)
        a = ps._norm('same.txt')
        check(a == ps._norm_uncached('same.txt'), '第一次就与未缓存版一致')
        check(ps._norm('same.txt') == a, '同 cwd 下重复调用稳定')
        check(a and a.startswith(ps._norm_uncached(d1)), f'落在 cwd1 下:{a}')

        os.chdir(d2)                                  # ← 关键:换 cwd
        b = ps._norm('same.txt')
        check(b == ps._norm_uncached('same.txt'), '换 cwd 后与未缓存版一致')
        check(b and b.startswith(ps._norm_uncached(d2)),
              f'换 cwd 后落在 cwd2 下(不能复用旧结果):{b}')
        check(a != b, '两个 cwd 下同名相对路径解析结果确实不同')

        # 绝对路径不受 cwd 影响
        abs_p = os.path.join(d1, 'same.txt')
        c1 = ps._norm(abs_p)
        os.chdir(old)
        c2 = ps._norm(abs_p)
        check(c1 == c2, '绝对路径跨 cwd 结果一致')

        # 兜底行为不变
        check(ps._norm(123) is None, '非路径入参仍然返回 None(不是抛异常)')
        check(ps._norm(None) is None, 'None 入参返回 None')
        class _P:
            def __fspath__(self):
                return abs_p
        check(ps._norm(_P()) == c2, 'PathLike 入参走得通且结果正确')
    finally:
        os.chdir(old)

    print('FAILED:', FAIL or 'none')
    return 1 if FAIL else 0


if __name__ == '__main__':
    raise SystemExit(main())
