# -*- coding: utf-8 -*-
"""插件沙盒:用"能力(capability)"限制插件能碰的文件、网络与外部进程。

能力(默认都是关的,只有"读插件自己的目录"默认开):
    fs:read    读文件
    fs:write   写/删/改名
    net        网络(socket / urllib / http)
    proc       外部进程(subprocess / os.system / ctypes)

plugin.json 里的声明方式:
    "sandbox": {
        "fs_read":  [".", "../music"],   # 相对插件目录,也可以写绝对路径
        "fs_write": [],                  # 默认空:要用就得在运行期向用户申请
        "net": false,
        "proc": false,
        "ask": true,                     # 被拒时是否弹窗向用户请求授权
        "unsafe": false,                 # true = 完全不受限制(装载时需用户确认)
        "modules": ["numpy"]             # 额外放行的顶层模块(不受门面代理)
    }
历史遗留的 "privilege": ["no_sandbox_really"] 等价于 unsafe;其它取值只提示、不生效。

定位(重要):这是"能力约束 + 审计",不是安全边界。
    插件和播放器在同一个进程、同一个解释器里,所以下面这些路都能绕开本模块:
      * Python 内省(type(...).__mro__、__subclasses__、函数的 __globals__);
      * 插件为了画界面必然拿得到的 tkinter/Tcl 通道(Tcl 本身就能起进程);
      * 任何"给它一个路径它就去读"的第三方库。
    它挡的是"插件顺手读写文件、偷偷联网、起个进程"这类事故和明确越权,
    并把每次拒绝/授权都写进日志。要真正的隔离,只能把插件放进单独的受限
    进程里 —— 那样插件就无法直接操作 Tk 界面了,所以这里取舍是可用性优先。
"""

import builtins as _builtins
import collections
import copy
import datetime
import functools
import http.client
import importlib as _importlib
import io
import itertools
import json as _json
import logging
import math
import os
import random
import re
import socket
import string
import subprocess
import sys
import textwrap
import threading
import time
import traceback
import types as _types
import urllib.parse
import urllib.request

import mutagen
import mutagen.flac
import mutagen.id3
import tkinter
import tkinter.colorchooser
import tkinter.filedialog
import tkinter.font
import tkinter.messagebox
import tkinter.simpledialog
import tkinter.ttk
import ttkbootstrap
from PIL import Image as _PILImage
from PIL import ImageTk as _PILImageTk

__all__ = ['SandboxDenied','Policy','parse_policy','SandBox','SandboxView','env_box']

# 宿主专用凭据:只有"本来就持有它的一方"能给插件授权。插件拿不到这个对象,
# 所以 plugin/nb/a.py 里那种"给自己 grant 一下"的写法只会被拒。
_HOST_TOKEN = object()

# 询问授权时宿主可以返回的答案。
ASK_YES = 'yes'          # 只放行这一次
ASK_SESSION = 'session'  # 本次运行都放行(记住这个目录/能力)
ASK_ALWAYS = 'always'    # 放行并持久化到 config.json


class SandboxDenied(Exception):
    """插件访问了没有授权的能力。调用方应当把它当成"操作没发生"。"""


# ---------------------------------------------------------------- 路径判定

def _norm(path):
    """规范化成"绝对 + 解析过符号链接 + 大小写统一"的形式,用于前缀比较。

    路径不存在也能算(realpath 只解析已存在的部分),所以写入新文件前也能判定。
    """
    if not isinstance(path,str):
        try:
            path = os.fspath(path)
        except TypeError:
            return None
        if not isinstance(path,str):
            return None
    try:
        return os.path.normcase(os.path.realpath(path))
    except (OSError,ValueError):
        return None


def under(root, path):
    """path 是否就是 root 或位于 root 之内。"""
    r = _norm(root)
    p = _norm(path)
    if r is None or p is None:
        return False
    if r == p:
        return True
    if not r.endswith(os.sep):
        r += os.sep
    return p.startswith(r)


def _mode_writes(mode):
    """open() 的 mode 是否涉及写。"""
    return any(c in mode for c in ('w','a','x','+'))


# ---------------------------------------------------------------- 策略

class Policy:
    """一个插件的能力策略。默认:只读自己的目录,写/网络/进程全关。"""

    def __init__(self,plugin_dir):
        self.plugin_dir = os.path.abspath(plugin_dir)
        self.fs_read = [self.plugin_dir]
        self.fs_write = []
        self.net = False
        self.proc = False
        self.ask = True
        self.unsafe = False            # 生效值:只有宿主在用户确认后能打开
        self.unsafe_requested = False   # 插件在 plugin.json 里"要求"不受限制
        self.modules = []
        self.warnings = []

    def roots(self,write):
        """写权限隐含读权限。"""
        if write:
            return list(self.fs_write)
        return list(self.fs_read) + list(self.fs_write)

    def freeze(self):
        """封存:把可变的列表换成元组,免得插件事后往里塞路径。"""
        self.fs_read = tuple(self.fs_read)
        self.fs_write = tuple(self.fs_write)
        self.modules = tuple(self.modules)

    def describe(self):
        if self.unsafe:
            return '不受沙盒限制(危险,已由你确认)'
        out = [f'读取:{", ".join(self.fs_read) or "无"}',
               f'写入:{", ".join(self.fs_write) or "无(需要时向你申请)"}']
        if self.net:
            out.append('网络:允许')
        if self.proc:
            out.append('外部进程:允许')
        if self.modules:
            out.append('额外模块:' + ','.join(self.modules))
        if self.unsafe_requested:
            out.append('插件要求不受沙盒限制(你没确认,当前仍受限)')
        return ' | '.join(out)


def _str_list(raw,key,policy,base_dir):
    v = raw.get(key,None)
    if v is None:
        return None
    if isinstance(v,str):
        v = [v]
    if not isinstance(v,(list,tuple)):
        policy.warnings.append(f'sandbox.{key} 不是字符串或数组,未生效')
        return None
    out = []
    for item in v:
        if not isinstance(item,str) or not item.strip():
            policy.warnings.append(f'sandbox.{key} 里的 {item!r} 无效,已忽略')
            continue
        p = item if os.path.isabs(item) else os.path.join(base_dir,item)
        out.append(os.path.normpath(p))
    return out


def parse_policy(raw_sandbox,legacy_privilege,plugin_dir):
    """从 plugin.json 的 sandbox 段(以及历史的 privilege)得出 Policy。

    任何不认识的键、类型不对的值都只记一条 warning 并按默认(更严)处理,
    不让一个写错的 plugin.json 把插件变成不受限制。
    """
    p = Policy(plugin_dir)
    raw = raw_sandbox if isinstance(raw_sandbox,dict) else {}
    if raw_sandbox is not None and not isinstance(raw_sandbox,dict):
        p.warnings.append('sandbox 不是 JSON 对象,按默认(最严)处理')

    known = ('fs_read','fs_write','net','proc','ask','unsafe','modules')
    for k in raw:
        if k not in known:
            p.warnings.append(f'sandbox 里的未知键 {k!r} 已忽略')

    got = _str_list(raw,'fs_read',p,p.plugin_dir)
    if got is not None:
        p.fs_read = got
    got = _str_list(raw,'fs_write',p,p.plugin_dir)
    if got is not None:
        p.fs_write = got

    for key in ('net','proc','ask'):
        v = raw.get(key,None)
        if v is None:
            continue
        if not isinstance(v,bool):
            p.warnings.append(f'sandbox.{key} 不是布尔值,未生效')
            continue
        setattr(p,key,v)
    # unsafe 只是"申请":真正放开要宿主在用户确认后调 set_unsafe()
    v = raw.get('unsafe',None)
    if v is not None:
        if not isinstance(v,bool):
            p.warnings.append('sandbox.unsafe 不是布尔值,未生效')
        elif v:
            p.unsafe_requested = True
            p.warnings.append('sandbox.unsafe=true:要求完全不受沙盒限制,需要你在装载时确认')

    mods = raw.get('modules',None)
    if mods is not None:
        if isinstance(mods,str):
            mods = [mods]
        if not isinstance(mods,(list,tuple)):
            p.warnings.append('sandbox.modules 不是字符串或数组,未生效')
        else:
            for item in mods:
                if isinstance(item,str) and item.strip() and re.fullmatch(r'[A-Za-z_][\w.]*',item):
                    p.modules.append(item)
                else:
                    p.warnings.append(f'sandbox.modules 里的 {item!r} 不是合法模块名,已忽略')

    # 历史 privilege
    legacy = legacy_privilege
    if legacy is not None and not isinstance(legacy,(list,tuple)):
        p.warnings.append('privilege 不是数组,已忽略')
        legacy = None
    for item in (legacy or ()):
        if item == 'no_sandbox_really':
            p.unsafe_requested = True
            p.warnings.append('privilege=no_sandbox_really:要求完全不受沙盒限制,'
                              '需要你在装载时确认')
        elif item == 'built':
            p.warnings.append("privilege 'built' 已被沙盒取代(常用内置函数默认可用),已忽略")
        else:
            p.warnings.append(f'未知的 privilege {item!r} 已忽略')

    return p


# ---------------------------------------------------------------- 门面

class _Proxy:
    """按白名单把真模块暴露给插件;能碰文件/网络/进程的入口换成检查过的包装。

    用 __slots__,插件即便拿到门面也塞不进新属性;未知属性一律 SandboxDenied
    (而不是 AttributeError),让插件作者一眼看出"是沙盒挡的"。
    """

    __slots__ = ('_sb','_real','_label','_allow','_wrap','_deny')

    def __init__(self,sb,real,label,allow=None,wrap=None,deny=(),extra=None):
        self._sb = sb
        self._real = real
        self._label = label
        self._allow = None if allow is None else frozenset(allow)
        merged = dict(wrap or {})
        if extra:
            merged.update(extra)          # 不涉及检查的固定属性(如 urllib.parse)
        self._wrap = merged
        self._deny = frozenset(deny or ())

    def __getattr__(self,item):
        if item in self._deny:
            self._sb.violation('attr',f'{self._label}.{item} 已被沙盒禁用',None)
            raise SandboxDenied(f'{self._label}.{item} 被插件沙盒禁用')
        if item in self._wrap:
            return self._wrap[item]
        if item.startswith('__') and item.endswith('__'):
            raise AttributeError(item)
        if self._allow is not None and item not in self._allow:
            self._sb.violation('attr',f'{self._label}.{item} 不在沙盒白名单里',None)
            raise SandboxDenied(f'{self._label}.{item} 不在插件沙盒白名单里')
        try:
            return getattr(self._real,item)
        except AttributeError:
            raise AttributeError(f'{self._label} 没有属性 {item}')

    def __dir__(self):
        names = set(self._wrap) | set(self._allow or ())
        return sorted(names)

    def __repr__(self):
        return f'<sandbox {self._label} for {self._sb.name}>'


class SandboxView:
    """给插件自己看的只读视图:能查"我有什么权限",改不了策略。"""

    __slots__ = ('name','can','describe','events')

    def __init__(self,sb):
        self.name = sb.name
        self.can = sb.can
        self.describe = sb.policy.describe
        self.events = sb.audit

    def __repr__(self):
        return f'<sandbox {self.name}: {self.describe()}>'


# ---------------------------------------------------------------- 沙盒

class SandBox:
    """一个插件的策略 + 受控命名空间。

    宿主用法(见 b.py 的 Plugin):
        box = SandBox(env_id,name,plugin_dir,policy)
        box.set_ask(...); box.set_persist(...)
        box.attach_host(tkaapp)      # 决定 pro 是什么
        exec(code,box.namespace)
        box.seal()                   # 装载结束、插件代码开跑之前
    """

    def __init__(self,env_id,name,plugin_dir,policy):
        self.env_id = env_id
        self.name = name
        self.policy = policy
        self.plugin_dir = os.path.abspath(plugin_dir)
        self.namespace = {}
        self.events = []                 # 审计:最近的拒绝/授权
        self._session = {'fs:read':set(),'fs:write':set(),'net':False,'proc':False}
        self._asked = set()
        self._ask = None
        self._persist = None
        self._host = None
        self._facade = None              # 插件真正拿到的 pro(白名单门面)
        self._local_modules = {}
        self._module_cache = {}
        self.__sealed = False            # 名字改写后是 _SandBox__sealed
        self._build_namespace()
        register_plugin_frames(self)     # 让审计钩子认得"这是插件的代码帧"

    # ---------------- 封存与授权 ----------------

    @property
    def frame_tag(self):
        """b.py 用 <plugin 名字 init/command> 当编译出来的文件名,靠它认插件帧。"""
        return f'<plugin {self.name} '

    def _guarded(self,func,*args,**kwargs):
        """执行"沙盒 / 宿主替插件做的真实调用"。

        这期间审计钩子放行:权限刚刚已经查过了。顺带也避免了钩子自己的
        策略检查(realpath 会触发 os.lstat 审计事件)递归回来。
        """
        _facade_enter()
        try:
            return func(*args,**kwargs)
        finally:
            _facade_exit()

    def seal(self):
        """冻结策略。之后连宿主也不能再 grant,插件更不行。"""
        self.policy.freeze()
        self.__sealed = True
        self.note('seal','*',None,f'策略冻结:{self.policy.describe()}')

    def is_sealed(self):
        return self.__sealed

    def grant(self,*args,_host_token=None,**kwargs):
        """只有宿主(持有 _HOST_TOKEN 的一方)能调用。

        插件调用会走到这里然后被拒 —— plugin/nb/a.py 里那几行
        "env_dict.grant(__env_id__,[...])" 就是想走这条路。
        """
        if _host_token is not _HOST_TOKEN:
            self.violation('grant','插件试图给自己授权',None)
            raise SandboxDenied(f'插件 {self.name} 不能给自己授权(只有宿主能)')
        if self.is_sealed():
            raise SandboxDenied(f'插件 {self.name} 的沙盒已封存,不能再改策略')
        cap = args[0] if args else kwargs.get('cap')
        target = args[1] if len(args) > 1 else kwargs.get('target')
        self._apply_grant(cap,target,persist=False)
        self.note('grant','*',target,f'宿主授予 {cap}')

    def set_unsafe(self,value=True,_host_token=None):
        """把插件从沙盒里放出来。只有宿主能调,而且必须已经得到用户确认。"""
        if _host_token is not _HOST_TOKEN:
            self.violation('set_unsafe','插件试图解除自己的沙盒限制',None)
            raise SandboxDenied(f'插件 {self.name} 不能解除自己的沙盒限制')
        if self.is_sealed():
            raise SandboxDenied(f'插件 {self.name} 的沙盒已封存,不能再改')
        self.policy.unsafe = bool(value)
        self.note('unsafe','*',None,f'unsafe={bool(value)}')

    def _apply_grant(self,cap,target,persist=False):
        if cap in ('fs:read','fs:write'):
            root = self._grant_root(target)
            self._session[cap].add(root)
            if cap == 'fs:write':
                self._session['fs:read'].add(root)
            self.policy.warnings.append(f'宿主额外授予 {cap} = {root}')
        elif cap in ('net','proc'):
            self._session[cap] = True
        else:
            raise SandboxDenied(f'没有这种能力:{cap!r}')
        if persist and self._persist is not None:
            try:
                self._persist(self.name,cap,self._grant_root(target) if target else True)
            except Exception:
                logging.exception('保存插件授权失败')

    def _grant_root(self,target):
        if not target:
            return None
        target = os.path.abspath(os.fspath(target))
        if os.path.isdir(target):
            return target
        parent = os.path.dirname(target)
        return parent or target

    # ---------------- 宿主接口 ----------------

    def set_ask(self,func):
        """func(sandbox,cap,target,detail) -> 'yes'/'session'/'always'/其它=拒绝"""
        self._ask = func

    def set_persist(self,func):
        """func(plugin_name,cap,target) -> None:把授权写进 config.json。"""
        self._persist = func

    def attach_host(self,tkaapp,facade=None,**kwargs):
        """把宿主以"白名单门面"的形式交给插件(不再是整个 Tkapp)。"""
        self._host = tkaapp
        if tkaapp is None:
            return None
        self._facade = HostFacade(self,tkaapp) if facade is None else facade
        self.namespace['pro'] = self._facade
        return self._facade

    def audit(self,limit=20):
        return list(self.events[-limit:])

    def note(self,action,cap,target,detail=''):
        rec = {'plugin':self.name,'action':action,'cap':cap,
               'target':str(target) if target is not None else None,'detail':detail}
        self.events.append(rec)
        if len(self.events) > 200:
            del self.events[:100]
        return rec

    def violation(self,action,detail,target):
        self.note('violation:'+action,'*',target,detail)
        _facade_enter()
        try:
            logging.warning('插件 %s 触发沙盒拦截:%s(%s)',self.name,detail,target)
            print(f'[沙盒] 插件 {self.name}:{detail}')
        finally:
            _facade_exit()

    # ---------------- 能力判定 ----------------

    def can(self,cap,target=None):
        """现在是否允许,不弹窗、不抛异常(插件自查与宿主 UI 都用它)。"""
        if self.policy.unsafe:
            return True
        if cap in ('fs:read','fs:write'):
            if target is None:
                return False
            roots = self.policy.roots(cap == 'fs:write')
            if any(under(r,target) for r in roots):
                return True
            return any(under(r,target) for r in self._session[cap])
        if cap in ('net','proc'):
            return bool(getattr(self.policy,cap)) or bool(self._session[cap])
        raise ValueError(f'未知能力:{cap!r}')

    def _ask_user(self,cap,target,detail):
        if self.policy.unsafe:
            return True
        if not self.policy.ask or self._ask is None:
            return False
        if threading.current_thread() is not threading.main_thread():
            self.note('ask_skipped',cap,target,'不在主线程,不能弹窗,直接拒绝')
            return False
        key = (cap,_norm(target) if target else '')
        if key in self._asked:
            return False
        self._asked.add(key)
        try:
            ans = self._guarded(self._ask,self,cap,target,detail)
        except Exception:
            logging.exception('插件沙盒的授权询问失败,按拒绝处理')
            return False
        if ans == ASK_YES:
            self.note('grant_once',cap,target,detail)
            return True
        if ans in (ASK_SESSION,ASK_ALWAYS):
            self._apply_grant(cap,target,persist=(ans == ASK_ALWAYS))
            self.note('grant_'+ans,cap,target,detail)
            return True
        self.note('denied_by_user',cap,target,detail)
        return False

    def deny(self,cap,target,what):
        self.violation('deny',f'{what or "操作"}需要能力 {cap},未获授权',target)
        raise SandboxDenied(
            f'插件 {self.name} 的 {what or "操作"} 需要 {cap} 能力,'
            f'而它没有被授权:{target if target is not None else ""}')

    def check_fs(self,path,write=False,what=''):
        """返回通过检查的路径;没通过就抛 SandboxDenied(可能先弹窗问用户)。"""
        if isinstance(path,int):
            self.violation('fd','不支持用文件描述符访问文件',path)
            raise SandboxDenied('沙盒不允许用文件描述符(fd)访问文件')
        cap = 'fs:write' if write else 'fs:read'
        if self.can(cap,path):
            return path
        if self._ask_user(cap,path,what or ('写入文件' if write else '读取文件')):
            return path
        self.deny(cap,path,what)

    def check_net(self,what='网络访问'):
        if self.can('net'):
            return True
        if self._ask_user('net',None,what):
            return True
        self.deny('net',None,what)

    def check_proc(self,what='启动外部进程'):
        if self.can('proc'):
            return True
        if self._ask_user('proc',None,what):
            return True
        self.deny('proc',None,what)

    # ---------------- 文件操作(受控) ----------------

    def fs_open(self,file,mode='r',*args,**kwargs):
        if isinstance(file,int):
            self.violation('fd','不支持用文件描述符打开文件',file)
            raise SandboxDenied('沙盒不允许用文件描述符(fd)打开文件')
        path = self.check_fs(file,_mode_writes(mode),f'open({mode!r})')
        return self._guarded(_builtins.open,path,mode,*args,**kwargs)

    def fs_listdir(self,path='.'):
        p = self.check_fs(path,False,'listdir')
        return self._guarded(os.listdir,p)

    def fs_scandir(self,path='.'):
        p = self.check_fs(path,False,'scandir')
        return self._guarded(lambda q:list(os.scandir(q)),p)

    def fs_stat(self,path,**kwargs):
        p = self.check_fs(path,False,'stat')
        return self._guarded(lambda *a,**k:os.stat(*a,**k),p,**kwargs)

    def fs_walk(self,top,*args,**kwargs):
        top = self.check_fs(top,False,'walk')

        def _gen():
            for root,dirs,files in os.walk(top,*args,**kwargs):
                dirs[:] = [d for d in dirs if self.can('fs:read',os.path.join(root,d))]
                yield root,dirs,files
        return _gen()

    def fs_write_call(self,func,what,path,*args,**kwargs):
        p = self.check_fs(path,True,what)
        return self._guarded(func,p,*args,**kwargs)

    def fs_rename(self,src,dst,**kwargs):
        src = self.check_fs(src,True,'rename')
        dst = self.check_fs(dst,True,'rename')
        if kwargs.pop('replace',False):
            return self._guarded(os.replace,src,dst)
        return self._guarded(os.rename,src,dst)

    def fs_probe(self,func,path,*args,**kwargs):
        """只读探测(exists/isfile/isdir/getsize):没权限就安静地返回 False/0。

        探测到处弹窗会很烦;这里不抛异常也不问,只记一条审计。
        """
        if not self.can('fs:read',path):
            self.note('probe_denied','fs:read',path,'只读探测被拒,返回空值')
            return False
        try:
            return func(path,*args,**kwargs)
        except OSError:
            return False

    # ---------------- 网络/进程 ----------------

    def net_guard(self,func,what):
        @functools.wraps(func)
        def wrapper(*args,**kwargs):
            self.check_net(what)
            return self._guarded(func,*args,**kwargs)
        return wrapper

    def proc_guard(self,func,what):
        @functools.wraps(func)
        def wrapper(*args,**kwargs):
            self.check_proc(what)
            return self._guarded(func,*args,**kwargs)
        return wrapper

    # ---------------- 模块门面 ----------------

    def os_module(self):
        wrap = {
            'open':self.fs_open,
            'listdir':self.fs_listdir,
            'scandir':self.fs_scandir,
            'stat':self.fs_stat,
            'walk':self.fs_walk,
            'mkdir':lambda p,*a,**k:self.fs_write_call(os.mkdir,'mkdir',p,*a,**k),
            'makedirs':lambda p,*a,**k:self.fs_write_call(os.makedirs,'makedirs',p,*a,**k),
            'remove':lambda p,*a,**k:self.fs_write_call(os.remove,'remove',p,*a,**k),
            'unlink':lambda p,*a,**k:self.fs_write_call(os.remove,'unlink',p,*a,**k),
            'rmdir':lambda p,*a,**k:self.fs_write_call(os.rmdir,'rmdir',p,*a,**k),
            'removedirs':lambda p,*a,**k:self.fs_write_call(os.removedirs,'removedirs',p,*a,**k),
            'utime':lambda p,*a,**k:self.fs_write_call(os.utime,'utime',p,*a,**k),
            'rename':self.fs_rename,
            'replace':lambda s,d:self.fs_rename(s,d,replace=True),
            'system':self.proc_guard(os.system,'os.system'),
            'popen':self.proc_guard(os.popen,'os.popen'),
            'startfile':self.proc_guard(os.startfile,'os.startfile'),
            'path':self.path_module(),
        }
        allow = {'name','sep','altsep','pathsep','linesep','curdir','pardir','devnull','extsep',
                 'getcwd','getcwdb','getpid','cpu_count','get_terminal_size','fspath','PathLike',
                 'error','stat_result'}
        deny = {'environ','putenv','setenv','unsetenv','getenv','chdir','chroot','chmod','chown',
                'link','symlink','readlink','kill','abort','_exit','fork','forkpty','setsid',
                'setuid','setgid','execv','execve','execl','execle','execlp','execvp','execvpe',
                'execlpe','spawnv','spawnve','spawnl','spawnle','spawnlp','spawnvpe','spawnlpe',
                'posix_spawn','posix_spawnp','add_dll_directory','nice','waitpid','wait',
                'dup','dup2','fdopen','close','openpty','pipe'}
        return _Proxy(self,os,'os',allow=allow,wrap=wrap,deny=deny)

    def path_module(self):
        wrap = {
            'exists':lambda p:self.fs_probe(os.path.exists,p),
            'isfile':lambda p:self.fs_probe(os.path.isfile,p),
            'isdir':lambda p:self.fs_probe(os.path.isdir,p),
            'islink':lambda p:self.fs_probe(os.path.islink,p),
            'getsize':lambda p:self.fs_probe(os.path.getsize,p) or 0,
            'getmtime':lambda p:self.fs_probe(os.path.getmtime,p) or 0,
            'getctime':lambda p:self.fs_probe(os.path.getctime,p) or 0,
            'getatime':lambda p:self.fs_probe(os.path.getatime,p) or 0,
            'realpath':lambda p:self.fs_probe(os.path.realpath,p) or os.path.abspath(str(p)),
        }
        allow = {'join','dirname','basename','splitext','split','splitdrive','abspath',
                 'normpath','normcase','isabs','expanduser','expandvars','commonpath',
                 'commonprefix','relpath','sep','altsep','pathsep','curdir','pardir','extsep'}
        return _Proxy(self,os.path,'os.path',allow=allow,wrap=wrap,
                      deny={'sameopenfile','samestat','samefile','lexists'})

    def sys_module(self):
        wrap = {'argv':list(sys.argv)}
        allow = {'version','version_info','platform','maxsize','float_info','int_info',
                 'byteorder','getdefaultencoding','getfilesystemencoding','getrecursionlimit',
                 'setrecursionlimit','getswitchinterval','stdout','stderr','stdin',
                 'executable','hexversion','api_version','implementation'}
        deny = {'modules','path','meta_path','path_hooks','path_importer_cache','exit','_getframe',
                'settrace','setprofile','addaudithook','breakpointhook','displayhook',
                'excepthook','unraisablehook','__interactivehook__','intern','getrefcount',
                'set_coroutine_origin_tracking_depth','_xoptions','dont_write_bytecode'}
        return _Proxy(self,sys,'sys',allow=allow,wrap=wrap,deny=deny)

    def io_module(self):
        allow = {'BytesIO','StringIO','BufferedReader','BufferedWriter','BufferedRWPair',
                 'TextIOWrapper','UnsupportedOperation','SEEK_SET','SEEK_CUR','SEEK_END',
                 'DEFAULT_BUFFER_SIZE','IOBase','BlockingIOError'}
        return _Proxy(self,io,'io',allow=allow,wrap={'open':self.fs_open})

    def builtins_view(self):
        """import builtins 拿到的是"受控内置"的视图,不是真的 builtins 模块。"""
        ns = self.restricted_builtins()

        class _B:
            def __getattr__(self,item):
                if item in ns:
                    return ns[item]
                raise AttributeError(item)

            def __dir__(self):
                return sorted(ns)
        return _B()

    def image_module(self):
        wrap = {'open':self.guarded_image_open}
        return _Proxy(self,_PILImage,'PIL.Image',wrap=wrap)

    def mutagen_module(self):
        wrap = {
            'File':self.guarded_mutagen_file,
            'flac':_Proxy(self,mutagen.flac,'mutagen.flac',
                          wrap={'FLAC':self.guarded_mutagen_file}),
            'id3':_Proxy(self,mutagen.id3,'mutagen.id3',
                         wrap={'ID3':self.guarded_mutagen_file}),
        }
        return _Proxy(self,mutagen,'mutagen',wrap=wrap)

    def guarded_image_open(self,fp,*args,**kwargs):
        if isinstance(fp,(str,bytes,os.PathLike)):
            fp = self.check_fs(fp,False,'PIL.Image.open')
        return self._guarded(_PILImage.open,fp,*args,**kwargs)

    def guarded_mutagen_file(self,filething,*args,**kwargs):
        if isinstance(filething,(str,bytes,os.PathLike)):
            filething = self.check_fs(filething,False,'读取音频标签')
        return self._guarded(mutagen.File,filething,*args,**kwargs)

    def socket_module(self):
        allow = {'AF_INET','AF_INET6','AF_UNIX','SOCK_STREAM','SOCK_DGRAM','SOL_SOCKET',
                 'SO_REUSEADDR','IPPROTO_TCP','has_ipv6','gaierror','error','timeout',
                 'getdefaulttimeout','setdefaulttimeout','gethostname'}
        wrap = {
            'socket':self.net_guard(socket.socket,'创建 socket'),
            'create_connection':self.net_guard(socket.create_connection,'socket.create_connection'),
            'getaddrinfo':self.net_guard(socket.getaddrinfo,'DNS 查询'),
            'gethostbyname':self.net_guard(socket.gethostbyname,'DNS 查询'),
            'create_server':self.net_guard(socket.create_server,'创建监听'),
        }
        return _Proxy(self,socket,'socket',allow=allow,wrap=wrap)

    def urllib_module(self):
        request = _Proxy(self,urllib.request,'urllib.request',
                         allow={'Request','build_opener','url2pathname'},
                         wrap={'urlopen':self.net_guard(urllib.request.urlopen,'urlopen'),
                               'urlretrieve':self.net_guard(urllib.request.urlretrieve,'urlretrieve')},
                         deny={'urlcleanup','getproxies','proxy_bypass','install_opener',
                               'FancyURLopener','URLopener','pathname2url'})
        return _Proxy(self,urllib,'urllib',
                      extra={'request':request,'parse':urllib.parse})

    def http_module(self):
        client = _Proxy(self,http.client,'http.client',
                        allow={'HTTPConnection','HTTPSConnection','HTTPResponse','responses',
                               'HTTPException','NotConnected','InvalidURL'},
                        wrap={'HTTPConnection':self.net_guard(http.client.HTTPConnection,'HTTP 连接'),
                              'HTTPSConnection':self.net_guard(http.client.HTTPSConnection,'HTTPS 连接')})
        return _Proxy(self,http,'http',wrap={'client':client})

    def subprocess_module(self):
        wrap = {
            'run':self.proc_guard(subprocess.run,'subprocess.run'),
            'Popen':self.proc_guard(subprocess.Popen,'subprocess.Popen'),
            'call':self.proc_guard(subprocess.call,'subprocess.call'),
            'check_call':self.proc_guard(subprocess.check_call,'subprocess.check_call'),
            'check_output':self.proc_guard(subprocess.check_output,'subprocess.check_output'),
        }
        allow = {'PIPE','STDOUT','DEVNULL','SubprocessError','CalledProcessError','TimeoutExpired'}
        return _Proxy(self,subprocess,'subprocess',allow=allow,wrap=wrap)

    def module_builders(self):
        """模块名 -> 门面对象(建一次就缓存:import os 和命名空间里的 os 是同一个)。"""
        if not self._module_cache:
            self._module_cache = {
                'os':self.os_module(),
                'sys':self.sys_module(),
                'io':self.io_module(),
                'socket':self.socket_module(),
                'urllib':self.urllib_module(),
                'http':self.http_module(),
                'subprocess':self.subprocess_module(),
                'PIL':self.image_module(),
                'mutagen':self.mutagen_module(),
                'builtins':self.builtins_view(),
            }
        return self._module_cache

    # 直接放行的模块:它们本身不提供文件/网络/进程能力(不碰路径的那些功能)。
    _PASSTHROUGH = {
        'json':_json,'re':re,'math':math,'random':random,'time':time,
        'datetime':datetime,'collections':collections,'itertools':itertools,
        'functools':functools,'string':string,'textwrap':textwrap,
        'traceback':traceback,'logging':logging,
        'tkinter':tkinter,'tkinter.ttk':tkinter.ttk,
        'tkinter.simpledialog':tkinter.simpledialog,
        'tkinter.messagebox':tkinter.messagebox,
        'tkinter.filedialog':tkinter.filedialog,
        'tkinter.colorchooser':tkinter.colorchooser,
        'tkinter.font':tkinter.font,
        'ttkbootstrap':ttkbootstrap,
    }

    # ---------------- import ----------------

    # 这些包的子模块可以直接给真模块(tkinter.ttk / tkinter.scrolledtext /
    # PIL.ImageDraw…):它们只提供界面/图像工具,本身不含文件、网络、进程能力;
    # 真去读文件的调用由审计钩子兜底。
    _SUBMODULE_PACKAGES = ('tkinter','ttkbootstrap','PIL')

    @staticmethod
    def _dotted_exists(obj,name):
        """门面背后是真模块/真对象:校验 a.b.c 这条链真的存在。

        否则 `import os.environ.x` 这种(真 Python 里根本不存在的模块)也会被放行,
        插件会拿到一个莫名其妙的 os 门面。
        """
        real = getattr(obj,'_real',obj)
        for part in name.split('.')[1:]:
            try:
                real = getattr(real,part)
            except AttributeError:
                return False
        return True

    def _resolve_module(self,name):
        """模块名 -> 插件该拿到的东西(受控门面 / 安全模块 / None=不在白名单)。"""
        root = name.split('.')[0]
        built = self.module_builders().get(root)
        if built is not None:
            if '.' in name and not self._dotted_exists(built,name):
                return None
            return built
        if name in self._PASSTHROUGH:
            return self._PASSTHROUGH[name]
        if root in self._PASSTHROUGH:
            if root in self._SUBMODULE_PACKAGES:
                try:
                    mod = _importlib.import_module(name)
                except Exception:
                    return None
                self.note('import_submodule','*',name,
                          f'{root} 的子模块,只提供界面/工具能力')
                return mod
            return None
        local = self.import_local(root)
        if local is not None:
            return local
        if root in tuple(self.policy.modules):
            try:
                mod = _importlib.import_module(name)
            except Exception as e:
                raise ImportError(f'{name} 导入失败:{e}')
            self.note('import_declared','*',name,'插件在 plugin.json 里声明的模块,不受沙盒代理')
            logging.warning('插件 %s 导入了声明放行的模块 %s(不经沙盒代理)',self.name,name)
            return mod
        return None

    def _attach_fromlist(self,name,obj,fromlist):
        """from X import Y 里 Y 是子模块时,先把它挂到父模块上,否则 getattr 找不到。"""
        root = name.split('.')[0]
        if root not in self._SUBMODULE_PACKAGES:
            return obj
        for item in fromlist:
            if not isinstance(item,str) or item == '*':
                continue
            try:
                present = hasattr(obj,item)
            except SandboxDenied:
                present = False
            if present:
                continue
            try:
                sub = _importlib.import_module(f'{name}.{item}')
            except Exception:
                continue
            real = getattr(obj,'_real',obj)
            try:
                setattr(real,item,sub)
            except Exception:
                pass
        return obj

    def import_module(self,name,globals=None,locals=None,fromlist=(),level=0):
        if not isinstance(name,str) or not name:
            raise SandboxDenied('import 的模块名无效')
        if level:
            self.violation('import','不支持相对 import',name)
            raise SandboxDenied('沙盒里不支持相对 import')
        obj = self._resolve_module(name)
        if obj is None:
            self.violation('import',f'import {name!r} 不在沙盒白名单里',name)
            raise SandboxDenied(
                f'import {name!r} 被插件沙盒拒绝;'
                f'确有需要请在 plugin.json 的 sandbox.modules 里声明')
        if fromlist:
            # from X import a,b:补上子模块,剩下的交给 import 机制去 getattr
            return self._attach_fromlist(name,obj,fromlist)
        # __import__ 的契约:没有 fromlist 的 "import a.b" 必须返回**顶层** a,
        # 这样字节码把 a 绑到名字上之后,a.b 仍能按属性取到。
        # 这里原来直接返回了 a.b,于是 `import tkinter.simpledialog` 会把
        # tkinter 绑成子模块,之后 tkinter.simpledialog.xxx 全都炸 —— 就是这个 bug。
        top = name.split('.')[0]
        if top == name:
            return obj
        topobj = self._resolve_module(top)
        return obj if topobj is None else topobj

    def import_local(self,name):
        """插件自己目录里的模块:用沙盒命名空间执行,不能借它拿到真 os。"""
        if name in self._local_modules:
            return self._local_modules[name]
        path = os.path.join(self.plugin_dir,name+'.py')
        pkg = os.path.join(self.plugin_dir,name,'__init__.py')
        target = path if os.path.isfile(path) else (pkg if os.path.isfile(pkg) else None)
        if target is None:
            return None
        mod = _types.ModuleType(name)
        mod.__dict__['__builtins__'] = self.restricted_builtins()
        mod.__dict__['__sandbox__'] = SandboxView(self)
        mod.__dict__['__file__'] = target
        self._local_modules[name] = mod
        try:
            with self._guarded(_builtins.open,target,'r',encoding='utf-8') as fp:
                code = compile(fp.read(),target,'exec')
            exec(code,mod.__dict__)
        except Exception:
            del self._local_modules[name]
            raise
        return mod

    # ---------------- 受控内置 ----------------

    _SAFE_BUILTINS = (
        'abs','all','any','ascii','bin','bool','bytearray','bytes','callable','chr',
        'classmethod','complex','delattr','dict','divmod','enumerate','filter','float',
        'format','frozenset','getattr','hasattr','hash','hex','id','int','isinstance',
        'issubclass','iter','len','list','map','max','min','next','object','oct','ord',
        'pow','print','property','range','repr','reversed','round','set','setattr','slice',
        'sorted','staticmethod','str','sum','super','tuple','type','vars','zip',
        'ArithmeticError','AssertionError','AttributeError','BaseException','BlockingIOError',
        'BrokenPipeError','BufferError','BytesWarning','ChildProcessError','ConnectionAbortedError',
        'ConnectionError','ConnectionRefusedError','ConnectionResetError','DeprecationWarning',
        'EOFError','EnvironmentError','Exception','FileExistsError','FileNotFoundError',
        'FloatingPointError','FutureWarning','GeneratorExit','IOError','ImportError',
        'ImportWarning','IndentationError','IndexError','InterruptedError','IsADirectoryError',
        'KeyError','KeyboardInterrupt','LookupError','MemoryError','ModuleNotFoundError',
        'NameError','NotADirectoryError','NotImplementedError','OSError','OverflowError',
        'PendingDeprecationWarning','PermissionError','ProcessLookupError','RecursionError',
        'ReferenceError','ResourceWarning','RuntimeError','RuntimeWarning','StopAsyncIteration',
        'StopIteration','SyntaxError','SyntaxWarning','SystemError','TabError','TimeoutError',
        'TypeError','UnboundLocalError','UnicodeDecodeError','UnicodeEncodeError',
        'UnicodeError','UnicodeTranslateError','UnicodeWarning','UserWarning','ValueError',
        'Warning','ZeroDivisionError','__build_class__','__name__','NotImplemented',
        'Ellipsis','True','False','None','copyright','credits','license',
    )

    def restricted_builtins(self):
        b = {}
        for k in self._SAFE_BUILTINS:
            if k in ('True','False','None','__name__'):
                continue
            v = getattr(_builtins,k,None)
            if v is not None:
                b[k] = v
        b['open'] = self.fs_open
        b['__import__'] = self.import_module
        b['eval'] = self.sandbox_eval
        b['exec'] = self.sandbox_exec
        b['compile'] = self.sandbox_compile
        b['globals'] = _builtins.globals
        b['locals'] = _builtins.locals
        b['dir'] = _builtins.dir
        b['input'] = self._no_input
        return b

    def _no_input(self,*args,**kwargs):
        self.violation('builtin','插件里不能用 input()(控制台在播放器手上)',None)
        raise SandboxDenied('沙盒里不允许使用 input()')

    def _scope(self,globals):
        g = globals if isinstance(globals,dict) else self.namespace
        g['__builtins__'] = self.restricted_builtins()
        return g

    def sandbox_eval(self,source,globals=None,locals=None):
        g = self._scope(globals)
        return _builtins.eval(source,g,g if locals is None else locals)

    def sandbox_exec(self,source,globals=None,locals=None):
        g = self._scope(globals)
        return _builtins.exec(source,g,g if locals is None else locals)

    def sandbox_compile(self,source,filename=None,mode='exec',*args,**kwargs):
        if filename is None:
            filename = f'<plugin {self.name} exec>'
        return _builtins.compile(source,filename,mode,*args,**kwargs)

    # ---------------- 命名空间 ----------------

    def _build_namespace(self):
        ns = self.namespace
        ns['__name__'] = 'plugin_'+str(self.env_id)
        ns['__doc__'] = None
        ns['__builtins__'] = self.restricted_builtins()
        ns['__sandbox__'] = SandboxView(self)
        ns['SandboxDenied'] = SandboxDenied    # 插件可以精确地 except 它
        ns['open'] = self.fs_open
        for name,mod in self._PASSTHROUGH.items():
            if '.' not in name:
                ns[name] = mod
        facades = self.module_builders()
        ns['os'] = facades['os']
        ns['sys'] = facades['sys']
        ns['io'] = facades['io']
        ns['Image'] = facades['PIL']
        ns['ImageTk'] = _PILImageTk
        ns['mutagen'] = facades['mutagen']
        ns['plugin_dir'] = self.plugin_dir
        ns['__env_id__'] = self.env_id     # 老插件(如 plugin/nb)在用它
        if self._host is not None:
            ns['pro'] = self._facade or self._host   # 重建命名空间时别把宿主弄丢
        return ns


# ---------------------------------------------------------------- 注册表

class env_box:
    """宿主侧的沙盒注册表:env_id -> SandBox。

    保留 a / get() 的形状,免得插件里已经写下的 pro.env_dict.a[...] 直接失效。
    命名空间一旦被删(plugin/nb/a.py 就试过 del pro.env_dict.a['2']),宿主手里的
    SandBox 对象仍然有效,get()/create() 会按需把它重新登记回来。
    """
    #只能防老实的插件
    #陌生插件还是发给gpt吧

    def __init__(self):
        self.a = {}
        self._boxes = {}
        self.__sealed = False

    def create(self,env_id,name,plugin_dir,policy,_host_token=None):
        """只有宿主能建沙盒/改策略。插件即便拿着 pro.env_dict 也只能取回自己的命名空间。"""
        box = self._boxes.get(env_id)
        if _host_token is not _HOST_TOKEN:
            if box is None:
                raise SandboxDenied(f'只有宿主能创建插件沙盒(env_id={env_id!r})')
            box.violation('create','插件试图改造已有沙盒',env_id)
            self.a[env_id] = box.namespace
            return box
        if box is not None:
            self.a[env_id] = box.namespace      # 自我修复:被删了就重新登记
            if self.__sealed or box.is_sealed():
                # 封存之后谁都不能再改策略,只允许把命名空间登记回来
                return box
            merged = _merge_policy(box.policy,policy,box.name,name)
            if merged is not None:
                box.policy = merged
                box._build_namespace()
                print(f'[沙盒] {name} 与 {box.name} 共用 env_id={env_id},'
                      f'合并后的策略:{merged.describe()}')
            return box
        box = SandBox(env_id,name,plugin_dir,policy)
        self._boxes[env_id] = box
        self.a[env_id] = box.namespace
        return box

    def sandbox(self,env_id):
        return self._boxes.get(env_id)

    def boxes(self):
        return list(self._boxes.values())

    def get(self,env_id):
        box = self._boxes.get(env_id)
        if box is None:
            raise KeyError(f'env_id={env_id!r} 还没有创建沙盒;请先调用 create()')
        self.a[env_id] = box.namespace
        return box.namespace

    def seal(self):
        self.__sealed = True
        for box in self._boxes.values():
            if not box.is_sealed():
                box.seal()
        return len(self._boxes)

    def is_sealed(self):
        return self.__sealed

    def grant(self,*args,_host_token=None,**kwargs):
        """宿主专用:给某个 env 的插件额外授权。插件调用会在这里被拒。"""
        if _host_token is not _HOST_TOKEN:
            for box in self._boxes.values():
                box.violation('grant','插件试图通过 env_dict 给自己授权',None)
            raise SandboxDenied('插件不能给自己授权(只有宿主能)')
        if not args:
            raise SandboxDenied('grant 需要 env_id 和能力名')
        box = self._boxes.get(args[0])
        if box is None:
            raise SandboxDenied(f'没有 env_id={args[0]!r} 的沙盒')
        return box.grant(*args[1:],_host_token=_HOST_TOKEN)


def _merge_policy(old,new,old_name,new_name):
    """共用 env_id 的两个插件合并成一份策略(取并集,并打印提示)。

    共用 env_id 等于自愿共享一个命名空间,所以这里把它们当成同一个信任域;
    真要最小权限,就别让两个插件共用 env_id。
    """
    if old.unsafe or new.unsafe:
        return None if old.unsafe else new
    merged = Policy(old.plugin_dir)
    merged.fs_read = list(dict.fromkeys(list(old.fs_read) + list(new.fs_read)))
    merged.fs_write = list(dict.fromkeys(list(old.fs_write) + list(new.fs_write)))
    merged.net = old.net or new.net
    merged.proc = old.proc or new.proc
    merged.ask = old.ask or new.ask
    merged.modules = list(dict.fromkeys(list(old.modules) + list(new.modules)))
    merged.warnings = [f'与 {old_name} / {new_name} 共用 env_id,策略已合并']
    return merged


# ---------------------------------------------------------------- 宿主门面

class _MenuFacade:
    """只放行"往菜单里加东西"。不给控件对象:控件有 .tk,那就是 Tcl 通道。"""

    __slots__ = ('_sb','_menu')

    _ALLOW = ('add_command','add_separator','add_checkbutton','add_radiobutton')

    def __init__(self,sb,menu):
        self._sb = sb
        self._menu = menu

    def _delegate(self,item):
        # 用闭包包一层,不把绑定方法交出去:绑定方法的 __self__ 是控件,
        # 而控件有 .tk 通道(这条只是抬高门槛,不是边界,见 SANDBOX.md)
        target = getattr(self._menu,item)

        def call(*a,**k):
            return target(*a,**k)
        return call

    def __getattr__(self,item):
        if item in self._ALLOW and self._menu is not None:
            return self._delegate(item)
        self._sb.violation('host_attr',f'插件不能访问 pro.menu.{item}',None)
        raise SandboxDenied(f'插件不能访问 pro.menu.{item}(只能加菜单项)')

    def __repr__(self):
        return f'<sandbox menu for {self._sb.name}>'


class _AppFacade:
    """主窗口门面:白名单,不暴露 tk/call/eval/winfo_children/nametowidget。"""

    __slots__ = ('_sb','_app')

    _ALLOW = ('after','after_idle','after_cancel','title','geometry','deiconify',
              'withdraw','iconify','destroy','update','update_idletasks',
              'bind','unbind','bind_all','unbind_all','resizable','minsize','maxsize',
              'winfo_width','winfo_height','winfo_screenwidth','winfo_screenheight',
              'winfo_x','winfo_y','winfo_exists','attributes')

    def __init__(self,sb,app):
        self._sb = sb
        self._app = app

    def _delegate(self,item):
        target = getattr(self._app,item)

        def call(*a,**k):
            return target(*a,**k)
        return call

    def __getattr__(self,item):
        if item in self._ALLOW and self._app is not None:
            return self._delegate(item)
        self._sb.violation('host_attr',f'插件不能访问 pro.app.{item}',None)
        raise SandboxDenied(
            f'插件不能访问 pro.app.{item}'
            f'(Tk 的 Tcl 通道沙盒挡不住,所以这里只放行白名单方法)')

    def __repr__(self):
        return f'<sandbox app for {self._sb.name}>'


class HostFacade:
    """插件看到的 pro。

    旧版把整个 Tkapp 交给插件,于是 pro.player.music_player(能放任意 URL/文件)、
    pro.app.tk.eval("exec ...")、pro.env_dict(注册表)、pro.plugin_list(别人的命名
    空间)全敞着 —— 等于绕开前面所有能力检查。现在只给这几样:
        pro.menu            加菜单项
        pro.app             白名单方法(窗口标题/几何/after/destroy...)
        pro.music_dict      播放列表的深拷贝快照(改了不影响播放器)
        pro.plugin_names    已加载插件的名字(不给对象)
    """

    __slots__ = ('_sb','_pro','menu','app')

    def __init__(self,sb,pro):
        self._sb = sb
        self._pro = pro
        self.menu = _MenuFacade(sb,getattr(pro,'menu',None))
        self.app = _AppFacade(sb,getattr(pro,'app',None))

    @property
    def music_dict(self):
        try:
            return copy.deepcopy(self._pro.music_dict)
        except Exception:
            logging.exception('取播放列表快照失败,返回空表')
            return {}

    @property
    def plugin_names(self):
        try:
            return [getattr(p,'name','') for p in self._pro.plugin_list]
        except Exception:
            return []

    def __getattr__(self,item):
        self._sb.violation('host_attr',f'插件不能访问 pro.{item}',None)
        raise SandboxDenied(
            f'插件不能访问 pro.{item};可用的是 pro.menu / pro.app / '
            f'pro.music_dict / pro.plugin_names')

    def __repr__(self):
        return f'<sandbox host facade for {self._sb.name}>'


# ---------------------------------------------------------------- 审计钩子

# 门面 / 宿主替插件做事时置位:这期间审计钩子放行(权限已经查过,也避免递归)
_FRAME_LOCAL = threading.local()
_FRAME_TAGS = {}          # '<plugin 名字 ' -> SandBox
_FRAME_DIRS = []          # [(规范化插件目录, SandBox)]
_HOOK_INSTALLED = False

# 实测:CPython 的审计表里没有 os.stat/os.lstat(它们不产生事件),
# 所以"探测文件是否存在/多大"在内省路径下挡不住 —— 这是已知缺口,
# 写在 SANDBOX.md 的"已知的坑"里,别指望这里。
_FS_READ_EVENTS = ('os.listdir','os.scandir')
_FS_WRITE_EVENTS = ('os.mkdir','os.rmdir','os.remove','os.utime','os.chmod',
                    'os.truncate','os.link','os.symlink')
_PROC_EVENTS = ('os.system','os.exec','os.spawn','os.posix_spawn','os.startfile',
                'os.fork','os.forkpty','subprocess.Popen','ctypes.dlopen','pty.spawn')
_NET_EVENTS = ('socket.__new__','socket.connect','socket.bind','socket.getaddrinfo',
               'socket.gethostbyname','socket.sendto')

_O_WRITE_FLAGS = 0
for _flag in ('O_WRONLY','O_RDWR','O_CREAT','O_APPEND','O_TRUNC'):
    _O_WRITE_FLAGS |= getattr(os,_flag,0)


def _facade_enter():
    _FRAME_LOCAL.depth = getattr(_FRAME_LOCAL,'depth',0) + 1


def _facade_exit():
    _FRAME_LOCAL.depth = max(getattr(_FRAME_LOCAL,'depth',1) - 1,0)


def _always_readable(path):
    """解释器自己的目录(site-packages/stdlib)永远可读。

    否则插件界面一碰到 ttkbootstrap/PIL 读自带资源就会被钩子拦下。
    """
    for root in _ALWAYS_READABLE:
        if under(root,path):
            return True
    return False


def register_plugin_frames(box):
    _FRAME_TAGS[box.frame_tag] = box
    _FRAME_DIRS.append((_norm(box.plugin_dir),box))


def _box_for_frame(filename):
    if not filename:
        return None
    for tag,box in _FRAME_TAGS.items():
        if filename.startswith(tag):
            return box
    for root,box in _FRAME_DIRS:
        if root and under(root,filename):
            return box
    return None


def _plugin_box_on_stack():
    """从调用栈里找最近的插件代码帧(找不到就是宿主自己在访问)。"""
    try:
        frame = sys._getframe(1)
    except ValueError:
        return None
    depth = 0
    while frame is not None and depth < 200:
        box = _box_for_frame(frame.f_code.co_filename)
        if box is not None:
            return box
        frame = frame.f_back
        depth += 1
    return None


def _is_pathlike(value):
    return isinstance(value,(str,bytes,os.PathLike))


def _audit_target(event,args):
    """把审计事件翻译成 (能力, 路径, 说明);不关心的返回 None。"""
    a = list(args)
    if event == 'open':
        p = a[0] if a else None
        if not _is_pathlike(p):
            return None
        mode = a[1] if len(a) > 1 else None
        flags = a[2] if len(a) > 2 else None
        write = False
        if isinstance(mode,str):
            write = any(c in mode for c in ('w','a','x','+'))
        if isinstance(flags,int):
            write = write or bool(flags & _O_WRITE_FLAGS)
        return ('fs:write' if write else 'fs:read',os.fspath(p),f'open({mode!r})')
    if event == 'os.rename' or event == 'os.replace':
        paths = [os.fspath(x) for x in a[:2] if _is_pathlike(x)]
        return ('fs:write',paths,event) if paths else None
    if event in _FS_READ_EVENTS:
        p = a[0] if a else None
        return ('fs:read',os.fspath(p),event) if _is_pathlike(p) else None
    if event in _FS_WRITE_EVENTS:
        p = a[0] if a else None
        return ('fs:write',os.fspath(p),event) if _is_pathlike(p) else None
    if event in _NET_EVENTS:
        return ('net',None,event)
    if event in _PROC_EVENTS:
        return ('proc',None,event)
    return None


def _audit_hook(event,args):
    """进程级审计钩子:插件绕开沙盒门面(内省拿真模块)时,这里兜底。"""
    if getattr(_FRAME_LOCAL,'depth',0) and __name__ !="__main__":
        
        return                                  # 门面/宿主自己发起,权限已查过
    _facade_enter()
    try:
        target = _audit_target(event,args)
        if target is None:
            return
        box = _plugin_box_on_stack()
        if box is None or box.policy.unsafe:
            return                              # 不是插件干的,或用户放开了它
        cap,path,what = target
        paths = path if isinstance(path,list) else [path]
        for one in paths:
            if cap == 'fs:read' and one and _always_readable(one):
                continue
            if not box.can(cap,one):
                box.violation('audit',f'绕过沙盒门面的 {what} 被审计钩子拦下',one)
                raise SandboxDenied(
                    f'插件 {box.name} 的 {what} 绕过了沙盒门面,被审计钩子拒绝:{one}')
    except SandboxDenied:
        raise
    except Exception:
        # 钩子自己出问题不能把播放器带崩:记一次日志后放行
        if not getattr(_FRAME_LOCAL,'warned',False):
            _FRAME_LOCAL.warned = True
            logging.exception('沙盒审计钩子内部出错,该事件放行')
    finally:
        _facade_exit()


def install_audit_hook():
    """装一次进程级审计钩子。装了它,插件即便通过内省拿到真的 os/socket,
    碰文件、联网、起进程也会被拦(门面之外的第二道闸)。

    说明:钩子只在"栈上确实有插件代码帧"时才动手,宿主自己的访问不受影响;
    C 扩展直接调系统接口(比如 Tcl 自己 exec)不在审计事件里,仍挡不住。
    """
    global _HOOK_INSTALLED
    if _HOOK_INSTALLED:
        return False
    _HOOK_INSTALLED = True
    sys.addaudithook(_audit_hook)
    logging.info('插件沙盒审计钩子已安装')
    return True


_ALWAYS_READABLE = tuple(_norm(p) for p in
                         (sys.prefix,sys.base_prefix,os.path.dirname(os.__file__))
                         if p)
if __name__ == "__main__":
    env = env_box()
    env_id = 'main'
    m = input('file path:')
    if not os.path.isfile(m):
        exit(1)
    cm = open(m,'r',encoding='utf-8').read()
    b = parse_policy(None,[],'.')
    box = env.create(env_id,'main','.',
                            b,
                            _host_token=_HOST_TOKEN)
    d = box.namespace
    for w in b.warnings:
        print(f'[沙盒] {"."}:{w}')
    
    

    try:
    
        # 先编译:语法错误在这里就能拿到,不用等回调里再炸
        code = compile(cm,f'<file main>','exec')
    except Exception as e:
        logging.exception('编译插件 init 代码失败')
    install_audit_hook()
    try:
    
        exec(code,d)
    except SandboxDenied as e:
        # 被沙盒挡下属于"预期内"的结果:一行干净提示,不吓人
        logging.warning('文件 的 init 被沙盒拦截:%s',e)
        print(f'[沙盒] 文件的 init 被拦截:{e}')
    except Exception as e:
        logging.exception('执行 代码失败')
        print(f'文件错误:{e}')
        traceback.print_exc()
    
