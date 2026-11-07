"""
一键启动脚本：后端 (FastAPI) + 前端 (Vite)。

使用方式：
    conda activate langchain1.2
    python start.py

说明：
- 后端默认端口 8001（避开 8000 可能被占用的情况）
- 前端默认端口 5173，自动代理 /api 到后端
- Milvus 需提前启动（Docker Standalone）
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND_PORT = 8001
FRONTEND_PORT = 5173


def run_backend():
    """启动 FastAPI 后端。"""
    cmd = [
        sys.executable, "-m", "uvicorn",
        "backend.main:app",
        "--host", "0.0.0.0",
        "--port", str(BACKEND_PORT),
    ]
    return subprocess.Popen(cmd, cwd=ROOT)


def run_frontend():
    """启动 Vite 前端开发服务器。"""
    cmd = ["npm.cmd", "run", "dev", "--", "--port", str(FRONTEND_PORT)]
    return subprocess.Popen(cmd, cwd=ROOT / "frontend", shell=True)


def main():
    print("=" * 50)
    print("  星栈数码智能客服 - 启动中...")
    print("=" * 50)

    print(f"[1/2] 启动后端 (端口 {BACKEND_PORT})...")
    backend = run_backend()
    time.sleep(3)

    print(f"[2/2] 启动前端 (端口 {FRONTEND_PORT})...")
    frontend = run_frontend()
    time.sleep(3)

    print(f"\n✅ 服务已启动！")
    print(f"   前端地址: http://localhost:{FRONTEND_PORT}")
    print(f"   后端 API: http://localhost:{BACKEND_PORT}/health")
    print(f"\n按 Ctrl+C 停止所有服务。")

    try:
        frontend.wait()
    except KeyboardInterrupt:
        print("\n正在停止服务...")
        backend.terminate()
        frontend.terminate()
        print("已停止。")


if __name__ == "__main__":
    main()
