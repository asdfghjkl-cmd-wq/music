from threading import Thread
import tkinter,io,time,random,queue,platform,os,functools,sys,copy
import mutagen.flac
import mutagen.id3
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.environ['PYTHON_VLC_MODULE_PATH'] = f"{BASE_DIR}/pvlc"
from PIL import ImageTk,Image
import traceback,pystray,ttkbootstrap,json,vlc,mutagen
import builtins as _pybuiltins




NO_FILE = 'no<>found*.mp3*.mp4..'


class Player:
    def __init__(self,d:dict): 
        params = [
                        "--network-caching=3000",  
                        "--avcodec-hw=any",       
                        "--codec=avcodec",         
                        "--verbose=-1"
                    ]
        self.player= vlc.Instance(" ".join(params))

        self.music_photo = None

        self.play_tt = 0

 
        
        
        self.music_message = {'name': ''}
        self._pending: "queue.Queue" = queue.Queue()
        self._pumping = False

        self._play_started = 0.0
        self._short_plays = 0

        self._bad: "set" = set()
 
        self._gen = 0
        # 当前播放的是条目里的哪一路:'music' 或 'video'。
        # 自动切歌按这个字段取路径,避免在 MV 模式下切到音频文件。
        self.media_key = 'music'
        self.ml = None
        self.music_player = vlc.MediaPlayer(self.player)
        self.music_dict = d

        self.listb: "ttkbootstrap.Listbox | None" = None
        self._listening = False
    def add_listen(self,event,func,*args):
        self.music_player.event_manager().event_attach(event,func,*args)
    def del_listen(self,event):
        self.music_player.event_manager().event_detach(event)
    def play_jump_not_stop(self):
        # 拖动进度条之后,按"上次开始播放的时刻"算很容易被误判成短播
        # (见 _advance 里的三次拉黑),这里用一个远早于任何 time.monotonic()
        # 的值把计时起点顶开。
        self._play_started = -64.0
    def _stop_listen(self):

        self._gen += 1
        if not self._listening:
            return
        self._listening = False
        for ev in (vlc.EventType.MediaPlayerEndReached,
                   vlc.EventType.MediaPlayerEncounteredError):
            try:
                self.del_listen(ev)
            except Exception as e:
                print(f'摘除 {ev} 监听失败(忽略):{e}')

    def _listen(self, path, end_action):
        # 正常放完(EndReached)和打不开/解不开(EncounteredError)都必须接管:
        # 只接前者的话,遇到坏文件自动切歌会永久停在那里,不会跳到下一首。
        self.add_listen(vlc.EventType.MediaPlayerEndReached,
                        self._on_end_reached, path, end_action, self._gen)
        self.add_listen(vlc.EventType.MediaPlayerEncounteredError,
                        self._on_encountered_error, path, end_action, self._gen)
        self._listening = True


    def set_dict_and_tk_obj(self,d:dict,l:"ttkbootstrap.Listbox",ml):
        self.music_dict = d
        self.listb = l
        self.ml = ml
        # 重扫之后已经不在列表里的曲目,没必要继续留在拉黑名单里
        self._bad &= set(d.keys())
        if not self._pumping:
            self._pumping = True
            self._pump()
    def set_play_way(self,aint):
        
        if 0<=aint<=2:self.play_tt = aint
    def _on_end_reached(self,event,path,end_action,gen):
        # VLC 在自己的线程里回调,这里只投队列,真正的切歌在 Tk 线程的 _pump 里做
        self._pending.put((path,end_action,gen,False))

    def _on_encountered_error(self,event,path,end_action,gen):
        # "打不开/解不开"是确定性证据,标记 failed 让 _advance 直接拉黑这一首
        self._pending.put((path,end_action,gen,True))

    def _pump(self):
        
        lb = self.listb
        if lb is None:
            # 列表控件还没准备好。这里必须放开 _pumping,否则 set_dict_and_tk_obj
            # 之后再也不会启动轮询,队列里的事件就永远没人处理了。
            self._pumping = False
            return
        try:
            while True:
                path,end_action,gen,failed = self._pending.get_nowait()
                if gen != self._gen:
                    
                    
                    continue
                self._advance(path,end_action,failed)
        except queue.Empty:
            pass
        try:
            lb.after(50,self._pump,)
        except tkinter.TclError:
            pass                      

    def _select_index(self,index):
        lb = self.listb
        if lb is None:
            return
        lb.selection_clear(0,tkinter.END)
        lb.selection_set(index)
        lb.see(index)

    def _advance(self,path,end_action,failed=False):
        name = self.music_message.get('name') or ''
        if failed:
            # 打开/解码失败是确定性证据,不用等短播计数攒够三次,直接拉黑
            if name and name not in self._bad:
                self._bad.add(name)
                print(f'{name} 打开或解码失败,已跳过并不再自动选择它')
            self._short_plays = 0
        elif time.monotonic() - self._play_started < 1.0:
            self._short_plays += 1
            if self._short_plays >= 3:
                if name and name not in self._bad:
                    self._bad.add(name)
                    print(f'{name} 连续 {self._short_plays} 次无法正常播放,自动切歌不再选它')
        else:
            self._short_plays = 0

        match self.play_tt:
            case 0:       
                if name in self._bad:
                    
                    
                    print(f'{name} 无法播放,单曲循环已停止,请换一首或检查文件')
                    self._set_status('播放失败:文件无法播放')
                    self._gen += 1          
                    return
                ok = self._replay(name,path,end_action)
                self._after_switch(ok,path,end_action)
                
            case 1:
                
                self._advance_to(self._next_seq(),end_action)
                
            case 2:
                
                self._advance_to(self._next_random(),end_action)
            case _:
                print(f'未知播放模式:{self.play_tt}')

            
                        


    def _replay(self,name,path,end_action):
        # 按当前模式重放指定的这一首:音频走 set_media_path,视频走 set_media_path_mv
        if self.media_key == 'video':
            return self.set_media_path_mv(name,path,end_action)
        return self.set_media_path(name,path,end_action)

    def _playable(self,name):
        
        if name in self._bad:
            return ''
        p = self.music_dict.get(name,{}).get(self.media_key,'')
        return p if p and p != NO_FILE else ''

    def _next_seq(self):

        keys = list(self.music_dict.keys())
        if not keys:
            return None
        n = self.music_message.get('name','')
        start = keys.index(n) if n in keys else -1
        for i in range(1,len(keys)+1):
            cand = keys[(start+i) % len(keys)]
            if cand == n:
                # 绕了整整一圈又回到自己:说明确实没有别的可播,
                # 顺序播放不该把自己当成"下一首"反复重放。
                continue
            if self._playable(cand):
                return cand
        return None

    def _next_random(self):

        cand = [x for x in self.music_dict if self._playable(x)]
        n = self.music_message.get('name','')
        if len(cand) > 1 and n in cand:
            cand.remove(n)
        if not cand:
            return None
        return random.choice(cand)
    def stop_listen(self):
        self._stop_listen()
    def _advance_to(self,name,end_action):
        
        # _next_seq/_next_random 已经过滤过拉黑名单,这里再兜一次底:
        # 任何情况下都不自动切到已知放不出来的曲目,否则会来回空转。
        if name in self._bad:
            print(f'{name} 已被标记为无法播放,忽略这次自动切歌')
            name = None
        if name is None:
            print('没有可播放的下一首,自动切歌停止')
            self._set_status('无可播放的下一首')
            self._gen += 1
            return
        path = self._playable(name)
        ok = self._replay(name,path,end_action)
        if ok:
            keys = list(self.music_dict.keys())
            if name in keys:
                self._select_index(keys.index(name))
        self._after_switch(ok,path,end_action)
    def set_volume(self,volume):
        self.music_player.audio_set_volume(volume)
    def _set_status(self,text):
        # 状态文字只有 Tk 线程能碰,保留 after(0) 是为了万一有插件从别的线程调用
        w = self.listb if self.listb is not None else self.ml
        if w is None:
            return
        def _apply():
            ml = self.ml
            if ml is None:
                return
            try:
                ml.config(text=text)
            except tkinter.TclError:
                pass
        try:
            w.after(0,_apply)
        except tkinter.TclError:
            pass

    def start_current(self):
        # 播放已经 set_mrl 好的媒体,并把短播计时的起点重置到此刻。
        # 给 _after_switch 和 Tkapp 用,外部就不必再去改 _play_started 了。
        self.play()
        self._play_started = time.monotonic()

    def _after_switch(self,ok,path,end_action):
        if not ok:
            
            
            
            print(f'切歌失败,自动播放已停止:{path!r}')
            self._set_status('切歌失败')
            return
        self.start_current()
        if end_action is not None:
            end_action()

    def set_media_path_mv(self,name,path,end_action=None):
        if not path or path == NO_FILE:
            ttkbootstrap.Messagebox.show_warning('未指定文件',parent=None)
            return False
        self._stop_listen()            
        self._gen += 1
        self.music_player.set_mrl(path)
        # 标签/封面的来源显式给出:优先用同名音乐文件的标签;
        # 没有可用的音乐文件时传 None(而不是把 NO_FILE 哨兵当路径去读标签),
        # 由 load_message 统一负责重置信息与封面。
        tag_path = self.music_dict.get(name,{}).get('music',None)
        if not tag_path or tag_path == NO_FILE or not os.path.isfile(tag_path):
            tag_path = None
        self.media_key = 'video'
        self.load_message(name,tag_path)
        if end_action is not None:
            self._listen(path,end_action)
        return True


    def set_media_path(self,name:str,path:str,end_action=None):
        
        if not path or path == NO_FILE:
            ttkbootstrap.Messagebox.show_warning('未指定文件')
            return False
        self._stop_listen()            
        self._gen += 1
        self.music_player.set_mrl(path)
        self.media_key = 'music'
        if end_action is not None:
            self._listen(path,end_action)
        self.load_message(name,path)
        return True


    def load_message(self,name:str,path:"str | None"):
        
        
        
        
        self.music_message = {'name': name}
        if self.music_photo:
            self.music_photo.close()
        self.music_photo = Image.open(os.path.join(BASE_DIR,'a.png'))
        if not path or path == NO_FILE:
            # 没有标签来源:只保留默认封面与空信息,不做无意义的标签读取
            return
        try:
            a = mutagen.File(path,easy=True)
            if a is not None:                  
                for aa,b in a.items():
                    self.music_message[aa] = b
            _,b = os.path.splitext(path)
            b:str
            if b.lower() == '.flac':
                audio = mutagen.flac.FLAC(path)
                for pic in audio.pictures:
                    self.music_message['pic_mine'] = pic.mime
                    self.music_photo = Image.open(io.BytesIO(pic.data))
            elif b.lower() == '.mp3':
                tags = mutagen.id3.ID3(path)
                for pic in tags.getall("APIC"):
                    self.music_message['pic_mine'] = pic.mime
                    self.music_photo = Image.open(io.BytesIO(pic.data))
        except Exception:traceback.print_exc()
    def play(self) -> int:
        self._set_status('播放')
        return self.music_player.play()
    def pause(self) -> None:
        self._set_status('暂停')
        self.music_player.pause()
    def stop(self) -> None:
        self._set_status('无')
        self._stop_listen()
        self.music_player.stop()
    def cleanup(self):
        self._stop_listen()
        self.music_player.stop()
        self.music_player.release()
        self.player.release()
    def time_add_ten(self) -> None:
        if self.music_player.get_state() in (vlc.State.Playing,vlc.State.Paused):
            t = self.music_player.get_time()+10000
            length = self.music_player.get_length()
            
            if length > 0:
                t = min(t,length)
            self.music_player.set_time(t)
    def time_minus_ten(self) -> None:
        if self.music_player.get_state() in (vlc.State.Playing,vlc.State.Paused):self.music_player.set_time(max(self.music_player.get_time()-10000,0))
def fmt_time(ms):
    
    s = max(int(ms),0) // 1000
    h,s = divmod(s,3600)
    m,s = divmod(s,60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

class SandBox:
    # 插件环境。按 env_id 一份命名空间,同 env_id 的插件共享(老行为,保留)。
    #
    # 权限生命周期(插件自己提不了权):
    #   1) 只有装载期能授权:grant() 只在 seal() 之前可用;
    #   2) 授权表是私有的(_allowed),对外只给只读快照 allowed();
    #   3) 插件代码真正跑起来之前 seal() 已经调用,此后任何改权限的尝试
    #      (grant/clear/直接改集合)都会抛 SandBoxSealed,而不是静默生效;
    #   4) env_id 冲突由播放器裁决(grant 内部),插件自己说了不算。
    #
    # 边界说明(别把它当安全隔离):插件和播放器在同一进程里,pro 又是播放器
    # 对象,所以插件真想折腾宿主是拦不住的。这个类负责的是:插件拿不到裸的
    # open/eval/exec/__import__,也改不了自己或别人的权限状态(旧版提供
    # close_add/file_add 这些公开方法,任何插件调一句就能给自己解锁),也不会
    # 像以前那样把主程序的 builtins 改坏。真正决定"准不准跑"的是 allow_plugin
    # 白名单和加载时的确认弹窗;要硬隔离只能把插件放进独立进程。
    # _sealed/_allowed 也挡不住铁了心的人去改私有属性,只是把"顺手就能提权"
    # 变成"必须明目张胆地动私有属性"。
    #
    # 没有 import 权限时,插件还能 import 哪些模块。
    # 只列纯计算/画界面用的库:os、sys、subprocess、importlib、socket 这些
    # 能绕过沙箱或直接碰系统的一律不给。
    SAFE_IMPORT = (
        'tkinter','ttkbootstrap','PIL','json','math','random','time','datetime',
        're','collections','itertools','functools','string','copy','queue',
        'threading','traceback','typing','dataclasses','enum','uuid','base64',
        'hashlib','binascii','textwrap','pprint','abc','contextlib','decimal',
        'fractions','statistics','unicodedata','vlc','mutagen',
    )
    # 受限内置里剔掉的名字。open/__import__ 由权限单独决定,
    # 其余几个都是"换个名字就能跳出沙箱"的常见入口,不能留。
    UNSAFE_BUILTIN = (
        'open','__import__','eval','exec','compile','input','breakpoint',
        'exit','quit','help','globals','locals','vars','memoryview','__loader__',
    )

    # 各权限能给到什么:
    #   built  -> 完整内置(仍剔掉能绕回真内置的那几个)
    #   import -> 任意模块都能 import
    #   file   -> 真的 open,并顺带放开 os
    #   no_sandbox -> 不受限的"虚拟"内置,但不外泄宿主的真 globals
    #   no_sandbox_really -> 完整全局环境
    ALL_PRIVILEGE = ('built','import','file','no_sandbox',"no_sandbox_really")

    def __init__(self,privilege=()):
        # 私有:外部和插件只能读 allowed(),不能改
        self.a = {}                     # env_id -> 命名空间(保留原名,插件里有人读)
        self._allowed = {}
        self._sealed = False
        self._seq = 0
        if privilege:
            self.grant(None,privilege)

    # ---- 对外只读视图 ----
    def allowed(self,env_id=None):
        # env_id 给 None 就返回整张表(浅拷贝),给具体 id 就返回那一个的集合
        if env_id is None:
            return {k:set(v) for k,v in self._allowed.items()}
        return set(self._allowed.get(env_id,()))

    def env_ids(self):
        return tuple(self._allowed)

    def report(self,env_id=None):
        # 插件自查用:我现在有哪些权限、少了什么
        have = self.allowed(env_id)
        return {
            'env_id': env_id,
            'privilege': sorted(have),
            'missing': [p for p in self.ALL_PRIVILEGE if p not in have],
            'sealed': self._sealed,
        }

    # ---- 装载期授权(seal 之后一律拒绝) ----
    def grant(self,env_id,privilege):
        # 把一组权限授给 env_id。None 当 env_id 时表示"调用方自己指派",仅供内部/测试。
        if self._sealed:
            raise SandBoxSealed('沙箱已冻结,不能再改权限(权限只能在加载插件时声明)')
        env_id = str(env_id) if env_id is not None else self._auto_id()
        bad = [p for p in privilege if p not in self.ALL_PRIVILEGE]
        if bad:
            raise ValueError(f'未知权限:{bad!r},可用的是 {self.ALL_PRIVILEGE}')
        old = self._allowed.get(env_id,set())
        new = old | set(privilege)
        if old and old != new and not self._same_privilege(env_id,privilege):
            # 同 env_id 被两组不同权限声明:老行为是共享命名空间,那低权限插件
            # 就白拿高权限了。这里不静默并集,而是把后来者挪到独立 id。
            moved = self._auto_id()
            print(f'env_id={env_id!r} 已被权限 {sorted(old)} 占用,'
                  f'权限 {sorted(privilege)} 改用独立环境 {moved!r}')
            env_id = moved
            new = set(privilege)
        self._allowed[env_id] = new
        return env_id

    def seal(self):
        # 装载完成、插件代码开始跑之前调用。之后再改权限就抛异常。
        self._sealed = True
        return self

    def _same_privilege(self,env_id,privilege):
        # 同一插件声明两次同样的权限(或子集)是允许的,不算冲突
        return set(privilege) <= self._allowed.get(env_id,set())

    def _auto_id(self):
        while True:
            self._seq += 1
            cand = f'auto:{self._seq}'
            if cand not in self._allowed and cand not in self.a:
                return cand

    def _safe_import(self,env_id):
        # 受限 __import__:按各自权限决定放不放行。
        # 这个包装函数必须真的去调真正的 __import__,而不是返回现成模块,
        # 否则 import os.path 这种子模块导入会拿到父模块,插件里就张冠李戴了。
        real_import = _pybuiltins.__import__
        def _imp(name,globals=None,locals=None,fromlist=(),level=0):
            # 权限实时查私有表:插件就算把闭包抠出去,查到的还是同一份授权
            allow = self._allowed.get(env_id,set())
            # level>0 是相对导入,没有 __package__ 可依据,直接拒掉
            if level:
                raise ImportError(f'沙箱内不支持相对导入:{name!r}')
            root = name.split('.',1)[0]
            if 'no_sandbox' in allow:
                pass                      # 声明了不受限,这里就不拦
            elif root == 'os' and 'file' in allow:
                pass                      # 有文件权限,顺带放开 os(文件路径操作要用)
            elif root not in self.SAFE_IMPORT and 'import' not in allow:
                raise ImportError(
                    f'当前插件没有 import 权限,不能导入 {name!r}'
                    f'(可在 plugin.json 的 privilege 里加 "import")')
            return real_import(name,globals,locals,fromlist,level)
        return _imp

    def _builtins(self,env_id):
        # 造一份"干净的内置"。以前的做法是删 nm['__builtins__'],但 exec/eval
        # 发现命名空间里没有这个键时会自动塞回真正的 builtins,等于没限制;
        # 而直接删 builtins 模块上的 open/__import__ 又会把整个播放器的主程序
        # 一起弄坏(内建模块属性本来就删不掉,只会抛错)。所以这里走白名单。
        allow = self._allowed.get(env_id,set())
        nm = {}
        all_builtins = vars(_pybuiltins)
        if 'no_sandbox_really' in allow:
            nm.update(all_builtins)
            print(nm.keys())
            return nm
        

        if 'built' in allow or 'no_sandbox' in allow:
            # 有 built 权限:给完整内置(仍然去掉 __import__,换成受控版本)
            nm.update(all_builtins)
        else:
            for k,v in all_builtins.items():
                if k in self.UNSAFE_BUILTIN or k.startswith('__'):
                    continue
                nm[k] = v
        # eval/exec/compile/globals 一律不放:它们能绕开这次替换拿回真内置
        for k in self.UNSAFE_BUILTIN:
            nm.pop(k,None)
        nm['__import__'] = self._safe_import(env_id)
        if 'file' in allow:
            # file 权限才给真正的 open
            nm['open'] = all_builtins['open']
        return nm

    def get(self,name):
        nm = self.a.get(name,None)
        # 注意判 None:命名空间可能是空字典,用 if not nm 会把它当成没建过而反复重建
        if nm is None:
            d = dict(globals())
            # 删掉导入器入口,避免插件顺着它拿回真 import
            for k in ('__loader__','__spec__','__builtins__'):
                d.pop(k,None)
            # 插件只能看到 d 里已有的顶层名字(os、sys 这些照样能用,
            # 因为插件的 `from b import *` 本来就指望它们),但拿不到改成
            # 白名单的内置。os/sys 仍然保留:删掉它们会让插件普遍不可用,
            # 而且它们也是插件作者预期的 API 面。
            # no_sandbox 也不再外泄宿主真 globals:它只是"内置全开",
            # 免得插件改动直接落进播放器的全局命名空间。
            d['__env_id__'] = name
            d['__privilege__'] = frozenset(self._allowed.get(name,()))
            d['__builtins__'] = self._builtins(name)
            nm = d
            self.a[name] = nm
        return nm


class SandBoxSealed(RuntimeError):
    # 沙箱在插件代码跑起来之前就冻结了,之后任何改权限的尝试都抛这个
    pass


class SeekBar(ttkbootstrap.Frame):
    SCALE_MAX = 1000        
    def __init__(self, master, media_player: "vlc.MediaPlayer",
                 interval: int = 500, owner=None, **kw):
        super().__init__(master, **kw)
        self.media_player = media_player
        self.interval = interval
        self.owner = owner          

        self._dragging = False
        self._after_id = None
        self._destroyed = False
        self._total = 0

        self.scale = ttkbootstrap.Scale(self, from_=0, to=self.SCALE_MAX,
                               orient="horizontal")
        self.scale.pack(side="left", fill="x", expand=True)

        self.label = ttkbootstrap.Label(self, text="00:00 / 00:00",
                               width=13, anchor="e")
        self.label.pack(side="right", padx=(6, 0))

        
        self.scale.bind("<ButtonPress-1>", self._on_press)
        self.scale.bind("<ButtonRelease-1>", self._on_release)
        self.scale.configure(command=self._on_scale_move)

        
        self.bind("<Destroy>",self._on_destroy)
        self._start_poll()

    def _on_destroy(self,event):
        if event.widget is not self:
            return
        self._destroyed = True
        if self._after_id is not None:
            try:self.after_cancel(self._after_id)
            except Exception:pass
            self._after_id = None

    
    def _on_press(self, _event):
        self._dragging = True

    def _on_release(self, _event):
        self._dragging = False
        if self._total > 0:
            ratio = self.scale.get() / self.SCALE_MAX
            self.media_player.set_time(int(self._total * ratio))
            
            
            self.owner.play_jump_not_stop()

    def _on_scale_move(self, value):
        
        if not self._dragging or self._total <= 0:
            return
        ratio = float(value) / self.SCALE_MAX
        cur = int(self._total * ratio)
        self.label.config(text=f"{fmt_time(cur)} / {fmt_time(self._total)}")

    
    def _start_poll(self):
        self._poll()

    def _poll(self):
        if self._destroyed:
            return
        try:
            self._tick()
        except tkinter.TclError:
            
            self._destroyed = True
            self._after_id = None
            return
        try:
            self._after_id = self.after(self.interval, self._poll)
        except tkinter.TclError:
            self._destroyed = True
            self._after_id = None

    def _tick(self):
        mp = self.media_player
        if self._dragging:
            return
        self._total = mp.get_length() or 0
        cur = mp.get_time() or 0

        if self._total > 0:
            self.scale.set(cur / self._total * self.SCALE_MAX)
            self.label.config(text=f"{fmt_time(cur)} / {fmt_time(self._total)}")
        else:
            self.label.config(text="00:00 / 00:00")


class Plugin:
    def __init__(self,dir:str,env_dict:SandBox,other_names=None):
        self.env_dict = env_dict
        self.plugin_file_path = os.path.join(dir,'plugin.json')
        n = {}
        try:
            with open(self.plugin_file_path,'r',encoding='utf-8') as fp:
                n = json.load(fp)
        except Exception as e:
            print(f'读取 {self.plugin_file_path} 失败,按空配置处理:{e}')
            traceback.print_exc()
        if not isinstance(n,dict):
            print(f'{self.plugin_file_path} 的内容不是 JSON 对象,按空配置处理')
            n = {}
        
        self.init = n.get('init','').replace('from b import *','',1)
        init_file = n.get('init_file','')
        if init_file:
            try:
                with open(os.path.join(dir,init_file),'r',encoding="utf-8") as fp:
                    self.init = fp.read().replace('from b import *','',1)
            except Exception as e:
                print(f'读取 {init_file} 失败,忽略:{e}')

        self.command = n.get('command','')
        command_file = n.get('command_file','')
        if command_file:
            try:
                with open(os.path.join(dir,command_file),'r',encoding="utf-8") as fp:
                    self.command = fp.read()
            except Exception as e:
                print(f'读取 {command_file} 失败,忽略:{e}')

        self.init_ok = False
        
        self.com = None

        name = n.get('name','')
        if isinstance(name,str) and name:
            self.name = name
            if other_names is not None:
                # other_names 是所有插件的名字(含自己),所以先把自己摘掉:
                # 只有真的被别人占了才改名,免得没重名也加后缀
                others = set(other_names)
                others.discard(name)
                i = 2
                while self.name in others:
                    self.name = f'{name} ({i})'
                    i += 1
                if self.name != name:
                    print(f'{self.plugin_file_path} 的 name {name!r} 与别的插件重名,'
                          f'改用 {self.name!r}')
        else:
            # 没有 name 的插件以前会拿到空 env_id,和别的无名插件共用一份命名空间
            base = os.path.basename(os.path.normpath(dir))
            self.name = base
            if other_names is not None:
                others = set(other_names)
                i = 2
                while self.name in others:
                    self.name = f'{base} ({i})'
                    i += 1
            print(f'{self.plugin_file_path} 没有可用的 name,改用目录名 {self.name!r}')
        self.can_exec = n.get('can_exec',False)
        self.exec_path = n.get('exec_path',[])
        if not isinstance(self.exec_path,(list,tuple)):
            print(f'插件 {self.name} 的 exec_path 不是数组,按空处理')
            self.exec_path = []
        
        
        self.privilege = n.get('privilege',[])
        if not isinstance(self.privilege,(list,tuple)):
            print(f'插件 {self.name} 的 privilege 不是数组,按空处理')
            self.privilege = []
        self.privilege = [p for p in self.privilege if p in SandBox.ALL_PRIVILEGE]
        self.n = n
        # env_id 只是插件作者给的"共享命名空间的名字",不再是权限钥匙:
        # 权限由播放器用 grant() 单独记,同 env_id 也不会白拿别人的权限
        self.env_id = str(self.n.get('env_id',self.name))
    def init_env(self):
        
        self._env_dict = self.env_dict.get(self.env_id)

        
    def init_i(self,tkaapp):
        self._env_dict['pro'] = tkaapp
        
        if not self.can_exec or self.init_ok:
            return
        self.init_ok = True
        try:
            # 先编译:语法错误在这里就能拿到,不用等回调里再炸
            code = compile(self.init,f'<plugin {self.name} init>','exec')
        except Exception as e:
            print(f'插件 {self.name} 的 init 代码无法编译,已跳过:{e}')
            return
        def _run_init():
            # 插件代码出问题只应该影响它自己,不能把整个播放器带崩
            try:
                exec(code,self._env_dict)
            except Exception as e:
                print(f'插件 {self.name} 初始化失败:{e}')
                traceback.print_exc()
        tkaapp.app.after(0,_run_init)


    def run(self,tkaapp=None):
        if not self.can_exec or tkaapp is None:
            return
        self._env_dict['pro'] = tkaapp
        if not self.com:
            try:
                self.com = compile(self.command,f'<plugin {self.name} command>','exec')
            except Exception as e:
                print(f'插件 {self.name} 的 command 无法编译,已跳过:{e}')
                return
        
        
        
        def _run_command():
            try:
                exec(self.com,self._env_dict)
            except Exception as e:
                print(f'插件 {self.name} 执行失败:{e}')
                traceback.print_exc()
        tkaapp.app.after(0,_run_command)



class Config:

    DEFAULT = {'theme': 'solarized-light', 'exit_way': 0,'allow_plugin':[]}

    @classmethod
    def _default(cls,key):
        """取默认值的副本,避免 DEFAULT 里的可变对象(如白名单列表)被泄漏出去后就地改写"""
        return copy.deepcopy(cls.DEFAULT[key])

    def __init__(self):
        self.path = os.path.join(BASE_DIR,'config.json')
        self._dict = copy.deepcopy(self.DEFAULT)
        self.theme = self._default('theme')
        self.exit_way = self._default('exit_way')
        self.allow_plugin = self._default('allow_plugin')
        self.load()

    def load(self):
        try:
            with open(self.path,'r',encoding='utf-8') as fp:
                n = json.load(fp)
        except FileNotFoundError:
            n = {}                      
        except (OSError,UnicodeDecodeError,json.JSONDecodeError) as e:

            print(f'读取 {self.path} 失败,按默认配置处理:{e}')
            n = {}
        if not isinstance(n,dict):
            print(f'{self.path} 的内容不是 JSON 对象,按默认配置处理')
            n = {}
        self._dict = n

        t = n.get('theme',None)
        p = n.get('allow_plugin',None)
        if p is None:
            # 兼容历史配置里的旧键名,避免用户的白名单被静默清空
            for legacy in ('plugin','sussess_plugin','success_plugin'):
                if legacy in n:
                    print(f'{self.path} 使用了旧键 {legacy},已按 allow_plugin 处理')
                    p = n[legacy]
                    break
        self.theme = t if isinstance(t,str) and t else self._default('theme')

        if isinstance(p,list):
            bad = [x for x in p if not (isinstance(x,str) and x)]
            if bad:
                print(f'{self.path} 里的 allow_plugin 有 {len(bad)} 个无效项,已忽略:{bad!r}')
            # 空列表是合法值:表示不自动加载任何插件
            self.allow_plugin = [x for x in p if isinstance(x,str) and x]
        else:
            if p is not None:
                print(f'{self.path} 里的 allow_plugin 不是数组,按默认值处理')
            self.allow_plugin = self._default('allow_plugin')

        try:
            self.exit_way = int(n.get('exit_way',self._default('exit_way')))
        except (TypeError,ValueError):
            print(f'{self.path} 里的 exit_way 不是数字,按默认值处理')
            self.exit_way = self._default('exit_way')
        if self.exit_way not in (0,1,2):
            print(f'{self.path} 里的 exit_way={self.exit_way} 已失效,按默认值处理')
            self.exit_way = self._default('exit_way')

    def save(self):
        # 清掉已经迁移过的旧键,免得它们一直留在配置文件里
        for legacy in ('plugin','sussess_plugin','success_plugin'):
            self._dict.pop(legacy,None)
        self._dict['theme'] = self.theme
        self._dict['exit_way'] = self.exit_way
        self._dict['allow_plugin'] = self.allow_plugin

        tmp = self.path + '.tmp'
        try:
            with open(tmp,'w',encoding='utf-8') as fp:
                json.dump(self._dict,fp,ensure_ascii=False)
            os.replace(tmp,self.path)
        except OSError as e:
            print(f'保存 {self.path} 失败:{e}')


class Tkapp:
    def __init__(self):
        
        self.theme_var = None
        self.hwnd = None
        # <Configure> 去抖用的 after id,以及封面 PhotoImage 的缓存键
        # (避免窗口里任何一点尺寸变化都重建一次封面图)
        self._resize_after = None
        self._cover_src = None
        self._cover_size = -1
        self.music_dict = {}
        self.player = Player(self.music_dict)
        self.index: "int | None" = None     
        self.app= ttkbootstrap.Tk('music')   

        # 这里原先有一段"Python < 3.10 就提示升级"的检查,但本文件用了 match 语句,
        # 低版本解释器在解析阶段就会 SyntaxError,那段代码永远执行不到,故删除。
        # 结论:本程序需要 Python >= 3.10,只能由启动方式/文档来保证。
        self.app.bind_class('Button',"<space>",self.play_stop)
        self.app.protocol('WM_DELETE_WINDOW',self.on_closing)
        self.app.title('music')
        self.app.geometry("800x600+20+200")
        self.app.rowconfigure(0, weight=10)
        self.app.rowconfigure(1, weight=1)
        self.app.rowconfigure(2, weight=10)
        self.app.columnconfigure(0, weight=3)
        self.app.columnconfigure(1, weight=1)
        self.config = Config()

        self.style = ttkbootstrap.Style()
        names = self.style.theme_names()
        theme = self.config.theme if self.config.theme in names else 'solarized-light'
        if theme in names:
            self.style.theme_use(theme)
        else:
            print(f'没有可用的主题:{theme}')
        self.raw_image_obj  = Image.open(os.path.join(BASE_DIR,'a.png'))
        
        self._tk_image_obj = ImageTk.PhotoImage(self.raw_image_obj.resize((16,16)))
        self.app.iconphoto(True,self._tk_image_obj)

        
        
        self._ui_queue: "queue.Queue" = queue.Queue()
        self.icon_menu =  pystray.Menu(pystray.MenuItem('显示主界面',lambda:self._ui_queue.put('show'),default=True),
                                       pystray.MenuItem('退出',lambda:self._ui_queue.put('quit'))
                                       )
        self.backround_icon = pystray.Icon('music',self.raw_image_obj,'music player',self.icon_menu)
        self.music_player_icon = Thread(target=self.backround_icon.run,daemon=True)
        self.music_player_icon.start()
        self._pump_ui()

        
        self.information_frame = ttkbootstrap.Frame(self.app)
        self.information_frame.grid(row=0,column=0,sticky='nsew')
        
        self.Control_frame = ttkbootstrap.Frame(self.app)
        self.Control_frame.grid(row=0,column=1)

        self.volume_var = tkinter.IntVar(value=100)
        self.music_label = ttkbootstrap.Label(self.Control_frame,text='音量:100')
        self.music_volume = ttkbootstrap.Scale(self.Control_frame,from_=0,to=100,value=100,variable=self.volume_var,command=self.set_volume)

        self.music_label.pack()
        self.music_volume.pack()
        self.play_status_label = ttkbootstrap.Label(self.Control_frame,text="无")
        self.play_status_label.pack()

        self.play_button = ttkbootstrap.Button(self.Control_frame,text='播放',command=self.play)
        self.pause_button = ttkbootstrap.Button(self.Control_frame,text='暂停',command=self.pause)
        self.play_button.pack(side='top')
        self.pause_button.pack(side='top')
        self.player_mode_var = tkinter.StringVar(value='单曲循环')
        self.music_mode_combobox_obj = ttkbootstrap.Combobox(self.Control_frame,height=3,width=10,values=['单曲循环','顺序播放','随机播放'],textvariable=self.player_mode_var,state='readonly')
        self.music_mode_combobox_obj.pack(side='bottom')
        self.music_mode_combobox_obj.bind("<<ComboboxSelected>>",self.change_mode)

        self.Progress_bar = ttkbootstrap.Frame(self.app)
        self.Progress_bar.grid(row=1,column=0,columnspan=2,sticky='ew',padx=8,pady=4)
        self.Progress_bar.columnconfigure([0,2],weight=1)
        self.Progress_bar.columnconfigure(1,weight=10)

        self.seek = SeekBar(self.Progress_bar,self.player.music_player,owner=self.player)
        self.seek.grid(row=0,column=1,sticky='ew')
        self.music_minus_ten_button = ttkbootstrap.Button(self.Progress_bar,text='⏪',command=self.player.time_minus_ten)
        self.music_minus_ten_button.grid(row=0,column=0)
        self.music_add_ten_button = ttkbootstrap.Button(self.Progress_bar,text='⏩️',command=self.player.time_add_ten)
        self.music_add_ten_button.grid(row=0,column=2)

        self.information_frame.rowconfigure([0,1],weight=1)
        self.information_frame.columnconfigure([0,1],weight=1)

        ph = ImageTk.PhotoImage(Image.open(os.path.join(BASE_DIR,'a.png')).resize((int(600/4),int(600/4))))
        self.music_image = ttkbootstrap.Label(self.information_frame,image=ph)
        self.music_keep_not_clean = ph
        self.music_name = ttkbootstrap.Label(self.information_frame)
        self.music_maker_name = ttkbootstrap.Label(self.information_frame)

        self.music_image.grid(row=0,column=0,rowspan=2,sticky='nsew')
        self.music_name.grid(row=0,column=1,sticky='nsew')
        self.music_maker_name.grid(row=1,column=1,sticky='nsew')

        self.down_frame = ttkbootstrap.Frame(self.app)
        self.down_frame.rowconfigure(0,weight=1)
        self.down_frame.columnconfigure(0,weight=40)
        self.down_frame.columnconfigure(1,weight=1)
        self.down_frame.grid(row=2,column=0,columnspan=2,sticky='nsew')
        
        self.scrollbar = ttkbootstrap.Scrollbar(self.down_frame)
        self.down_frame_listbox = ttkbootstrap.Listbox(self.down_frame,selectmode=tkinter.SINGLE,exportselection=False,yscrollcommand=self.scrollbar.set)
        self.scrollbar.config(command=self.down_frame_listbox.yview)
        self.down_frame_listbox.grid(row=0,column=0,sticky='nsew')
        self.app.bind(sequence="<space>",func=self.play_stop)
        self.down_frame_listbox.bind(sequence="<space>",func=self.play_stop)
        self.menu = ttkbootstrap.Menu(self.app)


        
        self.music_mode_combobox_obj.bind('<Down>',self.volume_down)
        self.app.bind('<Down>',self.volume_down)
        self.app.bind('<Up>',self.volume_up)
        self.app.bind('<Left>',self.time_minus_ten)
        self.app.bind('<Right>',self.time_add_ten)
        self.app.bind("<Configure>", self.on_resize)

        self.env_dict = SandBox()


        self.menu.add_command(label='重加载音乐列表',command=self.flush_music)

        self.menu.add_command(label='设置',command=self.setting)
        
        
        self.app.config(menu=self.menu)

        self.music_mode = True
        
        self.menu.add_command(label='mv',command=self.mv_play)
        self.menu.add_command(label='music',command=self.back_music)
        self.menu.add_separator()
        self.screen = ttkbootstrap.Canvas(self.app)

        self.plugin_list = []
        self.run_play_list = []
        self.run_pause_list = []
        self.run_listbox_list = []

        plugin_dir = os.path.join(BASE_DIR,'plugin')
        # 先扫一遍插件名:Plugin 用它校验/消歧自己声明的 name,
        # 免得两个插件同名时抢同一份命名空间和权限
        plugin_names = set()
        if os.path.isdir(plugin_dir):
            for asa in os.listdir(plugin_dir):
                mn = os.path.join(plugin_dir,asa)
                if os.path.isdir(mn) and os.path.isfile(os.path.join(mn,'plugin.json')):
                    try:
                        with open(os.path.join(mn,'plugin.json'),'r',encoding='utf-8') as fp:
                            pn = json.load(fp)
                        if isinstance(pn,dict) and pn.get('name'):
                            plugin_names.add(pn['name'])
                    except Exception as e:
                        print(f'读取 {mn} 的 plugin.json 失败,按无名字处理:{e}')

        def load_p(n:Plugin):
            n.init_env()
            n.init_i(self)
            self.plugin_list.append(n)
            
            def _bind(fn, _app=self):
                return functools.partial(fn, _app)
            
            if 'play' in n.exec_path:
                self.run_play_list.append(_bind(n.run))
            if 'pause' in n.exec_path:
                self.run_pause_list.append(_bind(n.run))
            if 'listbox' in n.exec_path:
                self.run_listbox_list.append(_bind(n.run))


        self.scrollbar.grid(row=0,column=1,sticky='n')
        self.down_frame_listbox.bind('<<ListboxSelect>>', self.change_music)
        if os.path.isdir(plugin_dir):
            for asa in os.listdir(plugin_dir):
                mn = os.path.join(plugin_dir,asa)
                if not os.path.isdir(mn):continue
                if not os.path.isfile(os.path.join(mn,'plugin.json')):continue
                n = None
                try:
                    n = Plugin(mn,self.env_dict,plugin_names)
                    if n.name in self.config.allow_plugin:
                        # 权限只在装载期授予,而且由播放器自己调,插件碰不到
                        self.env_dict.grant(n.env_id,n.privilege)
                        load_p(n)
                    else:
                        if ttkbootstrap.Messagebox.yesno(f'是否加载{n.name}',title='插件',
                                                         parent=self.app,buttons=['是','否']) == '是':
                            if ttkbootstrap.Messagebox.yesno('是否默认加载',title='插件',
                                                             parent=self.app,buttons=['是','否']) == '是':
                                self.config.allow_plugin.append(n.name)
                            self.env_dict.grant(n.env_id,n.privilege)
                            load_p(n)
                except Exception as e:
                    # 单个插件出问题不该把整个播放器带崩
                    who = n.name if n is not None else mn
                    print(f'加载插件 {who} 失败,已跳过:{e}')
                    traceback.print_exc()

        # 装载结束:从此权限表冻结。这行必须在任何插件代码执行之前,
        # 因为插件的 init/command 都是 app.after 投递的,要等 mainloop 才开始跑。
        self.env_dict.seal()



        self.config.save()
        music_dir = os.path.join(BASE_DIR,'music')
        if not os.path.isdir(music_dir):
            
            os.makedirs(music_dir,exist_ok=True)
        self.flush_music()
    def on_resize(self,event):
        # 注意:子控件的 <Configure> 也会冒到 toplevel 的 bindtags 上,
        # 不判断 event.widget 的话,标签文字变一下就会触发一次封面缩放。
        if event.widget is not self.app:
            return
        if self._resize_after is not None:
            try:
                self.app.after_cancel(self._resize_after)
            except tkinter.TclError:
                pass
        self._resize_after = self.app.after(120,self._do_resize)

    def _do_resize(self):
        self._resize_after = None
        self.flush_display()

    def _select_name(self,name):
        # 让列表高亮当前正在播放的条目
        # (程序化 selection_set 不会触发 <<ListboxSelect>>,所以不会弹确认框)
        keys = list(self.music_dict.keys())
        if name not in keys:
            self.index = None
            return
        self.index = keys.index(name)
        self.down_frame_listbox.selection_clear(0,tkinter.END)
        self.down_frame_listbox.selection_set(self.index)
        self.down_frame_listbox.see(self.index)
    def cleanup_and_exit(self):
        self.app.destroy()
        self.backround_icon.stop()
        self.player.cleanup()
        sys.exit(0)

    def play_stop(self,e):
        n = self.player.music_player.get_state()
        if n == vlc.State.Playing:
            self.pause()
        elif n == vlc.State.Paused:
            self.play()
        return 'break'
        
    def _nav_focus(self):
        # 焦点在列表框/下拉框里时,方向键应该留给控件自己用。
        # toplevel 的绑定排在控件的类绑定之后照样会执行,所以必须自己挡一下,
        # 否则在列表里按上下键会同时改音量、按左右键会同时快进/快退。
        try:
            w = self.app.focus_get()
        except (KeyError,tkinter.TclError):
            return False
        return w is self.down_frame_listbox or w is self.music_mode_combobox_obj

    def volume_down(self,e):
        if self._nav_focus():
            return None
        n = self.volume_var.get()
        if n-10<=0:self.volume_var.set(0);self.music_label.config(text=f"音量:0")
        else:self.volume_var.set(n-10);self.music_label.config(text=f"音量:{n-10}")

        return 'break'

    def volume_up(self,e):
        if self._nav_focus():
            return None
        n = self.volume_var.get()
        if n+10>=100:self.volume_var.set(100);self.music_label.config(text=f"音量:100")
        else:self.volume_var.set(n+10);self.music_label.config(text=f"音量:{n+10}")

        return 'break'

    def _pump_ui(self):

        try:
            while True:
                cmd = self._ui_queue.get_nowait()
                if cmd == 'show':
                    self.app.deiconify()
                elif cmd == 'quit':
                    self.cleanup_and_exit()
                    return              
        except queue.Empty:
            pass
        except tkinter.TclError:
            return                      
        try:
            self.app.after(100,self._pump_ui)
        except tkinter.TclError:
            pass
    _EXIT_DIRECT = 0
    _EXIT_TRAY = 1
    _EXIT_ASK = 2

    def time_add_ten(self,event):
        if self._nav_focus():
            return None
        self.player.time_add_ten()
        return 'break'
    def time_minus_ten(self,event):
        if self._nav_focus():
            return None
        self.player.time_minus_ten()
        return 'break'
    def _show_exit_dialog(self) -> int:

        r = ttkbootstrap.Messagebox.yesnocancel(
            '请选择退出方式：直接退出 / 仅关闭窗口 / 取消',
            '退出',
            parent=self.app,
            buttons=['直接退出', '仅关闭窗口', '取消'],
        )
        if r == '直接退出' or r is True:
            return self._EXIT_DIRECT
        if r == '仅关闭窗口':
            return self._EXIT_TRAY
        return -1

    def on_closing(self):
        way = self.config.exit_way
        if way == self._EXIT_ASK:
            way = self._show_exit_dialog()
        if way == self._EXIT_DIRECT:
            self.cleanup_and_exit()
        elif way == self._EXIT_TRAY:
            self.app.withdraw()


    def setting(self):
        a = ttkbootstrap.Toplevel('setting',size=(250,250))
        v = tkinter.IntVar()
        v.set(self.config.exit_way)
        ttkbootstrap.Radiobutton(a, text="直接退出", variable=v, value=0).pack(anchor="w")
        ttkbootstrap.Radiobutton(a, text="仅关闭窗口", variable=v, value=1).pack(anchor="w")
        ttkbootstrap.Radiobutton(a, text="每次询问", variable=v, value=Tkapp._EXIT_ASK).pack(anchor="w")
        self.theme_var: tkinter.StringVar = tkinter.StringVar(value=self.style.theme_use())
        na = ttkbootstrap.Combobox(a,textvariable=self.theme_var,values=self.style.theme_names(),state='readonly')
        na.pack(anchor='w')
        def save():
            self.config.exit_way = v.get()
            self.config.theme = na.get()
            self.style.theme_use(self.config.theme)
            self.config.save()
            a.destroy()
        ttkbootstrap.Button(a,text='保存',command=save).pack()

    def set_volume(self,a):
        b = round(float(a))
        self.music_label.config(text=f"音量:{b}")
        self.player.set_volume(b)
    def play(self):
        for xnn in self.run_play_list:xnn()
        
        self.player.play()
    def pause(self):
        for xnn in self.run_pause_list:xnn()
        self.player.pause()

    def mv_play(self):
        self.music_mode = False
        self.information_frame.grid_remove()
        self.player.stop_listen()
        self.screen.grid(row=0,column=0,sticky='nsew')
        if platform.system() == "Windows":
            self.hwnd = self.screen.winfo_id()
            self.player.music_player.set_hwnd(self.hwnd)
        elif platform.system() == "Linux":
            self.player.music_player.set_xwindow(self.screen.winfo_id())
        elif platform.system() == "Darwin":
            # 未在 macOS 上验证:python-vlc 这里要的是 NSView*,Tk 的 window id 未必能用
            self.player.music_player.set_nsobject(self.screen.winfo_id())
        x = self.player.music_message.get('name','')
        video = (self.music_dict.get(x) or {}).get('video',NO_FILE)
        self.player.stop()
        
        if x and video != NO_FILE:
            # 与音频模式保持一致:选中当前条目,设置好媒体之后立刻开始播放
            if self.player.set_media_path_mv(x,video,self.flush_display):
                self._select_name(x)
                self.player.start_current()
        
    def back_music(self):
        self.music_mode = True
        self.screen.grid_remove()
        self.information_frame.grid()
        x = self.player.music_message.get('name','')
        self.player.stop()
        music = (self.music_dict.get(x) or {}).get('music',NO_FILE)
        if x and music != NO_FILE:
            # 从 MV 切回音频:同样选中条目并直接播放
            if self.player.set_media_path(x,music,self.flush_display):
                self._select_name(x)
                self.player.start_current()
    def flush_display(self):
        msg = self.player.music_message
        src = self.player.music_photo
        if src is not None:
            size = max(self.app.winfo_height() // 4, 32)
            # 只有封面换了或者目标尺寸变了才重建 PhotoImage:
            # 这个方法会被切歌回调和 <Configure> 频繁触发,重建一次的代价不小。
            if src is not self._cover_src or size != self._cover_size:
                self._cover_src,self._cover_size = src,size
                self.music_keep_not_clean = ImageTk.PhotoImage(src.resize((size, size)))
                self.music_image.config(image=self.music_keep_not_clean)

        def tag_text(key):
            
            
            v = msg.get(key)
            if not v:v = ['unknow']
            if isinstance(v,str):v = [v]
            return ','.join(str(x) for x in v if x) or 'unknow'

        self.music_name.config(text=f'标题:{tag_text("title")}')
        self.music_maker_name.config(text=f'作者:{tag_text("artist")}')

    def change_mode(self,event):
        a = self.player_mode_var.get()
        match a:
            case '单曲循环':self.player.set_play_way(0)
            case '顺序播放':self.player.set_play_way(1)
            case '随机播放':self.player.set_play_way(2)
            case _:print(f'未知播放模式:{a}')


    def change_music(self,event):
        for xnn in self.run_listbox_list:xnn()
        sel = self.down_frame_listbox.curselection()
        if not sel:return      
        idx = int(sel[0])
        a = self.down_frame_listbox.get(idx)
        if self.music_mode:
            file = self.music_dict.get(a,{}).get("music",NO_FILE)

            
            if not file or file == NO_FILE:
                ttkbootstrap.Messagebox.show_warning('未指定文件',parent=self.app)
                self._restore_selection()
                return

            aaa = ttkbootstrap.Messagebox.yesno('是否切换音乐','music player',parent=self.app,buttons=['是','否'])
            if aaa == '是':
                self.player.stop()
                if self.player.set_media_path(a,file,self.flush_display):
                    self.index = idx
                    self.player.start_current()
                    self.flush_display()
                else:
                    
                    self._restore_selection()
            else:
                self._restore_selection()
        else:
            file = self.music_dict.get(a,{}).get("video",NO_FILE)
            
            
            if not file or file == NO_FILE:
                ttkbootstrap.Messagebox.show_warning('未指定文件',parent=self.app)
                self._restore_selection()
                return
            
            aaa = ttkbootstrap.Messagebox.yesno('是否切换视频','music player',parent=self.app,buttons=['是','否'])
            if aaa == '是':
                self.player.stop()
                if self.player.set_media_path_mv(a,file,self.flush_display):
                    self.index = idx
                    self.player.start_current()

                else:
                    
                    self._restore_selection()
            else:
                self._restore_selection()
        self.down_frame_listbox.focus_set()
    def _restore_selection(self):
        self.down_frame_listbox.selection_clear(0, tkinter.END)
        if self.index is not None and 0 <= self.index < self.down_frame_listbox.size():
            self.down_frame_listbox.selection_set(self.index)


    def flush_music(self):
        self.music_dict=self.load_music()
        keys = list(self.music_dict.keys())
        self.player.set_dict_and_tk_obj(self.music_dict,self.down_frame_listbox,self.play_status_label)
        self.down_frame_listbox.delete(0,tkinter.END)
        for asa in keys:
            self.down_frame_listbox.insert(tkinter.END,asa)
        
        
        self.index = None
        self.player.stop_listen()
        self.down_frame_listbox.selection_clear(0,tkinter.END)


    @staticmethod
    def load_music():
        aaa = {}
        music_dir = os.path.join(BASE_DIR,'music')
        for a in sorted(os.listdir(music_dir)):
            b = os.path.join(music_dir,a)
            if not os.path.isdir(b):
                continue
            info = os.path.join(b,'info.json')
            if not os.path.isfile(info):
                continue

            try:
                with open(info,'r',encoding='utf-8') as fp:
                    n:dict = json.load(fp)
            except Exception as e:
                print(f'读取 {info} 失败:{e}')
                continue
            if not isinstance(n,dict):
                print(f'{info} 的内容不是 JSON 对象,跳过 {a}')
                continue

            try:
                name = n.get('name') or a
                fn = n.get('file','') or NO_FILE
                mv = n.get('mv','') or NO_FILE
                if not isinstance(name,str):
                    print(f'{info} 里的 name 不是字符串,改用目录名 {a}')
                    name = a
                if not isinstance(fn,str) or not isinstance(mv,str):
                    print(f'{info} 里的 file/mv 不是字符串,忽略这两个字段')
                    fn = fn if isinstance(fn,str) else ''
                    mv = mv if isinstance(mv,str) else ''
            except Exception as e:
                print(f'解析 {info} 失败,跳过 {a}:{e}')
                continue
            if not name:
                print(f'跳过 {a}:名称为空')
                continue
            music = os.path.join(b,fn) if fn and os.path.isfile(os.path.join(b,fn)) else NO_FILE
            video = os.path.join(b,mv) if mv and os.path.isfile(os.path.join(b,mv)) else NO_FILE
            if music == NO_FILE and video == NO_FILE:
                print(f'跳过 {a}:没有可播放的文件')
                continue

            key = name
            i = 2
            while key in aaa:
                key = f'{name} ({i})'
                i += 1
            aaa[key] = {'music':music,'video':video}
        return aaa

    def run(self):
        self.app.mainloop()


def main():

    pro = Tkapp()
    pro.run()

pro:Tkapp
__env_id__:str

if __name__ == "__main__":
    try:
        

        main()
    except Exception:

        traceback.print_exc()
        raise SystemExit(1)