"""把 tunesync-deploy.tar.gz 推到飞牛 NAS 并重建容器。

飞牛 NAS 的 SSH 只接受密码认证，而这里的终端是非交互的，ssh/scp 无法喂密码。
改用 paramiko（同样走密码认证，但可以在脚本里非交互地传）。

用法：
    python tools\\deploy_nas.py <密码>
"""
import posixpath
import sys

import paramiko

HOST = "2409:8a6c:2d2e:ed30:d1ef:c227:a457:858c"
USER = "youze27"
REMOTE_DIR = "/vol1/1000/tunesync-deploy"
LOCAL_TARBALL = "tunesync-deploy.tar.gz"


def run(client, cmd, timeout=1800):
    """跑一条远端命令，把 stdout/stderr 实时吐出来，失败抛异常。"""
    print(f"\n$ {cmd}", flush=True)
    _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    for line in iter(stdout.readline, ""):
        print(line, end="", flush=True)
    err = stderr.read().decode("utf-8", "replace").strip()
    code = stdout.channel.recv_exit_status()
    if err:
        print(f"[stderr] {err}", flush=True)
    if code != 0:
        raise SystemExit(f"远端命令失败（exit {code}）：{cmd}")
    return stdout


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    password = sys.argv[1]

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        HOST,
        username=USER,
        password=password,
        timeout=30,
        auth_timeout=30,
        banner_timeout=30,
    )
    print(f"已连上 {HOST}", flush=True)

    sftp = client.open_sftp()
    remote_tar = posixpath.join(REMOTE_DIR, LOCAL_TARBALL)
    print(f"上传 {LOCAL_TARBALL} -> {remote_tar}", flush=True)
    sftp.put(LOCAL_TARBALL, remote_tar)
    sftp.close()
    print("上传完成", flush=True)

    run(client, f"cd {REMOTE_DIR} && tar xzf {LOCAL_TARBALL}")
    run(client, f"cd {REMOTE_DIR} && docker compose build", timeout=3600)
    run(client, f"cd {REMOTE_DIR} && docker compose up -d --force-recreate", timeout=1800)
    run(client, "docker ps --filter name=tunesync --format '{{.Names}}\t{{.Status}}'", timeout=60)

    client.close()
    print("\n部署完成", flush=True)


if __name__ == "__main__":
    main()
