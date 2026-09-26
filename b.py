import glob
import os
import random
import threading
from time import sleep
import traceback
from types import NoneType
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
        self.listen_thread = None
        self.play_tt = 0
        self.stop_flag = threading.Event()
        self.stop_lock = threading.Lock()
        self.music_player = vlc.MediaPlayer(self.player)
        self.music_dict = d
        self.listb:ttkbootstrap.Listbox;NoneType = None
    def set_d(self,d,l):self.music_dict=d;self.listb = l
    def set_a(self,aint):
        if 0<=aint<=3:self.play_tt = aint
    def play_backbround_listen(self,path,end_action,event:threading.Event):
        event.clear()
        while True:
            sleep(1)
            with self.stop_lock:
                if event.is_set():break
                if self.music_player.get_state() == vlc.State.Ended:
                    match self.play_tt:
                        case 0:
                            
                            self.set_media_path(self.music_message.get('name',''),path,end_action,False)
                            self.play()
                            end_action()
                        case 1:
                            a = list(self.music_dict.keys())
                            try:n = self.music_message.get('name','')
                            except Exception:traceback.print_exc()
                            an = 0
                            if n in a:
                                an = a.index(n)+1
                                if an == len(a):an = 0
                                print(a[an])
                            self.set_media_path(a[an],self.music_dict.get(a[an]),end_action,False)
                            self.play()
                            end_action()
                            self.listb.selection_clear(0,tkinter.END)
                            self.listb.selection_set(an)
                            break
                        case 2:
                            a = list(self.music_dict.keys())
                            an= random.choice(a)
                            self.set_media_path(an,self.music_dict.get(an),end_action,False)
                            self.play()
                            nd =a.index(an)
                            self.listb.selection_clear(0,tkinter.END)
                            self.listb.selection_set(nd)
                            end_action()
                            break
            
                        



    def set_media_path(self,name,path,end_action,b=True):
        self.music_player.set_mrl(path)
        
        if b:
            self.stop_flag = threading.Event()
            self.listen_thread = threading.Thread(target=self.play_backbround_listen,args=(path,end_action,self.stop_flag),daemon=True,name='play_backround')
            self.listen_thread.start()
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
    def play(self):return self.music_player.play()
    def pause(self):self.music_player.pause()
    def stop(self):
        self.music_player.stop()
        with self.stop_lock:
            self.stop_flag.set()
    def set_time(self,time):"""time's Unit is seconds""";self.music_player.set_time(time*1000)
    def time_add_ten(self):
        if self.music_player.get_state() in (vlc.State.Playing,vlc.State.Paused):self.music_player.set_time(self.music_player.get_time()+10000)
    def time_minus_ten(self):
        if self.music_player.get_state() in (vlc.State.Playing,vlc.State.Paused):self.music_player.set_time(self.music_player.get_time()-10000)

            
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

        self._start_poll()

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
        n = json.load(open(file,'r',encoding='utf-8'))
        self.com = compile(n.get('command',''),"<string>","exec")
        self.init_command = compile(n.get('init',''),"<string>","exec")
        self.name = n.get('name','')
        self.can_exec = n.get('CanExec',False)
        self.exec_path = n.get('ExecPath',[])
        
    def init(self,tkaapp=None):
        self.kk = globals()
        self.kk['tkapp'] = tkaapp
        if self.can_exec:exec(self.init_command,self.kk)
    def run(self,tkaapp=None):
        self.kk = globals()
        self.kk['tkapp'] = tkaapp
        if self.can_exec:exec(self.com,self.kk)
            

class tkapp:
    def __init__(self):
        self.music_dict = {}
        self.player = player(self.music_dict)
        self.index = 0
        self.app= ttkbootstrap.Tk('music','64')
        
        self.app.title('music')
        self.app.geometry("800x600+20+200")
        self.app.rowconfigure(0, weight=10)
        self.app.rowconfigure(1, weight=1)
        self.app.rowconfigure(2, weight=10)
        self.app.columnconfigure((0), weight=3)
        self.app.columnconfigure((1), weight=1)
        self.a = ttkbootstrap.Style()
        print(self.a.theme_names())
        if 'solarized-light' in self.a.theme_names():
            self.a.theme_use('solarized-light')

        self.app.iconphoto(True,ImageTk.PhotoImage(Image.open('a.png').resize((16,16))))

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

        ph = ImageTk.PhotoImage(Image.open('a.png').resize((int(600/4),int(600/4))))
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




        if os.path.isdir(os.path.join(BASE_DIR,'music')):
            self.flush_music()
        else: os.mkdir('music');self.flush_music()


    def play(self):
        for xnn in self.run_play_list:xnn(self)
        self.player.play()
    def pause(self):
        for xnn in self.run_pause_list:xnn(self)
        self.player.play()
    
    
    def flush_display(self):
        if self.player.music_photo:
                size = max(self.app.winfo_height() // 4, 32)
                self.lll = ImageTk.PhotoImage(self.player.music_photo.resize((size, size)))
                self.music_image.config(image=self.lll)
        b = ','.join(self.player.music_message.get('title',['unknow']))
        c = ','.join(self.player.music_message.get('artist',['unknow']))
        if not b:b = 'unknow'
        if not c:c = 'unknow'
        self.music_image.config(image=self.lll)
        self.music_name.config(text=f'标题:{b}')
        self.music_maker_name.config(text=f'作者:{c}')

    def change_mode(self,event):
        a = self.cpls.get()
        print(a)
        match a:
            case '单曲循环':self.player.set_a(0)
            case '顺序播放':self.player.set_a(1)
            case '随机播放':self.player.set_a(2)
    def change_music(self,event):
        for xnn in self.run_listbox_list:xnn(self)
        sel = self.dfl.curselection()
        if not sel:return      # 选择被别处抢走(切播放模式/重建列表)时直接忽略
        a = self.dfl.get(sel)
        file = self.music_dict.get(a)
        
        aaa = ttkbootstrap.Messagebox.yesno('是否切换音乐','music player',buttons=['是','否'])
        if aaa == '是':
            self.player.stop()
            self.player.set_media_path(a,file,self.flush_display)
            self.flush_display()
        else:
            self.dfl.selection_clear(0, tkinter.END)
            self.dfl.select_set(self.index)
        self.index = self.dfl.curselection()


    def flush_music(self):
        self.music_dict=self.load_music()
        a = self.music_dict.keys()
        self.player.set_d(self.music_dict,self.dfl)
        self.dfl.delete(0,tkinter.END)
        for asa in a:
            self.dfl.insert(tkinter.END,asa)


    def load_music(self):
        aaa = {}
        for a in  os.listdir(os.path.join('.','music')):
            b = os.path.join('.','music',a)
            if os.path.isdir(b):
                if os.path.isfile(os.path.join(b,'info.json')):
                    n:dict = json.load(open(os.path.join(b,'info.json'),'r',encoding='utf-8'))
                    try:
                        name = n.get('name',None)
                        file = n.get('file',None)
                    except Exception as e:
                        print(e)
                        continue
                    if os.path.isfile(os.path.join(b,file)):
                        aaa[name]= os.path.join(b,file)
        print(aaa)
        return aaa

    def run(self):
        self.app.mainloop()

try:
    if __name__ == "__main__":
        a = tkapp()
        a.run()
except Exception as a:print(a)