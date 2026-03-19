from .file import UFile
# import time
import sys

def is_iterable(obj):
    try:
        iter(obj)
        return True
    except TypeError:
        return False

def _get_target_timestamp(args:list): # get min
    target_early = sys.maxsize
    for arg in args:
        if is_iterable(arg) and len(arg) > 0 and isinstance(arg[0], UFile):
            target_early = min(target_early, _get_target_timestamp(arg))
        elif isinstance(arg, UFile):
            if arg.exist:
                target_early = min(target_early, arg.get_timestamp())
            else:
                target_early = 0
                break

    return target_early

def _get_src_timestamp(args:list): # get min
    src_last = 0
    for arg in args:
        if is_iterable(arg) and len(arg) > 0 and isinstance(arg[0], UFile):
            src_last = max(src_last, _get_src_timestamp(arg))
        elif isinstance(arg, UFile):
            if arg.exist:
                src_last = max(src_last, arg.get_timestamp())
            else:
                src_last = 0
                raise FileNotFoundError(f"{arg} {arg.exist} not found.")

    return src_last


def make_style_decorate(func):
    '''
        @ brief
        @ param: 左边目标, 左边 src
    '''
    def decorated_func(*args:list, target_count=1):
        '''
            @param target_count: 目标数量, 如果target_count=-1(TODO: 无奈之举, 目前无法通过cmake 去获取依赖文件), 会被无脑执行.
        '''
        if target_count > 0:
            target_early = _get_target_timestamp(args[:target_count])
            src_older = _get_src_timestamp(args[target_count:])
            if src_older <= target_early:
                return
        return func(*args)
    return decorated_func
