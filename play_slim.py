# -*- coding: utf-8 -*-
"""精简版 VLC 运行时的启动入口。

用 pvlc_slim(212 文件 / 50.9 MB)替代原来的完整 pvlc(476 文件 / 137 MB),
播放器代码本体不动 —— 真正的切换在 dsh/b_pvltest.py 顶部那段环境变量里。

用法(项目根目录):
    .venv\\Scripts\\python.exe play_slim.py
"""

import logging
import os
import sys
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
DSH = os.path.join(ROOT, 'dsh')
sys.path.insert(0, ROOT)
sys.path.insert(0, DSH)

if not os.path.isdir(os.path.join(ROOT, 'pvlc_slim')):
    sys.exit('找不到 pvlc_slim\\,先跑:dsh\\make_pvlc_slim.ps1')

# 主程序副本。它的模块级代码会先把 PYTHON_VLC_LIB_PATH /
# PYTHON_VLC_MODULE_PATH 指到 pvlc_slim,再 import vlc,所以这里不要提前 import vlc。
import b_pvltest  # noqa: E402

if __name__ == '__main__':
    try:
        b_pvltest.main()
    except Exception:
        logging.exception('程序运行期间发生未捕获异常')
        traceback.print_exc()
        raise SystemExit(1)
