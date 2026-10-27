"""平台文件互斥锁；不存在可靠实现时拒绝继续，不退化为无锁写入。"""
import errno
import os
import time


def _windows_lock(stream, blocking):
    import msvcrt
    descriptor = stream.fileno()
    stream.flush()
    if os.fstat(descriptor).st_size == 0:
        os.write(descriptor, b'\0')
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            if not blocking:
                raise BlockingIOError(exc.errno, '文件锁已被其他进程持有') from exc
            time.sleep(.05)


def lock_file(stream, *, blocking=True):
    if os.name == 'nt':
        return _windows_lock(stream, blocking)
    if os.name == 'posix':
        import fcntl
        fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return
    raise OSError('此平台缺少已支持的文件锁；拒绝无锁打开状态或索引')


def unlock_file(stream):
    if os.name == 'nt':
        import msvcrt
        os.lseek(stream.fileno(), 0, os.SEEK_SET)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    elif os.name == 'posix':
        import fcntl
        fcntl.flock(stream, fcntl.LOCK_UN)
    else:
        raise OSError('此平台缺少文件锁解锁实现')
