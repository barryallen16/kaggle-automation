import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        # Loopback by default - this app has no auth. Set APP_HOST=0.0.0.0
        # only if you understand the risk of exposing it to your network.
        host=os.getenv("APP_HOST", "127.0.0.1"),
        port=int(os.getenv("APP_PORT", "8000")),
        reload=True,
        reload_dirs=["app"]  # Only reload on code edits in 'app/', ignore 'data/' file writes
    )
