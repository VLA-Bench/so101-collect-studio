import os
import socket

import uvicorn

from .server import app

PORT = 8600


def _lan_ip() -> str | None:
    """取本机局域网 IP（向公网地址建 UDP 连接探测出口网卡，不真正发包）。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return None


if __name__ == "__main__":
    # 默认仅本机;SO101_HOST=0.0.0.0 可局域网访问(无认证,仅在可信网络开启)
    host = os.environ.get("SO101_HOST", "127.0.0.1")
    print(f"SO101 Collect Studio 已启动:")
    print(f"  采集台:   http://127.0.0.1:{PORT}/")
    print(f"  场景展示: http://127.0.0.1:{PORT}/scene")
    print(f"  井字棋: http://127.0.0.1:{PORT}/tic-tac-toe")
    if host == "0.0.0.0":
        ip = _lan_ip()
        if ip:
            print(f"  局域网访问(另一台电脑打开):")
            print(f"    采集台:   http://{ip}:{PORT}/")
            print(f"    场景展示: http://{ip}:{PORT}/scene")
            print(f"    井字棋: http://{ip}:{PORT}/tic-tac-toe")
        print("  ⚠ 当前无认证,局域网内任何人都能控制机械臂/相机,请确认网络可信。")
    uvicorn.run(app, host=host, port=PORT, log_level="warning")
