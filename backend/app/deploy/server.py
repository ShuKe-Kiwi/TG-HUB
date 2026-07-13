"""Run Uvicorn without replacing the application's logging contract."""

import uvicorn

from app.config import load_settings


def main() -> None:
    settings = load_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.ADMIN_BIND_HOST,
        port=settings.ADMIN_PORT,
        workers=1,
        access_log=False,
        log_config=None,
    )


if __name__ == "__main__":
    main()
