import argparse
import asyncio
import sys


def main():
    parser = argparse.ArgumentParser(description="Web Test Trans — 本地自动化录制台")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", default="data")
    args = parser.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    import uvicorn
    from .app import create_app
    uvicorn.run(create_app(args.data_dir), host="127.0.0.1", port=args.port, loop="asyncio")


if __name__ == "__main__":
    main()
