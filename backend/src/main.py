"""ASGI 对象与本地可执行入口。"""

import uvicorn

from application import create_app
from settings import app_config

app = create_app()


def main() -> None:
    """启动仅监听回环地址的 API。"""
    uvicorn.run(
        "main:app",
        host=app_config.api.host,
        port=app_config.api.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
