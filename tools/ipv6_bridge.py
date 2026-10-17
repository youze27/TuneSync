"""IPv6 → IPv4 转发桥。

解决什么问题：Docker Desktop 给容器的是纯 IPv4 NAT，容器里 `getaddrinfo` 能解析
IPv6 目标、却永远连不上；而宿主机自己有 IPv6 出口。于是让宿主机在 IPv4 上听一个端口，
把流量原样转给 IPv6 目标，容器再连 `host.docker.internal:<监听端口>` 即可。

    python tools\ipv6_bridge.py --target 2001:db8::1 --target-port 4533 --listen-port 14533

（`2001:db8::/32` 是 RFC 3849 文档专用前缀，实际使用时换成你自己的 IPv6 地址。）

之后 TuneSync 的 Navidrome 服务地址填 http://127.0.0.1:14533 就行
（容器会自动改写成 host.docker.internal:14533）。

仅监听回环之外的可控端口，且只做字节转发、不解析内容——凭据仍是端到端 TLS
（本项目到 Navidrome 走的是 Subsonic 口令 + token，不是明文口令）。
"""
import argparse
import socket
import sys
import threading


def pump(src, dst):
    """单向转发。任一端关闭或出错即返回，由另一条方向自然收尾。"""
    try:
        while True:
            buf = src.recv(65536)
            if not buf:
                break
            dst.sendall(buf)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def handle(client, t_host, t_port, fam):
    try:
        upstream = socket.socket(fam, socket.SOCK_STREAM)
        upstream.settimeout(15)
        upstream.connect((t_host, t_port))
    except OSError as e:
        print(f"  ! 连不上目标 {t_host}:{t_port} -> {e}", flush=True)
        client.close()
        return
    t = threading.Thread(target=pump, args=(client, upstream), daemon=True)
    t.start()
    pump(upstream, client)
    t.join(timeout=5)
    client.close()
    upstream.close()


def main():
    ap = argparse.ArgumentParser(description="把宿主机 IPv4 端口转发到 IPv6 目标")
    ap.add_argument("--target", required=True, help="目标的 IPv6 地址（不带方括号）")
    ap.add_argument("--target-port", type=int, default=4533)
    ap.add_argument("--listen-port", type=int, default=14533)
    ap.add_argument("--listen", default="0.0.0.0", help="默认听所有 IPv4 网卡")
    args = ap.parse_args()

    # 目标带冒号就是 IPv6；否则当 IPv4 用
    fam = socket.AF_INET6 if ":" in args.target else socket.AF_INET

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.listen, args.listen_port))
    srv.listen(64)
    print(f"桥已启动: {args.listen}:{args.listen_port} -> "
          f"[{args.target}]:{args.target_port}", flush=True)
    print(f"容器里填: http://host.docker.internal:{args.listen_port}", flush=True)

    try:
        while True:
            c, addr = srv.accept()
            threading.Thread(target=handle, args=(c, args.target, args.target_port, fam),
                             daemon=True).start()
    except KeyboardInterrupt:
        print("\n桥已停止")


if __name__ == "__main__":
    sys.exit(main())
