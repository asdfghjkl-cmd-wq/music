# -*- coding: utf-8 -*-
"""探针:同一插件**反复**越权时,弹窗行为对不对。

用户确认的期望语义:**每次越权都弹窗,除非用户明确选"不再询问"**。
以前 `_ask_user` 用 `_asked` 集合"同类只弹一次",于是第一次答"拒绝"或
"只允许本次"之后,第二次起就被**静默拒绝**,用户再也没机会改主意。

本脚本直接打判权入口 `SandBox.check_fs()`(门面与审计钩子都走它),连续 5 次
做同一件越权的事,记录:弹窗回调调用了几次、每次放行还是拒绝、永久拒绝集合
和持久化回调收到了什么。

五种第一次答案各测一遍:
  ASK_YES(允许本次)/ 'no'(拒绝)/ ASK_SESSION(本次运行都允许)/
  ASK_ALWAYS(总是允许,会持久化授权)/ ASK_NEVER(不再询问)

跑法(项目根目录,别用管道):
    .\\.venv\\Scripts\\python.exe .\\dsh\\ask_repeat_probe.py
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

TMP = os.path.join(ROOT, '_t_askrepeat')
PLUG = os.path.join(TMP, 'p')
OUTSIDE = os.path.join(TMP, 'outside.txt')
TRIES = 5

# 期望:第一次答案 -> (期望弹窗次数, 期望放行次数)
EXPECT = {
    ps.ASK_YES:    (TRIES, 1),
    'no':          (TRIES, 0),
    ps.ASK_SESSION:(1, TRIES),
    ps.ASK_ALWAYS: (1, TRIES),
    ps.ASK_NEVER:  (1, 0),
}

FAIL = []


def setup():
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(PLUG)
    with open(OUTSIDE, 'w', encoding='utf-8') as fp:
        fp.write('secret\n')
    with open(os.path.join(PLUG, 'a.py'), 'w', encoding='utf-8') as fp:
        fp.write('def noop():\n    return 1\n')
    with open(os.path.join(PLUG, 'plugin.json'), 'w', encoding='utf-8') as fp:
        fp.write('{"name":"askrepeat","can_exec":true,"init_file":"a.py",'
                 '"sandbox":{"fs_read":["."],"fs_write":[],"net":false,'
                 '"proc":false,"ask":true,"unsafe":false}}')


def make_box():
    pol = ps.parse_policy({'fs_read': ['.'], 'ask': True}, None, PLUG)
    return ps.SandBox('askrepeat', 'askrepeat', PLUG, pol)


def case(label, first_answer):
    box = make_box()
    asked = []
    persisted = []
    denied_persisted = []

    def fake_ask(bx, cap, target, detail):
        asked.append((cap, target))
        return first_answer if len(asked) == 1 else 'no'

    box.set_ask(fake_ask)
    box.set_persist(lambda name, cap, target: persisted.append((cap, target)))
    box.set_persist_deny(lambda name, cap, target: denied_persisted.append((cap, target)))

    print(f'\n=== {label} ===', flush=True)
    allowed = denied = 0
    for i in range(1, TRIES + 1):
        try:
            box.check_fs(OUTSIDE, write=False, what='读取文件')
            allowed += 1
            res = '放行'
        except ps.SandboxDenied:
            denied += 1
            res = '拒绝'
        except Exception as e:
            denied += 1
            res = f'异常({type(e).__name__}: {e})'
        print(f'  第{i}次: {res:<24} 累计弹窗 {len(asked)} 次', flush=True)

    want_asks, want_allowed = EXPECT[first_answer]
    ok = (len(asked) == want_asks and allowed == want_allowed)
    print(f'  -> 弹窗 {len(asked)} 次(期望 {want_asks}) / 放行 {allowed} 次'
          f'(期望 {want_allowed}) / 拒绝 {denied} 次 / '
          f'授权持久化 {persisted} / 拒绝持久化 {denied_persisted}', flush=True)
    print(f'  -> {"ok" if ok else "FAIL"}  _never_ask={sorted(box._never_ask)}', flush=True)
    if not ok:
        FAIL.append(label)
    return len(asked), allowed


def main():
    setup()
    print(f'实现 = {ps.__name__} | 连续越权 {TRIES} 次读同一个沙盒外文件', flush=True)
    print(f'ASK_* 常量 = yes={ps.ASK_YES!r} session={ps.ASK_SESSION!r} '
          f'always={ps.ASK_ALWAYS!r} never={ps.ASK_NEVER!r}', flush=True)

    case('A. 第一次答"允许本次"(ASK_YES)', ps.ASK_YES)
    case('B. 第一次答"拒绝"', 'no')
    case('C. 第一次答"本次运行都允许"(ASK_SESSION)', ps.ASK_SESSION)
    case('D. 第一次答"总是允许"(ASK_ALWAYS)', ps.ASK_ALWAYS)
    case('E. 第一次答"不再询问"(ASK_NEVER)', ps.ASK_NEVER)

    # F. 宿主把 config.json 里记着的"不再询问"喂回来之后,一次都不该弹
    box = make_box()
    asked = []
    box.set_ask(lambda *a: asked.append(a) or 'no')
    box.remember_denied('fs:read', OUTSIDE, _host_token=ps._HOST_TOKEN)
    print('\n=== F. 宿主恢复"不再询问"后(不该弹窗) ===', flush=True)
    allowed = 0
    for i in range(3):
        try:
            box.check_fs(OUTSIDE, write=False, what='读取文件')
            allowed += 1
        except ps.SandboxDenied:
            pass
    ok = len(asked) == 0 and allowed == 0
    print(f'  -> 弹窗 {len(asked)} 次(期望 0)/ 放行 {allowed} 次(期望 0) -> '
          f'{"ok" if ok else "FAIL"}', flush=True)
    if not ok:
        FAIL.append('F. remember_denied')

    # G. 插件自己不能伪造"不再询问"
    box = make_box()
    print('\n=== G. 插件伪造"不再询问"(该被拒) ===', flush=True)
    try:
        box.remember_denied('fs:read', OUTSIDE)
        print('   FAIL 居然成功了', flush=True)
        FAIL.append('G. 伪造 remember_denied')
    except ps.SandboxDenied as e:
        print(f'   ok 被拒:{e}', flush=True)

    print('\n--- 结论 ---', flush=True)
    print(f'   FAILED: {FAIL or "none"}', flush=True)
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == '__main__':
    raise SystemExit(main())
