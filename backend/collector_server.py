"""Private browser owner and collection scheduler; started once per machine."""

import os

os.environ["PDD_SERVICE_ROLE"] = "collector"
from .main import app
