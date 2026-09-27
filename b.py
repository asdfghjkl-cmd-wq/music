import glob
import os
import random
import traceback

import mutagen.flac
import mutagen.id3
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.environ['PYTHON_VLC_MODULE_PATH'] = f"{BASE_DIR}/pvlc"
from PIL import ImageTk,Image
import ttkbootstrap,json,tkinter,vlc,mutagen,io

print(BASE_DIR)

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
    def set_a(self,aint):
        # play_tt 只有 0/1/2 三种有实现,允许 3 会变成"切了模式但行为不变"
        if 0<=aint<=2:self.play_tt = aint
    def play_backbround_listen(self,event,path,end_action):
        """
        MediaPlayerEndReached 的回调,运行在 libvlc 的事件线程里。
        libvlc 不可重入,所以这里绝不能调用 set_mrl/play/event_detach,
        只把真正的切歌动作排队到 Tk 主线程再执行。
        """
        print(event)
        lb = self.listb
        if lb is None:
            print('listb 未初始化,忽略本次切歌')
            return
        lb.after(0,lambda:self._advance(path,end_action))

    def _select_index(self,index):
        lb = self.listb
        if lb is None:
            return
        lb.selection_clear(0,tkinter.END)
        lb.selection_set(index)
        lb.see(index)

    def _advance(self,path,end_action):
        """真正切歌,运行在 Tk 主线程里。"""
        # 同一事件类型只需要一个回调:先摘掉旧的,再由 set_media_path 按新参数重新注册。
        # 少了这一步,EndReached 监听就是一次性的,切完这一首就再也不会自动切歌。
        self.del_listen(vlc.EventType.MediaPlayerEndReached)



        match self.play_tt:
            case 0:       
                name = self.music_message.get('name','')
                ok = self.set_media_path(name,path,end_action)
                self._after_switch(ok,path,end_action)
                
            case 1:
                a = list(self.music_dict.keys())
                if not a:
                    print('曲库为空,停止切歌')
                    return
                n = self.music_message.get('name','')
                an = a.index(n)+1 if n in a else 0
                if an == len(a):an = 0
                print(a[an])
                ok = self.set_media_path(a[an],(self.music_dict.get(a[an]) or {}).get("music",""),end_action)
                if ok:self._select_index(an)
                self._after_switch(ok,path,end_action)
                
            case 2:
                a = list(self.music_dict.keys())
                if not a:
                    print('曲库为空,停止切歌')
                    return
                an = random.choice(a)
                ok = self.set_media_path(an,(self.music_dict.get(an) or {}).get("music",""),end_action)
                if ok:self._select_index(a.index(an))
                self._after_switch(ok,path,end_action)
            case _:
                print(f'未知播放模式:{self.play_tt}')

            
                        


    def _after_switch(self,ok,path,end_action):
        if not ok:
            # 目标文件无效:把原来的监听重新挂回去,别把整条播放链弄断
            self.add_listen(vlc.EventType.MediaPlayerEndReached,self.play_backbround_listen,path,end_action)
            return
        self.play()
        end_action()

    def set_media_path_mv(self,name,path):
        # 目前没有调用点(MV 模式还没接入),保留给后面用
        if not path or path == 'no<>found':
            ttkbootstrap.Messagebox.show_warning('未指定文件')
            return False
        self.music_player.set_mrl(path)
        mini = (self.music_dict.get(name) or {}).get('music',None)
        if mini:
            self.load_message(name,mini)
        else:self.music_photo = Image.open(os.path.join(BASE_DIR,'a.png'))
        return True


    def set_media_path(self,name,path,end_action,b=True):
        """设置要播放的文件。返回 False 表示没有可播放的文件。"""
        if not path or path == 'no<>found':
            ttkbootstrap.Messagebox.show_warning('未指定文件')
            return False
            
        self.music_player.set_mrl(path)
        
        if b:
            self.add_listen(vlc.EventType.MediaPlayerEndReached,self.play_backbround_listen,path,end_action)
        self.load_message(name,path)
        return True

    def load_message(self,name,path):
        try:
            a = mutagen.File(path,easy=True)
            bd = {}
            for aa,b in a.items():
                bd[aa]=b
            bd['name'] = name
            self.music_message = bd
            self.music_photo = Image.open(os.path.join(BASE_DIR,'a.png'))
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
            else :self.music_photo = Image.open(os.path.join(BASE_DIR,'a.png'))
        except Exception as w:print(w) 
    def play(self) -> int:return self.music_player.play()
    def pause(self) -> None:self.music_player.pause()
    def stop(self) -> None:
        self.music_player.stop()
        self.del_listen(vlc.EventType.MediaPlayerEndReached)

    def set_time(self,time):"""time's Unit is seconds""";self.music_player.set_time(time*1000)
    def time_add_ten(self) -> None:
        if self.music_player.get_state() in (vlc.State.Playing,vlc.State.Paused):self.music_player.set_time(self.music_player.get_time()+10000)
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
    def __init__(self,file):
        with open(file,'r',encoding='utf-8') as fp:
            n = json.load(fp)
        self.com = compile(n.get('command',''),"<string>","exec")
        self.init_command = compile(n.get('init',''),"<string>","exec")
        self.name = n.get('name','')
        self.can_exec = n.get('CanExec',False)
        self.exec_path = n.get('ExecPath',[])
        # 插件用独立的命名空间:既能拿到 b.py 里的名字,又不会把插件写的东西
        # 塞进 b.py 的全局命名空间;init 和 run 共用同一个 dict,
        # 插件在 init 里定义的东西在 run 时还在。
        self.kk = dict(globals())
        
    def init(self,tkaapp=None):
        self.kk['tkapp'] = tkaapp
        if self.can_exec:exec(self.init_command,self.kk)
    def run(self,tkaapp=None):
        self.kk['tkapp'] = tkaapp
        if self.can_exec:exec(self.com,self.kk)
            

class tkapp:
    def __init__(self):
        self.music_dict = {}
        self.player = player(self.music_dict)
        self.index: "int | None" = None     # 当前选中项(整数索引,不是 curselection 返回的元组)
        self.app= ttkbootstrap.Tk('music','64')   # 第二个参数是主题名,'64' 不是合法主题(会被忽略)
        
        self.app.title('music')
        self.app.geometry("800x600+20+200")
        self.app.rowconfigure(0, weight=10)
        self.app.rowconfigure(1, weight=1)
        self.app.rowconfigure(2, weight=10)
        self.app.columnconfigure(0, weight=3)
        self.app.columnconfigure(1, weight=1)
        self.a = ttkbootstrap.Style()
        print(self.a.theme_names())
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

        


        self.plugin_list = []
        self.run_play_list = []
        self.run_pause_list = []
        self.run_listbox_list = []

        self.scrollbar.grid(row=0,column=1,sticky='n')
        self.dfl.bind('<<ListboxSelect>>', self.change_music)
        if os.path.exists(os.path.join(BASE_DIR,'plugin')) and os.path.isdir(os.path.join(BASE_DIR,'plugin')):
            for a in glob.iglob('*.json',root_dir=os.path.join(BASE_DIR,'plugin')):
                n = plugin(os.path.join(BASE_DIR,'plugin',a))
                if ttkbootstrap.Messagebox.yesno(f'是否加载{n.name}',buttons=['是','否']) == '是':
                    n.init(self)
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
        print(a)
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
            self.player.set_media_path(a,file,self.flush_display)
            self.flush_display()
            self.index = idx
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
            file = n.get('file','no<>found')
            video = n.get('mv','no<>found')
            # 文件不存在时统一用哨兵,交给 set_media_path 去提示,
            # 不能把 info.json 里的原始文件名当路径用(那是相对路径,会解析到别处)
            music = os.path.join(b,file) if os.path.isfile(os.path.join(b,file)) else 'no<>found'
            video = os.path.join(b,video) if os.path.isfile(os.path.join(b,video)) else 'no<>found'
            if music == 'no<>found' and video == 'no<>found':
                print(f'跳过 {a}:没有可播放的文件')
                continue
            aaa[name] = {'music':music,'video':video}
        print(aaa)
        return aaa

    def run(self):
        self.app.mainloop()

try:
    if __name__ == "__main__":
        a = tkapp()
        a.run()
except Exception as a:print(a);traceback.print_exc()