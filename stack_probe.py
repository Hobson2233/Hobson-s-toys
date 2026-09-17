# -*- coding: utf-8 -*-
"""在 8 秒后把**所有线程**的堆栈打到 stderr，用来看 --guitest 到底卡在哪。

用法: python stack_probe.py <要跑的脚本> [参数...]
"""
import faulthandler
import runpy
import sys

target = sys.argv[1]
rest = sys.argv[2:]

faulthandler.dump_traceback_later(8, exit=False)
sys.argv = [target] + rest
runpy.run_path(target, run_name="__main__")
