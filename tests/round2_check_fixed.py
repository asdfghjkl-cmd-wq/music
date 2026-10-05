# -*- coding: utf-8 -*-
"""批次 2 / 批次 3 改动的实证用例(针对 `b_fixed.py`)。

用法(项目根目录,**不用管道** —— dsh 下不能用管道):
    & .\\.venv\\Scripts\\python.exe .\\tests\\round2_check_fixed.py

全部用桩对象 + 未绑定方法调用,**不创建 Tk 窗口、不弹窗、不碰 VLC 实例**,
所以可以在无显示环境下跑。覆盖:

* M3  路径包含判定:必须挡住 `plugin/ab` 匹配到 `plugin/abc` 的前缀陷阱
* M11 `start_sandbox` 默认值必须是 True
* M14 状态文字必须走队列,不能再拿 `after(0)` 当线程安全边界
* M13 `cleanup_and_exit` 必须幂等
* L8  编译失败的插件不得把 `init_ok` 置成 True
* M8  `_select_index` 抛异常也不能挡住真正开始播放
* H5  MediaPlayer 释放后 `_tick`/`_poll` 必须彻底停手且不再排期
"""

import io
import itertools
import json
import os
import queue
import re
import shutil
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import b as b

# b_fixed.py 已改名为 b.py。这些用例是按"读源码文本"做断言的,所以从模块
# 自身取路径,别再硬编码文件名 —— 否则一次改名就让它们集体 FileNotFoundError。
B_SRC = os.path.abspath(getattr(b, '__file__', os.path.join(ROOT, 'b.py')))

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

FAIL = []
RAN = []
_SEQ = itertools.count()
TMP = os.path.join(ROOT, '_round2_%d' % os.getpid())


def test(fn):
    """沿用 tests/ 原有风格:装饰时立即执行;不要在 __main__ 里重跑。"""
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


# ================================================================ M3

@test
def t_is_within_blocks_sibling_prefix():
    """M3 的核心:`plugin/ab` 不能把 `plugin/abc` 认成"之内"。"""
    base = os.path.join(TMP, 'ab')
    sib = os.path.join(TMP, 'abc')
    os.makedirs(base, exist_ok=True)
    os.makedirs(sib, exist_ok=True)

    assert b._is_within(base, base) is True, '自身应算"之内"'
    assert b._is_within(base, os.path.join(base, 'x.txt')) is True, '子路径应算"之内"'
    # 前缀陷阱:纯字符串比较会在这里返回 True
    assert b._is_within(base, sib) is False, 'M3 未修:兄弟目录被当成"之内"'
    assert b._is_within(base, os.path.join(sib, 'y.py')) is False, \
        'M3 未修:兄弟目录下的文件被当成"之内"'
    # 越界与异常输入一律 fail-closed
    assert b._is_within(base, os.path.join(TMP, 'other')) is False
    assert b._is_within(base, r'C:\Windows\win.ini') is False
    assert b._is_within('', base) is False, '空 base 必须按越界处理'
    assert b._is_within(base, '') is False, '空 candidate 必须按越界处理'


# ================================================================ M11

@test
def t_start_sandbox_defaults_true():
    """M11:沙盒必须是默认姿势,不能默认关掉。"""
    assert b.Config.DEFAULT['start_sandbox'] is True, \
        'M11 未修:start_sandbox 默认仍是 False(插件默认无沙盒)'


# ================================================================ M14

class _Label:
    def __init__(self):
        self.text = None

    def config(self, **kw):
        self.text = kw.get('text')


class _StatusPlayer:
    def __init__(self):
        self._pending = queue.Queue()
        self._gen = 7
        self.ml = _Label()


_StatusPlayer._set_status = b.Player._set_status
_StatusPlayer._apply_status = b.Player._apply_status


@test
def t_set_status_goes_through_queue():
    """M14:状态文字不再直接 after(0),而是投队列由 Tk 线程消费。"""
    p = _StatusPlayer()
    p._set_status('播放中')
    assert p._pending.qsize() == 1, f'状态没有进队列:{p._pending.qsize()}'
    item = p._pending.get_nowait()
    assert len(item) == 4, f'必须与播放事件同为四元组,实际 {item!r}'
    assert item[0] is b._STATUS, '第一位应当是 _STATUS 哨兵'
    assert item[1] == '播放中', f'文字不对:{item[1]!r}'
    assert item[2] == 7, '应当带上当前代次'
    # 哨兵不能等于任何真实路径,否则会和播放事件撞车
    assert isinstance(b._STATUS, object) and not isinstance(b._STATUS, (str, bytes))
    # 消费端落到控件上
    p._apply_status(item[1])
    assert p.ml.text == '播放中'


@test
def t_status_events_survive_generation_change():
    """状态事件不该像播放事件那样被"代"作废(它只是提示)。"""
    p = _StatusPlayer()
    p._set_status('提示')
    item = p._pending.get_nowait()
    assert item[2] == p._gen
    # _pump 里对 _STATUS 是 continue,不比较 gen —— 这里直接验证哨兵分支可辨识
    assert item[0] is b._STATUS


# ================================================================ M13

class _Icon:
    def __init__(self):
        self.n = 0

    def stop(self):
        self.n += 1


class _PlayerC:
    def __init__(self):
        self.n = 0

    def cleanup(self):
        self.n += 1


class _Root:
    def __init__(self):
        self.n = 0

    def destroy(self):
        self.n += 1


class _Tkapp:
    def __init__(self):
        self.player = _PlayerC()
        self.app = _Root()
        self.backround_icon = _Icon()


_Tkapp.cleanup_and_exit = b.Tkapp.cleanup_and_exit


@test
def t_cleanup_and_exit_is_idempotent():
    """M13:托盘退出 + 窗口关闭两条路都调它,第二次必须直接返回。"""
    t = _Tkapp()
    try:
        t.cleanup_and_exit()
    except SystemExit:
        pass
    else:
        raise AssertionError('第一次调用应当结束进程(SystemExit)')
    assert (t.player.n, t.app.n, t.backround_icon.n) == (1, 1, 1), \
        f'收尾没跑到:{t.player.n},{t.app.n},{t.backround_icon.n}'

    # 第二次:必须幂等,不能再对已释放的 MediaPlayer 动手
    try:
        t.cleanup_and_exit()
    except SystemExit:
        raise AssertionError('M13 未修:第二次又走了一遍退出流程')
    assert (t.player.n, t.app.n, t.backround_icon.n) == (1, 1, 1), \
        'M13 未修:收尾动作被重复执行'


@test
def t_cleanup_runs_even_if_destroy_raises():
    """M13 的 finally:destroy 炸了也必须先完成 player.cleanup()。"""
    t = _Tkapp()

    def boom():
        raise RuntimeError('模拟 Tk 已散')

    t.app.destroy = boom
    try:
        t.cleanup_and_exit()
    except SystemExit:
        pass
    except Exception:
        pass
    assert t.player.n == 1, 'M13 未修:destroy 抛异常导致 player.cleanup 没执行'


# ================================================================ L8

class _FakeAfter:
    def __init__(self):
        self.scheduled = []

    def after(self, ms, fn=None):
        # 只记录不执行:本用例不启动 GUI
        self.scheduled.append(fn)
        return 'id'


class _FakeTkaapp:
    def __init__(self):
        self.app = _FakeAfter()


def _make_ns_plugin(name, init_src, env_id=None):
    d = os.path.join(TMP, name)
    os.makedirs(d, exist_ok=True)
    manifest = {'name': name, 'can_exec': True, 'init_file': 'a.py'}
    if env_id is not None:
        manifest['env_id'] = env_id
    with io.open(os.path.join(d, 'plugin.json'), 'w', encoding='utf-8') as fp:
        # 关键:manifest 必须声明 init_file,否则 self.init 是空串,
        # compile('') 会成功,根本走不到"编译失败"那条分支(第一版就踩了这个坑)
        json.dump(manifest, fp)
    with io.open(os.path.join(d, 'a.py'), 'w', encoding='utf-8') as fp:
        fp.write(init_src)
    return b.Plugin_no_sandbox(d)


@test
def t_init_ok_not_set_when_compile_fails():
    """L8:init 源码编译不过时,init_ok 必须保持 False(否则 run() 照样跑 command)。"""
    n = _make_ns_plugin('l8_bad', 'def init_i(pro):\n    return (\n')
    n.init_env({})
    tk = _FakeTkaapp()
    n.init_i(tk)
    assert n.init_ok is False, 'L8 未修:编译失败却把 init_ok 置成了 True'
    assert not tk.app.scheduled, 'L8 未修:编译失败仍然把 init 投递上去了'


@test
def t_init_ok_set_when_compile_succeeds():
    """回归:正常插件仍然要被标记成可运行。"""
    n = _make_ns_plugin('l8_good', 'def init_i(pro):\n    pro["ok"] = 1\n')
    n.init_env({})
    tk = _FakeTkaapp()
    n.init_i(tk)
    assert n.init_ok is True, '正常编译的插件没被标记 init_ok'
    assert len(tk.app.scheduled) == 1, '正常插件应当把 init 投递一次'


# ================================================================ M8

class _AdvPlayer:
    def __init__(self):
        self.calls = []
        self.music_dict = {'a': {}}
        self.media_key = 'music'
        self._bad = set()

    def _playable(self, name):
        return 'P'

    def _replay(self, name, path, end_action):
        self.calls.append('replay')
        return True

    def _set_status(self, text):
        pass

    def _after_switch(self, ok, path, end_action):
        self.calls.append('after_switch')

    def _select_index(self, index):
        self.calls.append('select')
        raise b.tkinter.TclError('模拟控件已失效')


_AdvPlayer._advance_to = b.Player._advance_to
# _advance_to 现在会走闸门键(L10/L11),桩也得提供这两个方法
_AdvPlayer._gate_key = b.Player._gate_key
_AdvPlayer._track_path = b.Player._track_path


@test
def t_advance_to_plays_even_if_selection_fails():
    """M8:界面选中抛异常也不能挡住播放(原来就是"点了却不播")。"""
    p = _AdvPlayer()
    p._advance_to('a', None)          # 不应向外抛异常
    assert 'after_switch' in p.calls, 'M8 未修:播放没被启动'
    assert 'select' in p.calls, '选中项这一步应当仍被尝试'
    assert p.calls.index('after_switch') < p.calls.index('select'), \
        'M8 未修:_select_index 仍然排在真正开始播放之前'


# ================================================================ H5

class _FakeMP:
    def __init__(self, released=False):
        self.touched = 0
        self._released = released

    def get_length(self):
        self.touched += 1
        return 1000

    def get_time(self):
        self.touched += 1
        return 0


class _FakeScale:
    def set(self, v):
        pass


class _FakeSeek:
    interval = 500
    SCALE_MAX = 1000

    def __init__(self, released=False):
        self._destroyed = False
        self._dragging = False
        self._after_id = None
        self._total = 0
        self.media_player = _FakeMP(released)
        self.scale = _FakeScale()
        self.label = _Label()

    def after(self, ms, fn):
        self._after_id = 'scheduled'
        return 'scheduled'

    def after_cancel(self, aid):
        self._after_id = None


_FakeSeek._tick = b.SeekBar._tick
_FakeSeek._poll = b.SeekBar._poll


@test
def t_tick_does_not_touch_released_player():
    """H5:MediaPlayer 已 release 后,_tick 必须直接返回,一次都不许碰 libvlc。"""
    s = _FakeSeek(released=True)
    s._tick()
    assert s.media_player.touched == 0, \
        'H5 未修:对已释放的 MediaPlayer 调用了 get_length/get_time(进程级崩溃风险)'


@test
def t_poll_stops_rescheduling_after_release():
    """H5:释放之后 _poll 不能继续排期,否则会把死掉的 player 一直问下去。"""
    s = _FakeSeek(released=True)
    s._poll()
    assert s._after_id is None, 'H5 未修:释放后仍在重新排期'


@test
def t_tick_still_works_when_alive():
    """回归:正常状态下进度条逻辑照旧工作。"""
    s = _FakeSeek(released=False)
    s._tick()
    assert s.media_player.touched >= 2, '正常状态下没去读长度/时间'
    assert s.label.text is not None, '正常状态下没更新标签'


@test
def t_stop_poll_is_idempotent():
    """H5:stop_poll 可重复调用(退出流程里可能被调多次)。"""
    s = _FakeSeek(released=False)
    s._after_id = 'x'
    b.SeekBar.stop_poll(s)
    assert s._destroyed is True and s._after_id is None
    b.SeekBar.stop_poll(s)          # 第二次不应抛异常
    assert s._destroyed is True


# ================================================================ M9

class _ListenPlayer:
    def __init__(self):
        self._gen = 0
        self._listening = True
        self.removed = []

    def del_listen(self, ev):
        self.removed.append(ev)


_ListenPlayer._stop_listen = b.Player._stop_listen


@test
def t_stop_listen_bumps_gen_and_detaches():
    """M9:_stop_listen 必须同时作废代次**并**摘掉监听(两者缺一就状态不一致)。"""
    p = _ListenPlayer()
    p._stop_listen()
    assert p._gen == 1, f'_gen 没推:{p._gen}'
    assert len(p.removed) == 2, f'应当摘掉 2 个监听,实际 {len(p.removed)}'
    assert p._listening is False, '_listening 没复位'

    # 第二次:gen 仍要推(用于作废在途回调),但不该重复摘
    p._stop_listen()
    assert p._gen == 2, 'gen 应当无条件推,便于作废在途回调'
    assert len(p.removed) == 2, '已经摘过了不该重复摘'


@test
def t_no_bare_gen_increment_outside_allowed_places():
    """M9(源码级):裸 `self._gen += 1` 只允许留在两处,其余必须走 _stop_listen。

    允许的两处:
      * `_stop_listen` 自身 —— 它就是推 gen 的那个入口
      * `_stop` —— 注释里写明是有意补推,用于丢弃 _pending 里同一份坏媒体的旧事件
    其他任何地方出现,都意味着"只作废代次、没摘监听"的不一致状态又回来了。
    """
    lines = io.open(B_SRC, encoding='utf-8').read().splitlines()
    cur = '?'
    found = {}
    for i, l in enumerate(lines, 1):
        m = re.match(r'\s*def (\w+)\(', l)
        if m:
            cur = m.group(1)
        if re.search(r'self\._gen \+= 1', l):
            found.setdefault(cur, []).append(i)
    assert sorted(found) == ['_stop', '_stop_listen'], \
        f'M9 未修:裸 _gen 自增出现在 {sorted(found)}(每处 {found})'


# ================================================================ L12 / L13

@test
def t_logging_installed_before_config_is_read():
    """L13:装日志必须排在读 config.json 之前,否则配置解析期的报错无处留痕。"""
    src = io.open(B_SRC, encoding='utf-8').read()
    i_log = src.find('def _setup_logging()')
    i_main_guard = src.find("if __name__ == '__main__':")
    i_boot = src.find('_boot_config = Config()')
    assert i_log != -1, '找不到 _setup_logging'
    assert i_main_guard != -1, '找不到 __main__ 保护'
    assert i_boot != -1, '找不到 _boot_config'
    assert i_log < i_main_guard < i_boot, \
        f'L13 未修:顺序不对 log={i_log} guard={i_main_guard} boot={i_boot}'


@test
def t_config_read_once_at_boot():
    """L12:启动路径上不应再出现两次独立的 Config() 构造。"""
    src = io.open(B_SRC, encoding='utf-8').read()
    assert 'start_plugin = Config().start_plugin' not in src, \
        'L12 未修:仍然是独立 new 出来的第二个 Config 实例'
    assert 'start_plugin = _boot_config.start_plugin' in src, \
        'L12 未修:没有复用同一个 Config 实例'


# ================================================================ M5 / M6 / M7

class _MediaObj:
    def __init__(self, raise_on_release=False):
        self.released = 0
        self.raise_on_release = raise_on_release

    def release(self):
        self.released += 1
        if self.raise_on_release:
            raise RuntimeError('模拟 libvlc release 失败')


class _VlcStub:
    """_set_media 走的是 self.music_player.set_mrl(),不是 self.set_mrl()。"""

    def __init__(self, new):
        self.new = new
        self.set_mrl_calls = []

    def set_mrl(self, path):
        self.set_mrl_calls.append(path)
        return self.new


class _MediaStubPlayer:
    def __init__(self, new=None):
        self._media = None
        self.music_player = _VlcStub(new)

    @property
    def set_mrl_calls(self):
        return self.music_player.set_mrl_calls


_MediaStubPlayer._set_media = b.Player._set_media
_MediaStubPlayer._release_media = b.Player._release_media


@test
def t_close_quietly_never_raises():
    """M6/M7:收尾式关闭必须吞掉一切异常,否则会把播放流程带挂。"""
    b._close_quietly(None, 'none')                 # 连对象都没有

    class _NoClose:
        pass

    b._close_quietly(_NoClose(), 'noclose')        # 没有 close 属性

    closed = []

    class _Good:
        def close(self):
            closed.append(1)

    b._close_quietly(_Good(), 'good')
    assert closed == [1], 'M6/M7 未修:正常的 close 根本没被调用'

    class _Bad:
        def close(self):
            raise RuntimeError('boom')

    b._close_quietly(_Bad(), 'bad')                # 不应向外抛


@test
def t_set_media_releases_previous():
    """M5/M6:set_mrl 的返回值必须被记住,且上一个 Media 要被释放。

    原来直接丢掉返回值,libvlc 每次新建的 Media 再没人释放,换曲越多漏得越多。
    """
    old = _MediaObj()
    new = _MediaObj()
    p = _MediaStubPlayer(new)
    p._media = old
    got = p._set_media(r'C:\m\x.mp3')
    assert got is new, 'set_mrl 的返回值没有被接住'
    assert p._media is new, '当前 Media 没有被更新'
    assert old.released == 1, 'M5/M6 未修:上一个 Media 没有被释放'
    assert p.set_mrl_calls == [r'C:\m\x.mp3']


@test
def t_set_media_survives_release_failure():
    """M6:释放旧对象失败不能让这次切歌失败。"""
    old = _MediaObj(raise_on_release=True)
    new = _MediaObj()
    p = _MediaStubPlayer(new)
    p._media = old
    got = p._set_media(r'C:\m\x.mp3')          # 不应向外抛
    assert got is new and p._media is new, '释放失败不该影响本次切歌'


@test
def t_release_media_clears_and_is_idempotent():
    """M7:释放后必须置空,重复调用不能再碰已释放的 Media(那是崩溃不是异常)。"""
    m = _MediaObj()
    p = _MediaStubPlayer()
    p._media = m
    p._release_media()
    assert m.released == 1, 'M7 未修:Media 没有被释放'
    assert p._media is None, 'M7 未修:释放后没有置空,可能被二次 release'

    p._release_media()                          # 第二次:应当什么都不做
    assert m.released == 1, 'M7 未修:对已释放的 Media 又 release 了一次'


@test
def t_release_media_swallows_failure():
    """M7:release 抛异常时也不能外泄,并且一样要置空。"""
    m = _MediaObj(raise_on_release=True)
    p = _MediaStubPlayer()
    p._media = m
    p._release_media()                          # 不应向外抛
    assert p._media is None, '释放失败也必须置空,避免下次再碰同一个对象'


# ================================================================ L10 / L11

class _GatePlayer:
    """只想验证闸门键:桩里不碰 VLC,也不建窗口。"""

    def __init__(self):
        self.music_message = {'name': ''}      # 无名曲目:标签缺失的情形
        self.media_key = 'music'
        self.music_dict = {}
        self._bad = set()
        self._prompted = set()
        self._settled = ''
        self._short_plays = 0
        self._play_started = 0.0
        self.play_tt = 0
        self.asked = []
        self.status = []

    def _ask_keep(self, label, message):
        self.asked.append((label, message))
        return True                            # 用户选择"不允许播放"

    def _set_status(self, t):
        self.status.append(t)

    def _stop(self):
        pass

    def _stop_listen(self):
        pass

    def _replay(self, name, path, end_action):
        return False

    def _after_switch(self, ok, path, end_action):
        pass

    def _advance_to(self, name, end_action):
        pass

    def _next_seq(self):
        return None

    def _next_random(self):
        return None


_GatePlayer._advance = b.Player._advance
_GatePlayer._gate_key = b.Player._gate_key
_GatePlayer._track_path = b.Player._track_path


@test
def t_gate_key_falls_back_to_path_only_when_nameless():
    """L10/L11:有名字时键不变(零回归);没名字时退回路径。"""
    p = _GatePlayer()
    assert p._gate_key('小星星', r'C:\m\a.mp3') == '小星星', \
        '有名字的曲目键必须和原来完全一致'
    assert p._gate_key('', r'C:\m\a.mp3') == r'C:\m\a.mp3', \
        'L10/L11 未修:无名曲目仍然用空串当键'
    assert p._gate_key('', '') == ''


@test
def t_nameless_track_still_gets_prompted_and_blacklisted():
    """L10/L11:无名曲目的闸门原来被 `if name` 整个跳过 —— 既不弹窗也不拉黑。"""
    p = _GatePlayer()
    p._advance(r'C:\m\broken.mp3', None, failed=True)
    assert p.asked, 'L10/L11 未修:无名曲目坏掉时连弹窗都没有(闸门被跳过)'
    assert r'C:\m\broken.mp3' in p._bad, '拉黑时应当用路径作键'
    assert '' not in p._bad, '绝不能拿空串当闸门键'


@test
def t_nameless_track_loop_gate_engages():
    """L10/L11 的后果:闸门失效会让同一个坏文件的事件无限重放。"""
    p = _GatePlayer()
    p._advance(r'C:\m\broken.mp3', None, failed=True)
    n = len(p.asked)
    assert p._settled == r'C:\m\broken.mp3', '处理后应当记下"这一轮处理过"'
    p._advance(r'C:\m\broken.mp3', None, failed=True)
    assert len(p.asked) == n, \
        'L10/L11 未修:无名曲目的反循环闸门没生效,会无限弹窗/重放'


# ================================================================ L9

class _StartPlayer:
    def __init__(self, rc):
        self.rc = rc
        self.music_message = {'name': 'x'}
        self.status = []
        self._prompted = set()
        self._settled = 'zzz'
        self._play_started = 0.0

    def play(self):
        return self.rc

    def _set_status(self, t):
        self.status.append(t)


_StartPlayer.start_current = b.Player.start_current


@test
def t_start_current_reports_play_failure():
    """L9:libvlc 拒绝播放时 play() 返回 -1,必须被接住并告知用户。"""
    p = _StartPlayer(-1)
    assert p.start_current() is False, '返回 -1 时 start_current 应当返回 False'
    assert p.status, 'L9 未修:播放起不来却没有任何状态提示'
    assert p._play_started == 0.0, '起播失败不该重置短播计时'
    assert p._settled == 'zzz', '起播失败不该清掉反循环闸门'


@test
def t_start_current_ok_path_unchanged():
    """回归:正常起播仍然返回 True,并照旧重置计时与闸门。"""
    p = _StartPlayer(0)
    assert p.start_current() is True
    assert p._play_started > 0.0, '正常起播必须重置短播计时'
    assert p._settled == '', '正常起播应当清掉"已处理"闸门'
    assert p._prompted == set()


# ================================================================ L14

def _tmp_config(name):
    c = b.Config()                     # 读真实 config.json(只读,不 save)
    c.path = os.path.join(TMP, name)
    c.load()                           # 文件不存在 -> 全默认
    return c


@test
def t_deny_round_trips_through_config_file():
    """L14:用户点的"不再询问"必须能落盘并读回,否则重启又问一遍。"""
    c = _tmp_config('deny1.json')
    c.deny('id_a', 'fs:read', r'C:\Windows')
    c.deny('id_a', 'net')
    assert c.denies_for('id_a') == [('fs:read', r'C:\Windows'), ('net', None)], \
        f'denies_for 结果不对:{c.denies_for("id_a")}'
    c.save()

    c2 = b.Config()
    c2.path = c.path
    c2.load()
    assert c2.denies_for('id_a') == [('fs:read', r'C:\Windows'), ('net', None)], \
        'L14 未修:"不再询问"没有落盘/读回'


@test
def t_deny_does_not_renormalize_target():
    """L14:deny 必须原样保存沙盒算好的目标,再归一化会让键对不上。"""
    c = _tmp_config('deny2.json')
    weird = r'C:\Windows\..\Windows'
    c.deny('id_b', 'fs:read', weird)
    assert c.denies_for('id_b') == [('fs:read', weird)], \
        'deny 不该对目标做二次归一化'


@test
def t_deny_survives_garbage_config():
    """L14:plugin_deny 被写坏时回落默认,不能把启动带崩。"""
    c = _tmp_config('deny3.json')
    with io.open(c.path, 'w', encoding='utf-8') as fp:
        json.dump({'plugin_deny': 'not a dict', 'plugin_grants': {}}, fp)
    c.load()
    assert c.denies_for('id_a') == [], '垃圾 plugin_deny 应当回落成空'

    with io.open(c.path, 'w', encoding='utf-8') as fp:
        json.dump({'plugin_deny': {'id_a': 'bad', '': {'net': True},
                                   'id_b': {'fs_read': [1, None, 'C:\\ok']}}}, fp)
    c.load()
    assert c.denies_for('id_a') == [], '非对象条目应当被忽略'
    assert c.denies_for('id_b') == [('fs:read', r'C:\ok')], \
        f'应当只保留合法路径:{c.denies_for("id_b")}'


@test
def t_persist_deny_is_wired():
    """L14:宿主必须既接回调、又在装载时把记录喂回去。"""
    src = io.open(B_SRC, encoding='utf-8').read()
    assert 'set_persist_deny(functools.partial(self._plugin_persist_deny,n))' in src, \
        'L14 未修:宿主没把 set_persist_deny 接上'
    assert 'self.config.denies_for(n.identity)' in src, \
        'L14 未修:装载时没把"不再询问"喂回沙盒'
    assert 'remember_denied' in src, 'L14 未修:没有调用沙盒的 remember_denied'


# ================================================================ M10

class _UiQueue:
    def __init__(self, items):
        self.items = list(items)

    def get_nowait(self):
        if not self.items:
            raise queue.Empty
        return self.items.pop(0)


class _UiApp:
    def __init__(self):
        self.scheduled = 0
        self.deiconified = 0

    def deiconify(self):
        self.deiconified += 1

    def after(self, ms, fn):
        self.scheduled += 1
        return 'id'


class _UiTkapp:
    def __init__(self, items):
        self._ui_queue = _UiQueue(items)
        self.app = _UiApp()

    def play(self):
        raise b.tkinter.TclError('模拟控件已失效')

    def pause(self):
        pass

    def cleanup_and_exit(self):
        pass


_UiTkapp._pump_ui = b.Tkapp._pump_ui


@test
def t_pump_ui_reschedules_after_tcl_error():
    """M10:处理托盘命令时抛 TclError,绝不能把轮询永久停掉。"""
    t = _UiTkapp(['play'])
    t._pump_ui()                                   # 不应向外抛
    assert t.app.scheduled == 1, \
        f'M10 未修:TclError 之后没有重新排期(托盘菜单永久静默)scheduled={t.app.scheduled}'


@test
def t_pump_ui_handles_normal_commands():
    """回归:正常命令照旧执行,并重新排期。"""
    t = _UiTkapp(['show', 'pause'])
    t._pump_ui()
    assert t.app.deiconified == 1, 'show 命令没有被执行'
    assert t.app.scheduled == 1, '处理完应当重新排期'


@test
def t_pump_ui_tray_never_vanishes_after_repeated_errors():
    """M10 的后果:连续多次异常之后轮询必须还活着。"""
    t = _UiTkapp(['play'])
    for _ in range(5):
        t._pump_ui()
    assert t.app.scheduled == 5, \
        f'M10 未修:连续异常之后轮询断了(scheduled={t.app.scheduled})'


@test
def t_pump_ui_quit_stops_rescheduling():
    """回归:quit 必须结束轮询,不能反复进入退出流程。"""
    t = _UiTkapp(['quit'])
    t._pump_ui()
    assert t.app.scheduled == 0, 'quit 之后不该再排期'


# ================================================================ M12

@test
def t_nosandbox_env_id_is_dir_derived():
    """M12:env_id 不能默认 '' —— 多个插件都用 '' 会共用同一份命名空间。"""
    dir_a = os.path.join(TMP, 'm12a')
    n = _make_ns_plugin('m12a', 'def init_i(pro):\n    pass\n')
    assert n.env_id, 'M12 未修:env_id 仍然是空串'
    assert n.env_id == os.path.normcase(os.path.realpath(dir_a)), \
        f'env_id 应当等于插件目录 realpath,实际 {n.env_id!r}'


@test
def t_nosandbox_plugins_do_not_share_namespace():
    """M12 的后果:两个插件 env_id 撞车时会互相覆盖变量。"""
    a = _make_ns_plugin('m12b1', 'def init_i(pro):\n    pass\n')
    c = _make_ns_plugin('m12b2', 'def init_i(pro):\n    pass\n')
    assert a.env_id != c.env_id, 'M12 未修:两个插件的 env_id 撞在一起'

    shared = {}
    a.init_env(shared)
    c.init_env(shared)
    assert a.env_dict is not c.env_dict, 'M12 未修:两个插件共用同一份命名空间'
    assert shared[a.env_id] is a.env_dict
    assert shared[c.env_id] is c.env_dict

    a.env_dict['__probe__'] = 1
    assert '__probe__' not in c.env_dict, 'M12 未修:插件之间仍会互相污染变量'


@test
def t_nosandbox_explicit_env_id_still_honoured():
    """回归:manifest 显式声明的 env_id 仍然优先(老插件依赖它)。"""
    n = _make_ns_plugin('m12c', 'def init_i(pro):\n    pass\n', env_id='my_env')
    assert n.env_id == 'my_env', f'显式 env_id 被忽略了:{n.env_id!r}'


# ================================================================ L5

class _PumpLb:
    def __init__(self):
        self.scheduled = 0

    def after(self, ms, fn):
        self.scheduled += 1
        return 'id'


class _PumpPlayer:
    def __init__(self):
        self._pending = queue.Queue()
        self._gen = 0
        self.listb = _PumpLb()
        self._pumping = True
        self.status = []
        self.calls = []

    def _advance(self, path, end_action, failed):
        self.calls.append(path)
        raise RuntimeError('模拟切歌流程炸了')

    def _apply_status(self, t):
        self.status.append(t)


_PumpPlayer._pump = b.Player._pump


@test
def t_pump_survives_advance_failure():
    """L5:_advance 抛异常时轮询必须继续,并且要让用户看见。"""
    p = _PumpPlayer()
    p._pending.put(('a', None, 0, False))
    p._pump()                                      # 不应向外抛
    assert p.calls == ['a'], '_advance 没有被调用'
    assert p.listb.scheduled == 1, \
        'L5 未修:一次异常就把播放事件轮询停掉了(下一首永远不来)'
    assert p.status, 'L5 未修:切歌流程出错却没有给用户任何提示'


# ================================================================ L6

class _WarnPlayer:
    def __init__(self, lb):
        self.listb = lb


_WarnPlayer._warn_no_file_soon = b.Player._warn_no_file_soon


@test
def t_warn_no_file_soon_survives_missing_listbox():
    """L6:列表控件还没就绪时,一个提示弹窗不能把播放流程打断。"""
    _WarnPlayer(None)._warn_no_file_soon()         # 不应抛

    sched = []

    class _Lb:
        master = None

        def after(self, ms, fn):
            sched.append(fn)

    _WarnPlayer(_Lb())._warn_no_file_soon()
    assert len(sched) == 1, 'L6 未修:提示没有投递到控件上'


if __name__ == '__main__':
    shutil.rmtree(TMP, ignore_errors=True)
    try:
        with io.open(os.path.join(tempfile.gettempdir(), 'round2_result.json'), 'w',
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
