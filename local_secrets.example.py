# -*- coding: utf-8 -*-
"""本地敏感词模板 —— **复制成 local_secrets.py 再填**。

    cp local_secrets.example.py local_secrets.py

`local_secrets.py` 已经在 `.gitignore` 里，**永远不会被提交**。
这个 `example` 文件是给你看格式用的，里面必须是假数据。

为什么要有这东西：
    打包门槛 `leak_check.py` 要拿「已知的真实账号密码」当搜索词，去 exe 里搜，
    证明它们**没被打进去**。搜索词本身不能写死在源码里 —— 这个仓库是公开的。
    `switch_test.py`（真实切换实测）同理。

用法二选一：
    A. 复制成 local_secrets.py，把下面两行改成真值
    B. 不改文件，用环境变量：set CAMPUS_SENSITIVE=学号1,密码1,学号2,密码2
"""

# leak_check 的兜底搜索词。真值填这里，格式就是一堆字符串。
NEEDLES = [
    "DEMO_STUDENT_ID_1",      # 换成你的学号
    "DEMO_PASSWORD_1",        # 换成你的密码
]

# switch_test 用的两个账号：(学号, 密码, 备注)
# 备注只影响打印出来的标签，方便你看是哪一轮。
ACCOUNTS = [
    ("DEMO_STUDENT_ID_1", "DEMO_PASSWORD_1", "主账号"),
    ("DEMO_STUDENT_ID_2", "DEMO_PASSWORD_2", "备用账号"),
]
