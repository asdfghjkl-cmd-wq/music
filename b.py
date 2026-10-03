from threading import Thread
import tkinter,io,time,random,queue,platform,os,functools,sys,copy

import mutagen.flac
import mutagen.id3
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.environ['PYTHON_VLC_MODULE_PATH'] = f"{BASE_DIR}/pvlc"
from PIL import ImageTk,Image
import traceback,pystray,ttkbootstrap,json,vlc,mutagen
import builtins as _pybuiltins
import logging

# 插件沙盒:能力模型、路径判定、封存与审计都在 plugin_sandbox.py 里。
# env_box 由它提供,替代原先那个把 b.py 的 globals() 整个暴露给插件的实现。
from plugin_sandbox import (env_box, parse_policy, SandboxDenied, _HOST_TOKEN,
                            ASK_YES, ASK_SESSION, ASK_ALWAYS, install_audit_hook)



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
        # 上一次挂监听时用的 (path, end_action)。重扫列表会摘掉监听,
        # 需要靠它把"放完自动切歌"接回去(见 rearm_listen)。
        self._last_listen = None

        self._bad: "set" = set()
        # 已经为哪些曲目弹过"是否不允许播放"的确认框。
        # 这一条是防死循环用的:同一个坏文件如果不记住"问过了",
        # 用户每按一次"否",下面 match 里的重放就会在同一轮里再触发一次
        # EncounteredError,于是弹窗→否→重放→弹窗,永远出不去。
        # 用户手动播放(start_current)时会清空,所以想重新问是能问到的。
        self._prompted: "set" = set()
        # 已经问过并处理完的那一首(按是→拉黑,按否→保留但不自动重放)。
        # 同一首的后续事件一律丢掉,直到用户重新起播才解除。
        self._settled = ''
 
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
                logging.exception('摘除事件监听失败')
                print(f'摘除 {ev} 监听失败(忽略):{e}')

    def _listen(self, path, end_action):
        # 正常放完(EndReached)和打不开/解不开(EncounteredError)都必须接管:
        # 只接前者的话,遇到坏文件自动切歌会永久停在那里,不会跳到下一首。
        self.add_listen(vlc.EventType.MediaPlayerEndReached,
                        self._on_end_reached, path, end_action, self._gen)
        self.add_listen(vlc.EventType.MediaPlayerEncounteredError,
                        self._on_encountered_error, path, end_action, self._gen)
        self._last_listen = (path,end_action)
        self._listening = True


    def rearm_listen(self,end_action=None):
        # flush_music() 重扫列表时会摘掉监听(旧条目已经无效)。如果当前这首
        # 还在新列表里,必须把 EndReached/EncounteredError 接回去,否则这首歌
        # 放完就停住,再也不会自动切歌。路径以新列表为准。
        if self._last_listen is None:
            return False
        if self.music_player.get_state() not in (vlc.State.Playing,vlc.State.Paused):
            return False
        old_path,old_action = self._last_listen
        path = self._playable(self.music_message.get('name',''))
        self._listen(path or old_path,
                     end_action if end_action is not None else old_action)
        return True

    def set_dict_and_tk_obj(self,d:dict,l:"ttkbootstrap.Listbox",ml):
        self.music_dict = d
        self.listb = l
        self.ml = ml
        # 重扫之后已经不在列表里的曲目,没必要继续留在拉黑名单里
        self._bad &= set(d.keys())
        self._prompted &= set(d.keys())
        if self._settled and self._settled not in d:
            self._settled = ''
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
                try:
                    self._advance(path,end_action,failed)
                except Exception :
                    logging.exception('处理播放结束事件失败')
                    
        except queue.Empty:
            pass
        try:
            lb.after(50,self._pump,)
        except tkinter.TclError:
            # 控件已经销毁/不可用:必须放开 _pumping,否则 set_dict_and_tk_obj
            # 之后再也不会重启轮询,队列里的事件就永远没人处理了(静默失效)。
            logging.exception('重新调度事件轮询失败,已暂停轮询')
            self._pumping = False

    def _select_index(self,index):
        lb = self.listb
        if lb is None:
            return
        lb.selection_clear(0,tkinter.END)
        lb.selection_set(index)
        lb.see(index)

    def _ask_keep(self,name,message):
        # 弹"是否不允许播放"的确认框。
        # 返回 True = 用户说不允许播放(该拉黑);False = 允许播放,别拉黑。
        # 取消/关窗(返回值不是"是")按"允许播放"处理,和原来 == '是' 的语义一致。
        # 必须显式传 buttons:这个参数是 keyword-only,漏了会直接 TypeError。
        try:
            ans = ttkbootstrap.Messagebox.yesno(message,'player',buttons=['是','否'])
        except Exception:
            logging.exception('弹出确认框失败,按允许播放处理')
            
            return False
        return ans == '是'

    def _stop(self):
        # stop() 摘监听时会 _gen += 1,但 _pending 里可能还压着同一份坏媒体的
        # EncounteredError 事件。这里再推一次 _gen,让队列里的旧事件在 _pump
        # 里被 gen 检查丢掉 —— 否则"停下来"之后又被旧事件叫起来重放同一首。
        self.stop()
        self._gen += 1

    def _advance(self,path,end_action,failed=False):
        name = self.music_message.get('name') or ''
        # 已经在本次播放里问过并处理过这一首了:
        # VLC 对一个坏文件会连着抛好几个 EncounteredError,而且"回答否→重放"
        # 本身就会再造一个新事件。没有这个闸门,事件会一轮轮喂回来,
        # 弹窗/重放就停不下来 —— 这就是按"否"后无限循环的根。
        # 只有用户手动重新起播(start_current)才会解锁,到时可以再问。
        if name and name == self._settled:
            return
        # 下面收集"这一轮要不要弹窗、弹什么",弹窗只做一次。
        # 按"否"= 允许播放(不拉黑),但事件链必须在这里断掉,不再重放。
        ask = None
        if failed:
            # 打开/解码失败是确定性证据,不用等短播计数攒够三次,直接拉黑
            if name and name not in self._prompted:
                self._prompted.add(name)
                ask = '无法打开音频,可能出现了问题,是否不允许播放'
            self._short_plays = 0
        elif time.monotonic() - self._play_started < 1.0:
            self._short_plays += 1
            if self._short_plays >= 3:
                # 数够了就清零:不管回答是"是"还是"否",都不能带着 3 这个计数
                # 往下走,否则下一次事件又会立刻满足 >=3 再弹一次。
                self._short_plays = 0
                if name and name not in self._prompted:
                    self._prompted.add(name)
                    ask = '音频可能出现问题,是否不允许播放'
        else:
            self._short_plays = 0

        if ask is not None:
            if self._ask_keep(name,ask):
                self._bad.add(name)
                print(f'{name} 无法正常播放,自动切歌不再选它')
                self._set_status('播放失败:文件无法播放')
                self._settled = name
                self._stop()
                if self.play_tt in (1,2):
                    # 拉黑之后没必要再重放这一首:顺序/随机模式直接切下一首。
                    self._advance_to(self._next_seq() if self.play_tt == 1 else self._next_random(),end_action)
                return
            # 回答"否":用户认定文件没问题,允许播放 —— 不拉黑,但这一轮的
            # 事件链到此为止(重放同一个坏文件只会立刻再失败一次,再弹一次框)。
            # 也把弹窗记忆去掉,用户手动重播时可以重新问,不会被静默拉黑。
            self._settled = name
            self._prompted.discard(name)
            print(f'{name} 播放异常,已按用户选择保留,停止自动重放,可手动重试或换一首')
            self._set_status('播放异常:已停止自动重放')
            self._stop()
            return

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
                logging.exception('更新状态文字失败')
                pass
        try:
            w.after(0,_apply)
        except tkinter.TclError:
            logging.exception('投递状态文字更新失败')
            pass

    def start_current(self):
        # 播放已经 set_mrl 好的媒体,并把短播计时的起点重置到此刻。
        # 给 _after_switch 和 Tkapp 用,外部就不必再去改 _play_started 了。
        self.play()
        self._play_started = time.monotonic()
        # 这里是"用户/流程明确要开播当前这一首"的唯一入口(选曲、切模式、
        # 自动切歌成功都会走到),所以在这里清掉弹窗记忆与"已处理"闸门:
        #   - 用户手动重播同一首时,坏文件还能再问一次,不至于永远静默;
        #   - 自动切歌进了新的一首,那一首的弹窗记忆也不需要留着。
        self._prompted.clear()
        self._settled = ''

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
            self.listb.after(50,lambda:(ttkbootstrap.Messagebox.show_warning('未指定文件',parent=self.listb.master)))
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
            self.listb.after(50,lambda:ttkbootstrap.Messagebox.show_warning('未指定文件',parent=self.listb.master))
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
        except Exception:
            logging.exception('读取标签或封面失败')
            traceback.print_exc()
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

# env_box 现在来自 plugin_sandbox.py:每个 env_id 一个沙盒,命名空间里的
# os/sys/io/socket/... 都是受控门面,默认只允许读插件自己的目录。


    


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
            logging.exception('刷新进度条失败')
            self._destroyed = True
            self._after_id = None
            return
        try:
            self._after_id = self.after(self.interval, self._poll)
        except tkinter.TclError:
            logging.exception('重新调度进度条刷新失败')
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
    def __init__(self,dir:str,env_dict:env_box,other_names=None):
        self.env_dict = env_dict
        self.plugin_file_path = os.path.join(dir,'plugin.json')
        n = {}
        try:
            with open(self.plugin_file_path,'r',encoding='utf-8') as fp:
                n = json.load(fp)
        except Exception as e:
            logging.exception('读取插件配置失败')
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
                logging.exception('读取插件 init 文件失败')
                print(f'读取 {init_file} 失败,忽略:{e}')

        self.command = n.get('command','').replace('from b import *','',1)
        command_file = n.get('command_file','')
        if command_file:
            try:
                with open(os.path.join(dir,command_file),'r',encoding="utf-8") as fp:
                    self.command = fp.read().replace('from b import *','',1)
            except Exception as e:
                logging.exception('读取插件 command 文件失败')
                print(f'读取 {command_file} 失败,忽略:{e}')

        self.init_ok = False
        
        self.com = None
        self._box = None                 # init_env() 里由沙盒注册表给出

        name = n.get('name','')
        if isinstance(name,str) and name:
            base = name
            named = True
        else:
            # 没有 name 的插件以前会拿到空 env_id,和别的无名插件共用一份命名空间
            base = os.path.basename(os.path.normpath(dir))
            named = False
        # other_names 是"已经被别的插件占用的名字"(不含自己):重名时依次试
        # name (2)、name (3)……,保证同名插件不会共用一份 env 命名空间
        # (env_id 默认就等于 name)。
        # 注意不能用 set + discard(自己) 的写法:set 会把两个同名声明合并成
        # 一个,再把自己 discard 掉,恰好把"重名"这个证据扔了,结果两个插件
        # 都叫同一个名字 —— 那正是这里要防的事。
        others = set(other_names or ())
        self.name = base
        i = 2
        while self.name in others:
            self.name = f'{base} ({i})'
            i += 1
        if not named:
            print(f'{self.plugin_file_path} 没有可用的 name,改用目录名 {self.name!r}')
        elif self.name != base:
            print(f'{self.plugin_file_path} 的 name {base!r} 已被别的插件占用,'
                  f'改用 {self.name!r}')
        self.can_exec = n.get('can_exec',False)
        self.exec_path = n.get('exec_path',[])
        if not isinstance(self.exec_path,(list,tuple)):
            print(f'插件 {self.name} 的 exec_path 不是数组,按空处理')
            self.exec_path = []
        
        
        self.privilege = n.get('privilege',[])
        if not isinstance(self.privilege,(list,tuple)):
            print(f'插件 {self.name} 的 privilege 不是数组,按空处理')
            self.privilege = []
        
        self.dir = dir
        # 沙盒策略:plugin.json 的 sandbox 段 + 历史 privilege。
        # 解析只做校验:字段写错只会更严,不会变成"不受限制"。
        self.sandbox_policy = parse_policy(n.get('sandbox',None),self.privilege,dir)
        # unsafe 只是插件"申请",要不要放开由宿主在装载时问用户(见 _confirm_unsafe)
        for w in self.sandbox_policy.warnings:
            print(f'[沙盒] {self.plugin_file_path}:{w}')

        self.n = n

        self.env_id = str(self.n.get('env_id',self.name))
    def init_env(self):

        # 命名空间由沙盒提供:默认只给"读插件自己的目录",写/网络/进程都要授权。
        # 建沙盒要带宿主凭据:插件自己调 create 是改不动策略的
        self._box = self.env_dict.create(self.env_id,self.name,self.dir,
                                         self.sandbox_policy,
                                         _host_token=_HOST_TOKEN)
        self._env_dict = self._box.namespace

        
    def init_i(self,tkaapp):
        self._box.attach_host(tkaapp)
        
        if not self.can_exec or self.init_ok:
            return
        
            
        self.init_ok = True
        try:
            
            # 先编译:语法错误在这里就能拿到,不用等回调里再炸
            code = compile(self.init,f'<plugin {self.name} init>','exec')
        except Exception as e:
            logging.exception('编译插件 init 代码失败')
            print(f'插件 {self.name} 的 init 代码无法编译,已跳过:{e}')
            return
        def _run_init():
            # 插件代码出问题只应该影响它自己,不能把整个播放器带崩
            try:

                exec(code,self._env_dict)
            except SandboxDenied as e:
                # 被沙盒挡下属于"预期内"的结果:一行干净提示,不吓人
                logging.warning('插件 %s 的 init 被沙盒拦截:%s',self.name,e)
                print(f'[沙盒] 插件 {self.name} 的 init 被拦截:{e}')
            except Exception as e:
                logging.exception('执行插件 init 代码失败')
                print(f'插件 {self.name} 初始化失败:{e}')
                traceback.print_exc()
        tkaapp.app.after(0,_run_init)


    def run(self,tkaapp=None):
        if not self.can_exec or tkaapp is None:
            return
        self._box.attach_host(tkaapp)
        if not self.com:
            try:
                
                self.com = compile(self.command,f'<plugin {self.name} command>','exec')
            except Exception as e:
                logging.exception('编译插件 command 代码失败')
                print(f'插件 {self.name} 的 command 无法编译,已跳过:{e}')
                return
        
        
        
        def _run_command():
            try:
                exec(self.com,self._env_dict)
            except SandboxDenied as e:
                logging.warning('插件 %s 的 command 被沙盒拦截:%s',self.name,e)
                print(f'[沙盒] 插件 {self.name} 的 command 被拦截:{e}')
            except Exception as e:
                logging.exception('执行插件 command 代码失败')
                print(f'插件 {self.name} 执行失败:{e}')
                traceback.print_exc()
        tkaapp.app.after(0,_run_command)



class Config:

    DEFAULT = {'theme': 'solarized-light', 'exit_way': 0,'allow_plugin':[],
               'plugin_unsafe':[],'plugin_grants':{}}

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
        self.plugin_unsafe = self._default('plugin_unsafe')
        self.plugin_grants = self._default('plugin_grants')
        self.load()
    
    def load(self):
        try:
            with open(self.path,'r',encoding='utf-8') as fp:
                n = json.load(fp)
        except FileNotFoundError:
            n = {}                      
        except (OSError,UnicodeDecodeError,json.JSONDecodeError) as e:
            logging.exception('读取配置文件失败,按默认配置处理')

            print(f'读取 {self.path} 失败,按默认配置处理:{e}')
            n = {}
        if not isinstance(n,dict):
            print(f'{self.path} 的内容不是 JSON 对象,按默认配置处理')
            self._backup_broken_config()
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

        u = n.get('plugin_unsafe',None)
        if isinstance(u,list):
            self.plugin_unsafe = list(dict.fromkeys(x for x in u
                                                  if isinstance(x,str) and x))
        elif u is not None:
            print(f'{self.path} 里的 plugin_unsafe 不是数组,按默认值处理')
            self.plugin_unsafe = self._default('plugin_unsafe')

        g = n.get('plugin_grants',None)
        if isinstance(g,dict):
            # 只收:插件名 -> {fs_read/fs_write: [路径], net/proc: true}
            self.plugin_grants = {}
            for who,item in g.items():
                if not isinstance(who,str) or not who or not isinstance(item,dict):
                    print(f'{self.path} 里的 plugin_grants {who!r} 无效,已忽略')
                    continue
                one = {}
                for key in ('fs_read','fs_write'):
                    v = item.get(key,None)
                    if v is None:
                        continue
                    if isinstance(v,str):
                        v = [v]
                    if not isinstance(v,list):
                        print(f'{self.path} 里 {who} 的 {key} 不是数组,已忽略')
                        continue
                    paths = []
                    for p in v:
                        if isinstance(p,str) and p.strip():
                            paths.append(os.path.normpath(p))
                        else:
                            print(f'{self.path} 里 {who} 的 {key} 有无效项 {p!r},已忽略')
                    if paths:
                        one[key] = paths
                for key in ('net','proc'):
                    v = item.get(key,None)
                    if v is None:
                        continue
                    if not isinstance(v,bool):
                        print(f'{self.path} 里 {who} 的 {key} 不是布尔值,已忽略')
                        continue
                    if v:
                        one[key] = True
                if one:
                    self.plugin_grants[who] = one
        elif g is not None:
            print(f'{self.path} 里的 plugin_grants 不是对象,按默认值处理')
            self.plugin_grants = self._default('plugin_grants')

        try:
            self.exit_way = int(n.get('exit_way',self._default('exit_way')))
        except (TypeError,ValueError):
            logging.exception('解析配置项 exit_way 失败')
            print(f'{self.path} 里的 exit_way 不是数字,按默认值处理')
            self.exit_way = self._default('exit_way')
        if self.exit_way not in (0,1,2):
            print(f'{self.path} 里的 exit_way={self.exit_way} 已失效,按默认值处理')
            self.exit_way = self._default('exit_way')

    def grants_for(self,name):
        """把 config 里记住的授权摊平成 (能力,目标) 列表,装载时喂回沙盒。"""
        g = self.plugin_grants.get(name) or {}
        out = []
        for p in g.get('fs_read',()) or ():
            out.append(('fs:read',p))
        for p in g.get('fs_write',()) or ():
            out.append(('fs:write',p))
        if g.get('net'):
            out.append(('net',None))
        if g.get('proc'):
            out.append(('proc',None))
        return out

    def grant(self,name,cap,target=None):
        """记一条授权:插件运行期点"总是允许"时由播放器调用,随后由 save() 落盘。"""
        g = self.plugin_grants.setdefault(name,{})
        if cap in ('fs:read','fs:write'):
            key = 'fs_read' if cap == 'fs:read' else 'fs_write'
            if not target:
                return g
            # 沙盒的白名单是"目录级"的:这里归一成真正生效的目录,
            # 免得 config.json 里记着一个文件路径、实际放开的却是整个目录
            target = os.path.normpath(os.path.abspath(target))
            if not os.path.isdir(target):
                target = os.path.dirname(target) or target
            g.setdefault(key,[])
            if target not in g[key]:
                g[key].append(target)
        elif cap in ('net','proc'):
            g[cap] = True
        return g

    def _backup_broken_config(self):
        """把无法解析的配置挪到带时间戳的 .bak,备份失败也不影响启动。

        原来这里直接 os.rename(path, path+".bak") 而且没有 try:
        Windows 上 .bak 已存在会抛 FileExistsError,又没人接住,
        结果是坏配置让程序启动即崩。os.replace 覆盖同名文件,不挑平台。
        """
        dst = f'{self.path}.{time.strftime("%Y%m%d-%H%M%S")}.bak'
        try:
            os.replace(self.path,dst)
        except OSError as e:
            logging.exception('备份配置文件失败,忽略')
            print(f'备份 {self.path} 失败(忽略):{e}')

    def save(self):
        # 清掉已经迁移过的旧键,免得它们一直留在配置文件里
        for legacy in ('plugin','sussess_plugin','success_plugin'):
            self._dict.pop(legacy,None)
        self._dict['theme'] = self.theme
        self._dict['exit_way'] = self.exit_way
        self._dict['allow_plugin'] = self.allow_plugin
        self._dict['plugin_unsafe'] = self.plugin_unsafe
        self._dict['plugin_grants'] = self.plugin_grants

        tmp = self.path + '.tmp'
        try:
            with open(tmp,'w',encoding='utf-8') as fp:
                json.dump(self._dict,fp,ensure_ascii=False)
            os.replace(tmp,self.path)
        except OSError as e:
            logging.exception('保存配置文件失败')
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
        self.down_frame_listbox = ttkbootstrap.Listbox(self.down_frame,selectmode=tkinter.SINGLE,exportselection=False,yscrollcommand=self.scrollbar.set)#SINGLE 无法被键盘控制
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

        self.env_dict = env_box()


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
        # 插件按目录名排序后依次装载,assigned_names 记的是"已经分配给别的
        # 插件的名字"。Plugin 用它给自己声明的 name 消歧:同名插件会被改名,
        # 于是不会共用一份 env 命名空间(env_id 默认就等于 name)。
        # 排序只是为了让改名结果稳定、可复现。
        assigned_names = set()
        print('[沙盒] 插件沙盒已启用:默认只允许插件读自己的目录,'
              '写/网络/外部进程需要授权')

        def load_p(n:Plugin):
            n.init_env()
            # 1) 把 config.json 里记住的授权喂回去(装载期、封存前)
            for cap,target in self.config.grants_for(n.name):
                try:
                    n._box.grant(cap,target,_host_token=_HOST_TOKEN)
                except SandboxDenied as e:
                    print(f'[沙盒] 恢复 {n.name} 的授权失败,已忽略:{e}')
            # 2) unsafe 申请要用户确认过才真的放开
            self._confirm_unsafe(n)
            # 3) 运行期要权限时问用户,并把"总是允许"写进 config.json
            n._box.set_ask(self._plugin_ask)
            n._box.set_persist(self._plugin_persist)
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
            for asa in sorted(os.listdir(plugin_dir)):
                mn = os.path.join(plugin_dir,asa)
                if not os.path.isdir(mn):continue
                if not os.path.isfile(os.path.join(mn,'plugin.json')):continue
                n = None
                try:
                    n = Plugin(mn,self.env_dict,assigned_names)
                    assigned_names.add(n.name)
                    if n.name in self.config.allow_plugin:
                        # 沙盒策略在装载期一次定好(见 plugin_sandbox),插件自己改不了
                        load_p(n)
                    else:
                        ask = (f'是否加载 {n.name}\n\n它申请的权限:\n'
                               f'{n.sandbox_policy.describe()}')
                        if ttkbootstrap.Messagebox.yesno(ask,title='插件',
                                                         parent=self.app,buttons=['是','否']) == '是':
                            if ttkbootstrap.Messagebox.yesno('是否默认加载',title='插件',
                                                             parent=self.app,buttons=['是','否']) == '是':
                                self.config.allow_plugin.append(n.name)

                            load_p(n)
                except Exception as e:
                    logging.exception('加载插件失败')
                    # 单个插件出问题不该把整个播放器带崩
                    who = n.name if n is not None else mn
                    print(f'加载插件 {who} 失败,已跳过:{e}')
                    traceback.print_exc()

        # 装载到此结束。插件的 init/command 都是 app.after 投递的,要等 mainloop
        # 才开始跑,所以上面这些装载逻辑一定先于任何插件代码执行。
        # 封存沙盒:策略从此冻结,连宿主都不能再改,插件更不可能给自己加权限。
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
        

    def volume_down(self,e):

        n = self.volume_var.get()
        if n-10<=0:self.volume_var.set(0);self.player.set_volume(0);self.music_label.config(text=f"音量:0")
        else:self.volume_var.set(n-10);self.player.set_volume(n-10);self.music_label.config(text=f"音量:{n-10}")

        return 'break'

    def volume_up(self,e):

        n = self.volume_var.get()
        if n+10>=100:self.volume_var.set(100);self.player.set_volume(100);self.music_label.config(text=f"音量:100")
        else:self.volume_var.set(n+10);self.player.set_volume(n+10);self.music_label.config(text=f"音量:{n+10}")

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

    # 这些控件自己要用方向键做导航/调值,方向键落到它们身上时不该去改播放进度。
    _NAV_CLASSES = ('Listbox','Text','Entry','TEntry','TCombobox','TScale','Scale','TSpinbox')

    def _nav_focus(self):
        # 焦点在"自己要用方向键"的控件上时,方向键让给控件本身:
        # 列表/下拉框要移动选中项,输入框要移动光标,Scale 要左右调值。
        # 之前这里只有一个调用点却没有实现,一按左右键就 AttributeError,
        # 键盘前进/后退 10 秒完全用不了。
        try:
            w = self.app.focus_get()
        except tkinter.TclError:
            return False
        if w is None:
            return False
        try:
            return w.winfo_class() in self._NAV_CLASSES
        except tkinter.TclError:
            return False

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
        # 重扫之后当前这一首可能已经不在列表里了。还在的话把结束监听接回去,
        # 否则"重加载音乐列表"会让正在放的那首歌播完就停,不再自动切歌。
        if self.player.music_message.get('name','') in self.music_dict:
            self.player.rearm_listen(self.flush_display)
        self.down_frame_listbox.selection_clear(0,tkinter.END)


    # ---------------- 插件沙盒的宿主侧 ----------------
    _PLUGIN_CAP_TEXT = {'fs:read':'读取文件','fs:write':'写入文件',
                        'net':'访问网络','proc':'启动外部进程'}

    def _plugin_ask(self,box,cap,target,detail):
        """插件运行期要权限时弹窗问用户;返回值是沙盒约定的答案。"""
        what = self._PLUGIN_CAP_TEXT.get(cap,cap)
        msg = (f'插件「{box.name}」想要{what}。\n\n'
               f'目标:{target or "(整个能力)"}\n'
               f'起因:{detail or "未说明"}\n\n'
               '允许本次 / 本次运行都允许 / 总是允许(写进 config.json) / 拒绝')
        try:
            r = ttkbootstrap.Messagebox.yesno(msg,'插件请求权限',parent=self.app,
                                              buttons=['允许本次','本次运行都允许',
                                                       '总是允许','拒绝'])
        except Exception:
            logging.exception('插件权限询问失败,按拒绝处理')
            return 'no'
        return {'允许本次':ASK_YES,'本次运行都允许':ASK_SESSION,
                '总是允许':ASK_ALWAYS}.get(r,'no')

    def _plugin_persist(self,name,cap,target):
        """用户点了"总是允许":记进 config.json,以后装载自动生效。"""
        self.config.grant(name,cap,target)
        self.config.save()
        print(f'[沙盒] 已记住:{name} 可以使用 {cap} {target or ""}')

    def _confirm_unsafe(self,n):
        """插件声明"不受沙盒限制":只有你确认过(记在 config)才真的放开。"""
        if not n.sandbox_policy.unsafe_requested:
            return False
        if n.name in self.config.plugin_unsafe:
            n._box.set_unsafe(True,_host_token=_HOST_TOKEN)
            print(f'[沙盒] {n.name} 按你之前的确认,完全不受沙盒限制')
            return True
        try:
            ans = ttkbootstrap.Messagebox.yesno(
                f'插件「{n.name}」要求完全不受沙盒限制:\n'
                '它将以播放器的全部权限运行:可以读写任何文件、联网、启动进程。\n\n'
                '是否允许?(允许后会记在 config.json 的 plugin_unsafe 里)',
                '插件请求解除沙盒',parent=self.app,buttons=['是','否']) == '是'
        except Exception:
            logging.exception('插件解除沙盒的询问失败,按拒绝处理')
            ans = False
        if not ans:
            print(f'[沙盒] {n.name} 要求不受限制但你未同意,按受限处理')
            return False
        self.config.plugin_unsafe.append(n.name)
        self.config.save()
        n._box.set_unsafe(True,_host_token=_HOST_TOKEN)
        return True

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
                logging.exception('读取曲目 info.json 失败')
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
                logging.exception('解析曲目 info.json 失败')
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
    # 只在真正启动播放器时配置日志:本文件也会被插件 import,
    # 那时候不该去动调用方的 logging 配置。
    logging.basicConfig(level=logging.INFO,
                        filename=os.path.join(BASE_DIR,'music.log'),
                        encoding='utf-8',
                        format='%(asctime)s %(levelname)s [%(threadName)s] %(message)s')

    # 审计钩子:插件绕开沙盒门面(内省拿到真 os/socket)时的第二道闸
    if install_audit_hook():
        print('[沙盒] 审计钩子已安装:绕开门面的文件/网络/进程访问同样会被拦')

    pro = Tkapp()
    pro.app.focus_get()
    pro.run()

pro:Tkapp
__env_id__:str

if __name__ == "__main__":
    try:
        

        main()
    except Exception:
        logging.exception('程序运行期间发生未捕获异常')

        traceback.print_exc()
        raise SystemExit(1)