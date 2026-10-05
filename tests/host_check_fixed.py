# -*- coding: utf-8 -*-
"""宿主侧安全验证用例(针对修复副本 `b_fixed.py`)。

用法(项目根目录,**不用管道** —— dsh 下不能用管道):
    & .\\.venv\\Scripts\\python.exe .\\tests\\host_check_fixed.py

只验证**装载器/配置**这一侧的信任边界,不启 GUI、不弹窗:

* H1 ★ 插件自报的 `name` 不能再决定授权归属(原先 `plugin.json` 里写
  `{"name":"debug"}` 就能白嫖 `plugin_unsafe` 里的 "debug" → **静默完全绕过沙盒**)。
* H2 ★ `env_id` 由宿主按插件目录推导,插件自报的值被忽略。
* H4 ★ `sandbox.modules` 只能"申请",不会自动放行。
* H6   坏掉的 config.json 必须留备份,不能被默认值静默覆盖。
* L7   `allow_plugin` 去重。
"""

import io
import itertools
import json
import os
import shutil
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import b as b

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

FAIL = []
RAN = []
_SEQ = itertools.count()
TMP = os.path.join(ROOT, '_hostcheck_%d' % os.getpid())


def test(fn):
    """沿用 tests/ 原有风格:装饰时立即执行;因此不要在 __main__ 里重跑。"""
    RAN.append(fn.__name__)
    try:
        fn()
    except Exception:
        FAIL.append(fn.__name__)
        print(f'FAIL {fn.__name__}')
        traceback.print_exc()
    else:
        print(f'ok   {fn.__name__}')
    return fn


def make_plugin_dir(declared, extra=None):
    """造一个插件目录:目录名与 plugin.json 里自报的 name **故意不一致**。"""
    d = os.path.join(TMP, 'd%d' % next(_SEQ))
    os.makedirs(d, exist_ok=True)
    manifest = {'name': declared, 'can_exec': True, 'sandbox': {}}
    manifest.update(extra or {})
    with io.open(os.path.join(d, 'plugin.json'), 'w', encoding='utf-8') as fp:
        json.dump(manifest, fp)
    with io.open(os.path.join(d, 'a.py'), 'w', encoding='utf-8') as fp:
        fp.write('def init_i(pro):\n    pass\n\ndef run(pro):\n    pass\n')
    return d


# ---------------------------------------------------------------- H1

@test
def t_declared_name_cannot_hijack_unsafe():
    """H1 核心:自报 name 命中 plugin_unsafe 也没用,授权键是目录 realpath。"""
    cfg = b.Config()
    # 真实 config.json 里就有 "debug"(而且没有同名目录 —— 正是原先可被劫持的空位)
    assert 'debug' in cfg.plugin_unsafe, '前置条件变了:config 里应仍有旧值 "debug"'

    d = make_plugin_dir('debug')
    n = b.Plugin(d, b.env_box(), set())

    # 显示名照旧,便于用户辨认
    assert n.name == 'debug', f'显示名应保持 debug,实际 {n.name!r}'
    # 但授权键必须是路径
    assert n.identity == os.path.normcase(os.path.realpath(d)), \
        f'identity 应为目录 realpath,实际 {n.identity!r}'
    assert n.identity != 'debug', 'identity 竟然还是自报的名字'
    # 关键:更新后的判定式不再命中 → 会走"询问"而不是"静默放行"
    assert n.identity not in cfg.plugin_unsafe, \
        'H1 未修:自报 name=debug 仍然白嫖了 plugin_unsafe(静默绕过沙盒)'


@test
def t_declared_name_cannot_hijack_allowlist():
    """H1:自报 name 命中 allow_plugin 也不能免询问加载。"""
    cfg = b.Config()
    d = make_plugin_dir('sandbox_escape2')
    n = b.Plugin(d, b.env_box(), set())
    assert 'sandbox_escape2' in cfg.allow_plugin, '前置条件变了:config 里应有旧值'
    assert n.identity not in cfg.allow_plugin, \
        'H1 未修:自报 name 仍然白嫖了 allow_plugin 白名单'


# ---------------------------------------------------------------- H2

@test
def t_env_id_is_host_derived():
    """H2:env_id 由宿主按目录推导,插件自报的 env_id 被忽略。"""
    d = make_plugin_dir('evplug', {'env_id': 'shared_evil'})
    n = b.Plugin(d, b.env_box(), set())
    assert n.env_id == n.identity, f'env_id 应为 identity,实际 {n.env_id!r}'
    assert n.env_id != 'shared_evil', 'H2 未修:仍在读插件自报的 env_id'


# ---------------------------------------------------------------- H4

@test
def t_modules_are_only_requested():
    """H4:sandbox.modules 只是申请,装载期不会自动放行。"""
    d = make_plugin_dir('modplug', {'sandbox': {'modules': ['ctypes']}})
    n = b.Plugin(d, b.env_box(), set())
    assert list(n.sandbox_policy.modules) == [], \
        f'H4 未修:模块在装载期就放行了 {n.sandbox_policy.modules}'
    assert 'ctypes' in tuple(n.sandbox_policy.modules_requested), \
        '申请没有记进 modules_requested'


# ---------------------------------------------------------------- H6

@test
def t_broken_config_backed_up():
    """H6:config.json 解析失败必须留一份现场,不能被默认值静默覆盖。"""
    bad_dir = os.path.join(TMP, 'badcfg')
    os.makedirs(bad_dir, exist_ok=True)
    bad = os.path.join(bad_dir, 'config.json')
    with io.open(bad, 'w', encoding='utf-8') as fp:
        fp.write('{ 这不是合法 JSON, 被截断了')

    c = b.Config()
    c.path = bad                     # 注入路径,绝不碰真的 config.json
    c.load()

    left = [f for f in os.listdir(bad_dir) if f != 'config.json']
    assert left, f'H6 未修:坏配置没有被备份,目录里只有 {os.listdir(bad_dir)}'
    # 备份里必须还是坏内容的原文,而不是被默认值覆盖后的东西
    bak = io.open(os.path.join(bad_dir, left[0]), encoding='utf-8').read()
    assert '这不是合法 JSON' in bak, f'备份内容不对:{bak!r}'


@test
def t_broken_config_backup_keeps_grants():
    """H6 的现实意义:授权数据不能因为一次坏配置就消失。"""
    bad_dir = os.path.join(TMP, 'badcfg2')
    os.makedirs(bad_dir, exist_ok=True)
    bad = os.path.join(bad_dir, 'config.json')
    with io.open(bad, 'w', encoding='utf-8') as fp:
        fp.write('{"allow_plugin":["keepme"],')   # 合法内容但被截断

    c = b.Config()
    c.path = bad
    c.load()
    left = [f for f in os.listdir(bad_dir) if f != 'config.json']
    bak = io.open(os.path.join(bad_dir, left[0]), encoding='utf-8').read()
    assert 'keepme' in bak, '备份里没保住 allow_plugin 的内容'


# ---------------------------------------------------------------- L7

@test
def t_allow_plugin_deduped():
    """L7:allow_plugin 也要去重(plugin_unsafe 早就去了)。"""
    d = os.path.join(TMP, 'dupcfg')
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, 'config.json')
    with io.open(p, 'w', encoding='utf-8') as fp:
        json.dump({'allow_plugin': ['x', 'x', 'y', '', None, 'y']}, fp)
    c = b.Config()
    c.path = p
    c.load()
    assert c.allow_plugin == ['x', 'y'], f'去重结果不对:{c.allow_plugin!r}'


if __name__ == '__main__':
    shutil.rmtree(TMP, ignore_errors=True)
    try:
        # 写到系统临时目录,不要污染仓库根目录
        with io.open(os.path.join(tempfile.gettempdir(), 'hostcheck_result.json'), 'w',
                     encoding='utf-8') as fp:
            json.dump({'total': len(RAN), 'ran': RAN, 'failed': FAIL,
                       'ok': not FAIL}, fp, ensure_ascii=False, indent=2)
    except Exception:
        traceback.print_exc()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print()
    if FAIL:
        print(f'失败:{FAIL}')
        raise SystemExit(1)
    print('全部通过')
    raise SystemExit(0)
