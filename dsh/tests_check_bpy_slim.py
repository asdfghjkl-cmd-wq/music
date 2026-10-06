# -*- coding: utf-8 -*-
"""验证 b.py 的副本能否真的用上 pvlc_slim。

tests_check_pvlc_slim.py 是自己设环境变量测运行时目录;这个脚本走另一条路:
直接 import 播放器主程序的副本(默认 b_pvltest.py),看它内部拿到的到底是
哪一套 VLC。用来回答"改哪一行才能切到精简版、别的代码要不要动"。

用法(任意目录,脚本自己定位项目根):
    ..\\.venv\\Scripts\\python.exe dsh\\tests_check_bpy_slim.py [主程序副本.py]
"""

import ctypes
import os
import sys

# 本脚本和主程序副本都住在 dsh\,项目根(有 pvlc_slim\、music\)是上一层。
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

TARGET = sys.argv[1] if len(sys.argv) > 1 else 'b_pvltest.py'
if not os.path.exists(os.path.join(HERE, TARGET)):
    raise SystemExit('找不到 %s' % TARGET)

print('主程序副本: %s' % TARGET)
print('导入前 PYTHON_VLC_LIB_PATH   = %r' % os.environ.get('PYTHON_VLC_LIB_PATH'))
print('导入前 PYTHON_VLC_MODULE_PATH= %r' % os.environ.get('PYTHON_VLC_MODULE_PATH'))

name = os.path.splitext(TARGET)[0]
mod = __import__(name)

print('\n导入后 PYTHON_VLC_LIB_PATH   = %r' % os.environ.get('PYTHON_VLC_LIB_PATH'))
print('导入后 PYTHON_VLC_MODULE_PATH= %r' % os.environ.get('PYTHON_VLC_MODULE_PATH'))

# 真的加载到的 libvlc.dll
where = getattr(mod.vlc.dll, '_name', '?')
try:
    buf = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.kernel32.GetModuleFileNameW(
            ctypes.c_void_p(mod.vlc.dll._handle), buf, 32768):
        where = buf.value
except Exception as e:
    print('(查 DLL 路径失败:%s)' % e)

print('libvlc 实际来自: %s' % where)
print('libvlc 版本    : %s' % mod.vlc.libvlc_get_version().decode())

# 起一个真的 Player(里面的 vlc.Instance 用的是同一批路径)
cfg = mod.Config()
player = mod.Player({})
print('\nPlayer 建好了,music_player = %r' % (player.music_player,))

# 放一段音频,确认这条路真能出声(不出声也无所谓,这里只验证解码在跑)
path = os.path.join(ROOT, 'music', 'a', 'm.mp3')
ok = player.set_media_path('m', path)
print('set_media_path -> %r' % ok)
player.start_current()
import time
for _ in range(20):
    time.sleep(0.25)
    if player.music_player.get_time() > 300:
        break
t = player.music_player.get_time()
print('播放进度: %s ms' % t)
player.cleanup()

import re
slim_in_dll = 'pvlc_slim' in where.lower()
mod_path_ok = 'pvlc_slim' in (os.environ.get('PYTHON_VLC_MODULE_PATH') or '')
print('\n结论: DLL 来自精简版 = %s' % slim_in_dll)
print('      PYTHON_VLC_MODULE_PATH 指向精简版 = %s' % mod_path_ok)
print('      能解码播放 = %s' % (t > 300))
sys.exit(0 if (slim_in_dll and mod_path_ok and t > 300) else 1)
