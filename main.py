"""DiscoBot - Remote-controlled audio player."""

import os

import uvicorn

if __name__ == "__main__":
    debug = os.environ.get("DISCOBOT_DEBUG", "").lower() in ("1", "true", "yes")
    host = os.environ.get("DISCOBOT_HOST", "0.0.0.0")
    port = int(os.environ.get("DISCOBOT_PORT", "8000"))
    uvicorn.run("app.api:app", host=host, port=port, reload=debug)
