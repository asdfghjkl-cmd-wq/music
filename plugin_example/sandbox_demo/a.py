# 沙盒示例插件
#
# 它**不会**被自动加载:播放器只扫 plugin/,这里放的是参考实现。
# 想看效果就把整个 sandbox_demo 目录拷进 plugin/,再启动播放器。
#
# 演示四件事:
#   1. 读自己的目录(默认策略就允许);
#   2. 自查权限(__sandbox__.can / describe);
#   3. 被拒时精确捕获 SandboxDenied,而不是把异常吞掉;
#   4. 只通过 pro 门面加菜单项(拿到的是白名单门面,不是整个播放器对象)。

import json
import os


def describe_me():
    print(f'[sandbox_demo] env_id={__env_id__} 权限:{__sandbox__.describe()}')


def read_own_manifest():
    # 读插件自己的目录:fs_read 默认就是 ["."],这个 plugin.json 又额外要了 "../.."
    with open(os.path.join(plugin_dir, 'plugin.json'), encoding='utf-8') as fp:
        return json.load(fp)


def write_demo_file():
    target = os.path.join(plugin_dir, 'demo_out.txt')
    if not __sandbox__.can('fs:write', target):
        print('[sandbox_demo] 现在没有写权限,试着写一下:会弹窗问用户')
    try:
        with open(target, 'w', encoding='utf-8') as fp:
            fp.write('hello from sandbox_demo\n')
    except SandboxDenied as e:
        print(f'[sandbox_demo] 被沙盒拒绝,操作没有发生:{e}')
        return False
    print(f'[sandbox_demo] 写成功:{target}')
    return True


def try_outside():
    # 越界读:没授权就是一个异常,读不到任何东西
    outside = os.path.join(plugin_dir, '..', '..', 'b.py')
    if not __sandbox__.can('fs:read', outside):
        print('[sandbox_demo] 提前自查就知道这个路径没权限,不去碰它')
    try:
        with open(outside, encoding='utf-8') as fp:
            return fp.read(20)
    except SandboxDenied as e:
        print(f'[sandbox_demo] 越界读取被拒(预期行为):{e}')
        return None


print(f'[sandbox_demo] 装载,manifest name = {read_own_manifest()["name"]}')
describe_me()
pro.menu.add_command(label='sandbox_demo:说明', command=describe_me)
pro.menu.add_command(label='sandbox_demo:写个文件', command=write_demo_file)
pro.menu.add_command(label='sandbox_demo:越界试试', command=try_outside)
