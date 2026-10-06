# -*- coding: utf-8 -*-
"""检验指定 VLC 运行时目录是否还能真的解码播放。

直接驱动 libvlc(python-vlc),不走 Tk 界面 —— 这样没有显示器也能跑。

用法(任意目录,脚本自己定位项目根):
    ..\\.venv\\Scripts\\python.exe dsh\\tests_check_pvlc_slim.py [运行时目录名]
参数默认 pvlc_slim;给 pvlc 就是完整版对照实验。

注意:dsh 不能玩管道,所以别用 | tee,用 1> / 2> 重定向。
"""

import os
import re
import sys
import time

# 本脚本住在 dsh\,项目根(有 pvlc_slim\ 和 music\)是上一层。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# 默认测精简版;命令行给目录名可以测别的(对照实验用 pvlc)。
SEL = sys.argv[1] if len(sys.argv) > 1 else 'pvlc_slim'
SLIM = os.path.join(ROOT, SEL)

# 关键:python-vlc 的 PYTHON_VLC_MODULE_PATH 只管插件目录,它自己仍会从 PATH
# 捡到 D:\vlc\libvlc.dll —— 那样测的是系统 VLC,不是精简版(上一版就是这么
# 假通过的)。见 vlc.py:find_lib(),PYTHON_VLC_LIB_PATH 优先级最高,把它指向
# 目标目录里的 DLL,插件目录再由 PYTHON_VLC_MODULE_PATH 交给 Instance。
os.environ['PYTHON_VLC_LIB_PATH'] = os.path.join(SLIM, 'libvlc.dll')
os.environ['PYTHON_VLC_MODULE_PATH'] = os.path.join(SLIM, 'plugins')

import ctypes  # noqa: E402

import vlc  # noqa: E402


def dll_path():
    """反查 libvlc.dll 的绝对路径,证明这次加载的确实是目标目录那份。"""
    name = getattr(vlc.dll, '_name', None)
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = ctypes.windll.kernel32.GetModuleFileNameW(
            ctypes.c_void_p(vlc.dll._handle), buf, 32768)
        if size:
            return buf.value
    except Exception as e:
        print('  (查 DLL 路径失败:%s)' % e)
    return name or '?'

MUSIC = os.path.join(ROOT, 'music', 'a', 'm.mp3')
VIDEO = os.path.join(ROOT, 'music', 'x', 'a.mp4')

# 关于日志:不要试图在这里分析 libvlc 日志。踩过的坑——
#   * Instance.log_set 要 ctypes 回调指针(vlc.py 的 LogCb 嵌套定义,拿不到);
#   * 进程内读自己被重定向的 stderr 会读空(重定向是 shell 做的);
#   * --file-logging 每次重开实例都覆盖同一文件,退出时还会被删掉,只能看到
#     最后一个文件解复用的几十行尾巴。
# 要看日志就用 shell 重定向到文件,跑完在外面看:
#   .venv\Scripts\python.exe dsh\tests_check_pvlc_slim.py pvlc_slim 1>out.txt 2>err.txt


def make_instance():
    inst = vlc.Instance(
        '--network-caching=3000 --avcodec-hw=any --codec=avcodec --verbose=2'
    )
    if inst is None:
        raise SystemExit('vlc.Instance() 返回 None:libvlc 没起来')
    return inst


def lines_matching(log, pattern, limit=40):
    pat = re.compile(pattern, re.I)
    out = []
    for line in log:
        if pat.search(line) and line not in out:
            out.append(line)
    return out[:limit]

MUSIC_DIR = os.path.join(ROOT, 'music')
MEDIA_EXT = ('.mp3', '.mp4', '.flac', '.m4a', '.wav', '.ogg', '.avi', '.mkv')


def media_files():
    out = []
    for dirpath, _dirnames, filenames in os.walk(MUSIC_DIR):
        for name in sorted(filenames):
            if name.lower().endswith(MEDIA_EXT):
                out.append(os.path.join(dirpath, name))
    return sorted(out)


def probe(inst, label, path):
    """放一小段,返回 (是否解出数据, 用到的状态集合)。"""
    mp = inst.media_player_new()
    media = inst.media_new_path(path)
    if media is None:
        print('  [失败] %s 建不出 media 对象' % label)
        return False, set()

    mp.set_media(media)
    if mp.play() == -1:
        print('  [失败] %s play() 返回 -1' % label)
        media.release()
        mp.release()
        return False, set()

    seen = set()
    decoding = False
    deadline = time.time() + 25
    while time.time() < deadline:
        time.sleep(0.25)
        st = mp.get_state()
        seen.add(str(st).split('.')[-1])
        if mp.get_time() > 300:
            decoding = True
            break
        if 'Error' in str(st) or 'Ended' in str(st):
            break

    length = mp.get_length()
    t = mp.get_time()
    tracks = mp.video_get_track_count()
    print('  %-12s %-9s time=%-6s len=%-7s tracks=%d %s' % (
        label, '/'.join(sorted(seen)) or '?', t, length, tracks,
        'OK' if decoding else '**解码失败**'))

    mp.stop()
    media.release()
    mp.release()
    return decoding, seen


def main():
    print('插件目录: %s' % SLIM)
    if not os.path.isdir(SLIM):
        raise SystemExit('pvlc_slim 不存在,先跑 make_pvlc_slim.ps1')

    inst = make_instance()
    print('libvlc 版本: %s' % vlc.libvlc_get_version().decode())
    where = dll_path()
    print('libvlc 来自: %s' % where)
    if SLIM.lower() not in where.lower():
        print('!! 加载的不是 pvlc_slim 里的 libvlc,这次结果不能算数')
        return 2

    files = media_files()
    print('媒体库共 %d 个文件' % len(files))
    if not files:
        raise SystemExit('music/ 下没找到媒体文件')

    failed = []
    print('\n=== 逐个播放探测(每个只放 0.3 秒) ===')
    for path in files:
        rel = os.path.relpath(path, ROOT)
        decoding, _seen = probe(inst, os.path.basename(path), path)
        if not decoding:
            failed.append(rel)

    inst.release()

    print('\n结果: %d/%d 解码成功%s' % (
        len(files) - len(failed), len(files),
        '' if not failed else ',失败: %s' % failed))
    print('(日志没在这里分析 —— 需要就看外面重定向的 err 文件)')
    return 0 if not failed else 1


if __name__ == '__main__':
    raise SystemExit(main())
