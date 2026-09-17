# app.py — runtime entrypoint
# Keep the existing relay implementation intact in relay_core.py, then load the
# compact /scan route onto the same FastAPI app. This works whether Render starts
# app:app directly or the Procfile entrypoint.

from relay_core import *  # noqa: F401,F403
from relay_core import app, snap, FRESH_CUTOFF_SECS

# Import for route-registration side effect only. scan_endpoint imports the
# app/snap names above and decorates this same FastAPI instance.
import scan_endpoint  # noqa: F401,E402
