import os
import platform
import queue
import random
import time
import traceback

import mutagen.flac
import mutagen.id3
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.environ['PYTHON_VLC_MODULE_PATH'] = f"{BASE_DIR}/pvlc"
from PIL import ImageTk,Image
import ttkbootstrap,json,tkinter,vlc,mutagen,io


class player:
    def __init__(self,d:dict): # type: ignore
        params = [
                        "--network-caching=3000",  
                        "--avcodec-hw=any",       
                        "--codec=avcodec",         
                        "--verbose=-1"
                    ]
        self.player= vlc.Instance(" ".join(params))
        self.music_message = {}
        self.music_photo = None

        self.play_tt = 0

        # EndReached 是在 libvlc 的事件线程里回调的,而 Tk 不是线程安全的,
        # 所以回调只往这个线程安全队列里塞数据,由主线程的 _pump() 取出来再切歌。
        self._pending: "queue.Queue" = queue.Queue()
        self._pumping = False
        # 开始播放的时刻,以及"连续播不到 1 秒就结束"的次数,用来识别根本没播出来的
        # 曲目(见 _advance)。单次可能是用户快进到结尾,连续多次才是真坏。
        self._play_started = 0.0
        self._short_plays = 0
        # 立刻 EndReached 的曲目(损坏/占位文件)记在这里,自动切歌时跳过,
        # 否则 VLC 会不停地"播完-切下一首",形成无限快速切歌。
        self._bad: "set" = set()

        self.music_player = vlc.MediaPlayer(self.player)
        self.music_dict = d
        # 注意:这里必须真正赋值,只写注解不会创建属性,
        # 否则 set_d() 之前访问 self.listb 会 AttributeError
        self.listb: "ttkbootstrap.Listbox | None" = None
    def add_listen(self,event,func,*args):
        self.music_player.event_manager().event_attach(event,func,*args)
    def del_listen(self,event):
        self.music_player.event_manager().event_detach(event)
    def set_d(self,d:dict,l:"ttkbootstrap.Listbox"):
        self.music_dict = d
        self.listb = l
        # 有 listb 之后才能在主线程里排期消费队列。只启动一次,避免重复轮询。
        if not self._pumping:
            self._pumping = True
            self._pump()
    def set_a(self,aint):
        # play_tt 只有 0/1/2 三种有实现,允许 3 会变成"切了模式但行为不变"
        if 0<=aint<=2:self.play_tt = aint
    def play_backbround_listen(self,event,path,end_action):
        """
        MediaPlayerEndReached 的回调,运行在 libvlc 的事件线程里。
        libvlc 不可重入,而且这个回调【不在 Tk 主线程】—— 原来这里直接调
        lb.after(...),那本身就是一次跨线程的 tkinter 调用,属于未定义行为。
        现在只往线程安全队列里塞一条数据,真正的切歌交给主线程的 _pump()。
        """
        self._pending.put((path,end_action))

    def _pump(self):
        """在主线程里排期,消费 vlc 事件线程投递过来的切歌请求。"""
        lb = self.listb
        if lb is None:
            return
        try:
            while True:
                path,end_action = self._pending.get_nowait()
                self._advance(path,end_action)
        except queue.Empty:
            pass
        try:
            lb.after(50,self._pump)
        except tkinter.TclError:
            pass                      # 控件已销毁,停止轮询

    def _select_index(self,index):
        lb = self.listb
        if lb is None:
            return
        lb.selection_clear(0,tkinter.END)
        lb.selection_set(index)
        lb.see(index)

    def _advance(self,path,end_action):
        """真正切歌,由 _pump() 在 Tk 主线程里排期调用。"""
        # 开始播放不到 1 秒就 EndReached:这一首很可能根本没播出来(损坏/占位文件),
        # 而 VLC 会立刻再报一次,不加这道闸就是无限快速切歌(CPU 打满 + 刷屏)。
        # 但用户手动快进到结尾也会出现同样现象,所以连续 3 次才判定为坏文件并跳过,
        # 单次只当偶发,避免把一首好歌误判掉。
        if time.monotonic() - self._play_started < 1.0:
            self._short_plays += 1
            if self._short_plays >= 3:
                bad = self.music_message.get('name','')
                if bad and bad not in self._bad:
                    self._bad.add(bad)
                    print(f'{bad} 连续 {self._short_plays} 次无法正常播放,自动切歌不再选它')
        else:
            self._short_plays = 0

        # 同一事件类型只需要一个回调:先摘掉旧的,再由 set_media_path 按新参数重新注册。
        # 少了这一步,EndReached 监听就是一次性的,切完这一首就再也不会自动切歌。
        self.del_listen(vlc.EventType.MediaPlayerEndReached)



        match self.play_tt:
            case 0:       
                name = self.music_message.get('name','')
                if name in self._bad:
                    # 单曲循环遇到播不出来的文件:重播只会立刻又 EndReached,
                    # 直接停下并提示,不要陷入无限重试。
                    print(f'{name} 无法播放,单曲循环已停止,请换一首或检查文件')
                    return
                ok = self.set_media_path(name,path,end_action)
                self._after_switch(ok,path,end_action)
                
            case 1:
                # 顺序播放:从当前这首往后绕一圈找第一个能播的,而不是只试一首。
                self._advance_to(self._next_seq(),end_action)
                
            case 2:
                # 随机播放:只在能播的条目里抽,并且排除当前这首。
                self._advance_to(self._next_random(),end_action)
            case _:
                print(f'未知播放模式:{self.play_tt}')

            
                        


    def _playable(self,name):
        """该条目可播放的文件路径,没有则返回空串。"""
        if name in self._bad:
            return ''
        p = (self.music_dict.get(name) or {}).get('music','')
        return p if p and p != 'no<>found' else ''

    def _next_seq(self):
        """顺序播放的下一首:从当前这首往后绕一圈,跳过没文件或播不出来的条目。
        原来只试一首,一旦那首缺文件,就把监听挂到了一个永远不会再响的 path 上
        (那是【上一首】的路径),自动切歌从此静默失效。"""
        keys = list(self.music_dict.keys())
        if not keys:
            return None
        n = self.music_message.get('name','')
        start = keys.index(n) if n in keys else -1
        for i in range(1,len(keys)+1):
            cand = keys[(start+i) % len(keys)]
            if self._playable(cand):
                return cand
        return None

    def _next_random(self):
        """随机播放:只从能播的条目里抽,并排除当前这首 ——
        否则会"随机到自己",表现为按了没反应。"""
        cand = [x for x in self.music_dict if self._playable(x)]
        n = self.music_message.get('name','')
        if len(cand) > 1 and n in cand:
            cand.remove(n)
        if not cand:
            return None
        return random.choice(cand)

    def _advance_to(self,name,end_action):
        """切到指定曲目。name 为 None 表示曲库里已经找不到可播的了。"""
        if name is None:
            print('没有可播放的下一首,自动切歌停止')
            return
        path = self._playable(name)
        ok = self.set_media_path(name,path,end_action)
        if ok:
            keys = list(self.music_dict.keys())
            if name in keys:
                self._select_index(keys.index(name))
        self._after_switch(ok,path,end_action)

    def _after_switch(self,ok,path,end_action):
        if not ok:
            # 目标文件在这一瞬间变得不可用(极少见)。原来这里拿"上一首"的 path
            # 重新挂监听,而那个文件已经播完、播放器也没在播,监听永远不会再触发 ——
            # 自动切歌从此静默失效。现在改成明确报错,不再挂一个假的监听。
            print(f'切歌失败,自动播放已停止:{path!r}')
            return
        self.play()
        self._play_started = time.monotonic()
        end_action()

    def set_media_path_mv(self,name,path):
        # 目前没有调用点(MV 模式还没接入),保留给后面用
        if not path or path == 'no<>found':
            ttkbootstrap.Messagebox.show_warning('未指定文件',path)
            return False
        self.music_player.set_mrl(path)
        mini = (self.music_dict.get(name) or {}).get('music',None)
        if mini:
            self.load_message(name,mini)
        else:self.music_photo = Image.open(os.path.join(BASE_DIR,'a.png'))
        return True


    def set_media_path(self,name,path,end_action):
        """设置要播放的文件。返回 False 表示没有可播放的文件。"""
        if not path or path == 'no<>found':
            ttkbootstrap.Messagebox.show_warning('未指定文件')
            return False
            
        self.music_player.set_mrl(path)
        
        # 每次切歌都重新挂一次 EndReached;调用方(_advance)已经先摘掉旧的了
        self.add_listen(vlc.EventType.MediaPlayerEndReached,self.play_backbround_listen,path,end_action)
        self.load_message(name,path)
        return True

    def load_message(self,name,path):
        # 先把状态重置成"当前这首歌"的默认值。原来 music_message 只在 try 内部赋值,
        # 而 mutagen.File 对非音频文件是【返回 None】而不是抛异常,于是 a.items() 抛的
        # AttributeError 被后面的 except 吞掉,music_message / music_photo 就留在了上一首 ——
        # 界面会显示上一首的标题和封面,顺序播放还会拿旧歌名去算下一首,导致跳错甚至打转。
        self.music_message = {'name': name}
        self.music_photo = Image.open(os.path.join(BASE_DIR,'a.png'))
        try:
            a = mutagen.File(path,easy=True)
            if a is not None:                  # None 表示不是 mutagen 认识的音频格式
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
        except Exception as w:print(w) 
    def play(self) -> int:return self.music_player.play()
    def pause(self) -> None:self.music_player.pause()
    def stop(self) -> None:
        self.music_player.stop()
        self.del_listen(vlc.EventType.MediaPlayerEndReached)

    def set_time(self,time):"""time's Unit is seconds""";self.music_player.set_time(time*1000)
    def time_add_ten(self) -> None:
        if self.music_player.get_state() in (vlc.State.Playing,vlc.State.Paused):
            t = self.music_player.get_time()+10000
            length = self.music_player.get_length()
            # 不越过结尾:越界时 VLC 会直接报 EndReached,表现成"快进一下就跳歌"
            if length > 0:
                t = min(t,length)
            self.music_player.set_time(t)
    def time_minus_ten(self) -> None:
        if self.music_player.get_state() in (vlc.State.Playing,vlc.State.Paused):self.music_player.set_time(max(self.music_player.get_time()-10000,0))

            
def fmt_time(ms):
    """毫秒 -> mm:ss（超过一小时用 h:mm:ss）"""
    s = max(int(ms),0) // 1000
    h,s = divmod(s,3600)
    m,s = divmod(s,60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class SeekBar(ttkbootstrap.Frame):
    """
    可拖动的播放进度条。
    - 拖动中：只更新文字预览，不 seek，避免卡顿
    - 松手后：执行 seek
    - 非拖动时：定时轮询播放器位置自动刷新
    刻度用 0~1000 归一化，避免 maximum 动态变化
    """

    SCALE_MAX = 1000        # 归一化刻度上限

    def __init__(self, master, media_player: "vlc.MediaPlayer",
                 interval: int = 500, **kw):
        super().__init__(master, **kw)
        self.media_player = media_player
        self.interval = interval

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

        # 用户操作
        self.scale.bind("<ButtonPress-1>", self._on_press)
        self.scale.bind("<ButtonRelease-1>", self._on_release)
        self.scale.configure(command=self._on_scale_move)

        # 窗口销毁时要停掉轮询,否则会在已销毁的控件上继续 after -> TclError
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

    # ---------- 用户拖动 ----------
    def _on_press(self, _event):
        self._dragging = True

    def _on_release(self, _event):
        self._dragging = False
        if self._total > 0:
            ratio = self.scale.get() / self.SCALE_MAX
            self.media_player.set_time(int(self._total * ratio))

    def _on_scale_move(self, value):
        # 程序 set() 也会触发这里，用 _dragging 区分
        if not self._dragging or self._total <= 0:
            return
        ratio = float(value) / self.SCALE_MAX
        cur = int(self._total * ratio)
        self.label.config(text=f"{fmt_time(cur)} / {fmt_time(self._total)}")

    # ---------- 自动刷新 ----------
    def _start_poll(self):
        self._poll()

    def _poll(self):
        if self._destroyed:
            return
        mp = self.media_player
        if not self._dragging:
            self._total = mp.get_length() or 0
            cur = mp.get_time() or 0

            if self._total > 0:
                if not self.scale.state():          # 未禁用时才更新
                    self.scale.set(cur / self._total * self.SCALE_MAX)
                self.label.config(
                    text=f"{fmt_time(cur)} / {fmt_time(self._total)}")
            else:
                self.label.config(text="00:00 / 00:00")

        self._after_id = self.after(self.interval, self._poll)


class plugin:
    def __init__(self,dir):
        # 读配置整体容错:文件缺失、编码不是 utf-8(中文 Windows 上手写配置很常见)、
        # json 语法错、没权限,都不该把整个程序启动带崩。原来 if 不成立时 n 根本没绑定,
        # 下一行 n.get 直接 UnboundLocalError,而调用点在 tkapp.__init__ 的插件循环里
        # 且没有 try,于是启动就整个失败了。
        cfg = os.path.join(dir,'plugin.json')
        n = {}
        try:
            with open(cfg,'r',encoding='utf-8') as fp:
                n = json.load(fp)
        except Exception as e:
            print(f'读取 {cfg} 失败,按空配置处理:{e}')
        if not isinstance(n,dict):
            print(f'{cfg} 的内容不是 JSON 对象,按空配置处理')
            n = {}
        
        self.init = n.get('init','')
        init_file = n.get('init_file','')
        if init_file:
            # 插件的外部代码文件同样不可信:缺文件/编码错都只跳过,不该让启动崩掉
            try:
                with open(os.path.join(dir,init_file),'r',encoding="utf-8") as fp:
                    self.init = fp.read()
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

        self.name = n.get('name','')
        self.can_exec = n.get('can_exec',False)
        self.exec_path = n.get('exec_path',[])
        # 插件用独立的命名空间:既能拿到 b.py 里的名字,又不会把插件写的东西
        # 塞进 b.py 的全局命名空间;init 和 run 共用同一个 dict,
        # 插件在 init 里定义的东西在 run 时还在。
        self.kk = dict(globals())

        
    def init_i(self,tkaapp):
        self.kk['pro'] = tkaapp
        
        if self.can_exec and not self.init_ok:tkaapp.app.after(0,lambda:exec(compile(self.init,"<string>","exec"),self.kk));self.init_ok=True


    def run(self,tkaapp=None):
        self.kk['pro'] = tkaapp
        if not self.can_exec:
            return
        if not self.com:
            self.com = compile(self.command,"<string>","exec")
        # 这一行原来漏了 lambda:after(0,exec(...)) 里的 exec 在实参求值阶段就同步跑完了,
        # after 收到的是它的返回值 None,而 after(ms,None) 会静默退化成一次空定时器 ——
        # 既不报错,也没有延后到主线程。
        tkaapp.app.after(0,lambda:exec(self.com,self.kk))

class tkapp:
    def __init__(self):
        self.music_dict = {}
        self.player = player(self.music_dict)
        self.index: "int | None" = None     # 当前选中项(整数索引,不是 curselection 返回的元组)
        self.app= ttkbootstrap.Tk('music')   # 第二个参数是主题名,'64' 不是合法主题(会被忽略)
        
        self.app.title('music')
        self.app.geometry("800x600+20+200")
        self.app.rowconfigure(0, weight=10)
        self.app.rowconfigure(1, weight=1)
        self.app.rowconfigure(2, weight=10)
        self.app.columnconfigure(0, weight=3)
        self.app.columnconfigure(1, weight=1)
        self.a = ttkbootstrap.Style()
        if 'solarized-light' in self.a.theme_names():
            self.a.theme_use('solarized-light')

        # PhotoImage 必须留下引用,否则对象被回收后窗口图标会消失
        self._icon_image = ImageTk.PhotoImage(Image.open(os.path.join(BASE_DIR,'a.png')).resize((16,16)))
        self.app.iconphoto(True,self._icon_image)

        self.dis_f = ttkbootstrap.Frame(self.app)
        self.dis_f.grid(row=0,column=0,sticky='nsew')

        self.but_f = ttkbootstrap.Frame(self.app)
        self.but_f.grid(row=0,column=1)

        self.play_button = ttkbootstrap.Button(self.but_f,text='播放',command=self.play)
        self.pause_button = ttkbootstrap.Button(self.but_f,text='暂停',command=self.pause)
        self.play_button.pack(side='top')
        self.pause_button.pack(side='top')
        self.cpls = tkinter.StringVar(value='单曲循环')
        self.cpl = ttkbootstrap.Combobox(self.but_f,height=3,width=10,values=['单曲循环','顺序播放','随机播放'],textvariable=self.cpls,state='readonly')
        self.cpl.pack(side='bottom')
        self.cpl.bind("<<ComboboxSelected>>",self.change_mode)

        self.c_f = ttkbootstrap.Frame(self.app)
        self.c_f.grid(row=1,column=0,columnspan=2,sticky='ew',padx=8,pady=4)
        self.c_f.columnconfigure([0,2],weight=1)
        self.c_f.columnconfigure(1,weight=10)

        self.seek = SeekBar(self.c_f,self.player.music_player)
        self.seek.grid(row=0,column=1,sticky='ew')
        self.atb = ttkbootstrap.Button(self.c_f,text='⏪',command=self.player.time_minus_ten)
        self.atb.grid(row=0,column=0)
        self.mtb = ttkbootstrap.Button(self.c_f,text='⏩️',command=self.player.time_add_ten)
        self.mtb.grid(row=0,column=2)

        self.dis_f.rowconfigure([0,1],weight=1)
        self.dis_f.columnconfigure([0,1],weight=1)

        ph = ImageTk.PhotoImage(Image.open(os.path.join(BASE_DIR,'a.png')).resize((int(600/4),int(600/4))))
        self.music_image = ttkbootstrap.Label(self.dis_f,image=ph)
        self.lll = ph
        self.music_name = ttkbootstrap.Label(self.dis_f)
        self.music_maker_name = ttkbootstrap.Label(self.dis_f)

        self.music_image.grid(row=0,column=0,rowspan=2,sticky='nsew')
        self.music_name.grid(row=0,column=1,sticky='nsew')
        self.music_maker_name.grid(row=1,column=1,sticky='nsew')

        self.df = ttkbootstrap.Frame(self.app)
        self.df.rowconfigure(0,weight=1)
        self.df.columnconfigure(0,weight=40)
        self.df.columnconfigure(1,weight=1)
        self.df.grid(row=2,column=0,columnspan=2,sticky='nsew')

        self.scrollbar = ttkbootstrap.Scrollbar(self.df)
        self.dfl = ttkbootstrap.Listbox(self.df,selectmode=tkinter.SINGLE,exportselection=False,yscrollcommand=self.scrollbar.set)
        self.scrollbar.config(command=self.dfl.yview)
        self.dfl.grid(row=0,column=0,sticky='nsew')


        
        
        self.menu = ttkbootstrap.Menu(self.app)

        self.menu.add_command(label='重加载音乐列表',command=self.flush_music)
        self.menu.add_separator()
        self.app.config(menu=self.menu)

        
        self.menu.add_command(label='mv',command=self.mv_play)
        self.menu.add_command(label='music',command=self.back_music)
        self.screen = ttkbootstrap.Canvas(self.app)

        self.plugin_list = []
        self.run_play_list = []
        self.run_pause_list = []
        self.run_listbox_list = []

        self.scrollbar.grid(row=0,column=1,sticky='n')
        self.dfl.bind('<<ListboxSelect>>', self.change_music)
        if os.path.exists(os.path.join(BASE_DIR,'plugin')) and os.path.isdir(os.path.join(BASE_DIR,'plugin')):
            for asa in os.listdir(os.path.join(BASE_DIR,'plugin')):
                mn = os.path.join(BASE_DIR,'plugin',asa)
                if not os.path.isdir(mn):continue
                if not os.path.isfile(os.path.join(mn,'plugin.json')):continue
                n = plugin(mn)
                if ttkbootstrap.Messagebox.yesno(f'是否加载{n.name}',buttons=['是','否']) == '是':

                    n.init_i(self)
                    self.plugin_list.append(n)

                    if 'play' in n.exec_path:
                        self.run_play_list.append(n.run)
                    if 'pause' in n.exec_path:
                        self.run_pause_list.append(n.run)
                    if 'listbox' in n.exec_path:
                        self.run_listbox_list.append(n.run)




        music_dir = os.path.join(BASE_DIR,'music')
        if not os.path.isdir(music_dir):
            # 原来这里用 os.mkdir('music'),会在当前工作目录下建目录
            os.makedirs(music_dir,exist_ok=True)
        self.flush_music()


    def play(self):
        for xnn in self.run_play_list:xnn(self)
        self.player.play()
    def pause(self):
        for xnn in self.run_pause_list:xnn(self)
        self.player.pause()
    
    def mv_play(self):
        self.dis_f.grid_remove()
        self.player.del_listen(vlc.EventType.MediaPlayerEndReached)
        self.screen.grid(row=0,column=0,sticky='nsew')
        if platform.system() == "Windows":
            self.hwnd = self.screen.winfo_id()
            self.player.music_player.set_hwnd(self.hwnd)
        elif platform.system() == "Linux":
            self.player.music_player.set_xwindow(self.screen.winfo_id())
        elif platform.system() == "Darwin":
            self.player.music_player.set_nsobject(self.screen.winfo_id())
        x = self.player.music_message.get('name','')
        if x and self.music_dict.get(x).get('video') != 'no<>found':
            self.player.set_media_path_mv(x,self.music_dict.get(x,'').get('video'))
        
    def back_music(self):
        self.screen.grid_remove()
        self.dis_f.grid()
    def flush_display(self):
        msg = self.player.music_message
        if self.player.music_photo:
            size = max(self.app.winfo_height() // 4, 32)
            self.lll = ImageTk.PhotoImage(self.player.music_photo.resize((size, size)))
        self.music_image.config(image=self.lll)

        def tag_text(key):
            # mutagen easy=True 取出来是 list,但也可能被写成 str,
            # 直接 join(str) 会得到 "雨,爱" 这种逐字符结果
            v = msg.get(key)
            if not v:v = ['unknow']
            if isinstance(v,str):v = [v]
            return ','.join(str(x) for x in v if x) or 'unknow'

        self.music_name.config(text=f'标题:{tag_text("title")}')
        self.music_maker_name.config(text=f'作者:{tag_text("artist")}')

    def change_mode(self,event):
        a = self.cpls.get()
        match a:
            case '单曲循环':self.player.set_a(0)
            case '顺序播放':self.player.set_a(1)
            case '随机播放':self.player.set_a(2)
            case _:print(f'未知播放模式:{a}')
    def change_music(self,event):
        for xnn in self.run_listbox_list:xnn(self)
        sel = self.dfl.curselection()
        if not sel:return      # 选择被别处抢走(切播放模式/重建列表)时直接忽略
        idx = int(sel[0])
        a = self.dfl.get(idx)
        file = (self.music_dict.get(a) or {}).get("music",'no<>found')

        # 先确认文件有效再停当前音乐,避免"点了是却只弹个警告然后静音"
        if not file or file == 'no<>found':
            ttkbootstrap.Messagebox.show_warning('未指定文件',parent=self.app)
            self._restore_selection()
            return

        aaa = ttkbootstrap.Messagebox.yesno('是否切换音乐','music player',buttons=['是','否'])
        if aaa == '是':
            self.player.stop()
            if self.player.set_media_path(a,file,self.flush_display):
                self.index = idx
                self.player.play()   # 原来漏了这一句:确认切歌后停在静音状态,要再点一次"播放"
                self.flush_display()
            else:
                # set_media_path 内部已经弹过警告了,这里把选择框拨回实际在播的那首
                self._restore_selection()
        else:
            self._restore_selection()

    def _restore_selection(self):
        self.dfl.selection_clear(0, tkinter.END)
        if self.index is not None and 0 <= self.index < self.dfl.size():
            self.dfl.selection_set(self.index)


    def flush_music(self):
        self.music_dict=self.load_music()
        keys = list(self.music_dict.keys())
        self.player.set_d(self.music_dict,self.dfl)
        self.dfl.delete(0,tkinter.END)
        for asa in keys:
            self.dfl.insert(tkinter.END,asa)
        # 列表重建后旧的 index 已经没有意义,必须清掉,
        # 否则 _restore_selection 会把选择恢复到另一首歌上。
        self.index = None
        self.dfl.selection_clear(0,tkinter.END)


    def load_music(self):
        aaa = {}
        music_dir = os.path.join(BASE_DIR,'music')
        for a in os.listdir(music_dir):
            b = os.path.join(music_dir,a)
            if not os.path.isdir(b):
                continue
            info = os.path.join(b,'info.json')
            if not os.path.isfile(info):
                continue
            # json 读取才是会抛异常的步骤,必须包在这里面
            try:
                with open(info,'r',encoding='utf-8') as fp:
                    n:dict = json.load(fp)
            except Exception as e:
                print(f'读取 {info} 失败:{e}')
                continue
            name = n.get('name') or a        # 缺 name 时退回目录名,不让 None 进列表
            # 文件不存在时统一用哨兵,交给 set_media_path 去提示,
            # 不能把 info.json 里的原始文件名当路径用(那是相对路径,会解析到别处)
            fn = n.get('file','')
            mv = n.get('mv','')
            music = os.path.join(b,fn) if fn and os.path.isfile(os.path.join(b,fn)) else 'no<>found'
            video = os.path.join(b,mv) if mv and os.path.isfile(os.path.join(b,mv)) else 'no<>found'
            if music == 'no<>found' and video == 'no<>found':
                print(f'跳过 {a}:没有可播放的文件')
                continue
            # 两个目录用了同一个 name 时原来会互相覆盖,列表里静默少一项。
            # 这里给重名的加后缀,保证每个目录都能出现在列表里。
            key = name
            i = 2
            while key in aaa:
                key = f'{name} ({i})'
                i += 1
            aaa[key] = {'music':music,'video':video}
        return aaa

    def run(self):
        self.app.mainloop()

if __name__ == "__main__":
    try:
        app = tkapp()
        app.run()
    except Exception:
        # 启动/运行期异常:打印完整堆栈并以非零码退出。
        # 原来写成 `except Exception as a:print(a)`,异常对象和上面的 tkapp 实例撞名,
        # 而且只看一行 print 很难定位。
        traceback.print_exc()
        raise SystemExit(1)