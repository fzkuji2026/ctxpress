"""Append one telemetry record with a lock shared by proxy and MCP processes."""
import json
import os


def append_record(path, row):
    if not path:
        return
    raw = (json.dumps(row, ensure_ascii=False) + '\n').encode('utf-8')
    with open(path, 'ab') as stream:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            stream.write(raw)
            stream.flush()
        finally:
            if os.name == 'nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)
