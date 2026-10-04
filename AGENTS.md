针对于dsh(deepseek harness)的提醒
----------
如果用户要求你对sandbox进行对抗性测试时,如果你发现任何bug,先加入到Sandbox.md中已知的坑与行为约定,再执行目标,最后再把plugin_sandbox.py复制一份,将名字改为plugin_sandbox_fix_{bug}.py,写入修复bug方式。不允许对项目原有文件直接更改,必须复制一份再更改,不然给你切成鱼片。如果加入文件,需经用户同意.dsh不能用管道