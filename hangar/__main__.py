import uvicorn

from .config import settings


def main() -> None:
    uvicorn.run("hangar.app:app", host=settings.host, port=settings.port, proxy_headers=True,
                forwarded_allow_ips="*", ws_ping_interval=20, log_level="warning")


if __name__ == "__main__":
    main()
