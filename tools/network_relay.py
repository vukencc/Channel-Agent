"""仅在私有网络命名空间运行；将本地 HTTP 代理流量送到受限 Unix 网关。"""
import select
import socket
import subprocess
import sys
import threading


def relay(client):
    with client, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as upstream:
        try:
            upstream.connect('/run/agent-network/proxy.sock')
            while True:
                readable, _, _ = select.select([client, upstream], [], [], 5)
                if not readable:
                    return
                for source in readable:
                    data = source.recv(16384)
                    if not data:
                        return
                    (upstream if source is client else client).sendall(data)
        except OSError:
            return


def serve(listener):
    while True:
        client, _ = listener.accept()
        # 命令资源配额同时约束中继线程；远端网关另有限制。
        threading.Thread(target=relay, args=(client,), daemon=True).start()


if __name__ == '__main__':
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 8877))
        listener.listen(4)
        threading.Thread(target=serve, args=(listener,), daemon=True).start()
        raise SystemExit(subprocess.call(sys.argv[1:], stdin=subprocess.DEVNULL))
