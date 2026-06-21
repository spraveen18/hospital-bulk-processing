# app/config.py

import os

# How many hospitals to create concurrently
# Higher = faster but risks overwhelming external API
# Lower = safer but slower
# Sweet spot for a free-tier external API: 5
CONCURRENCY_LIMIT: int = int(os.getenv("CONCURRENCY_LIMIT", "5"))

MAX_HOSPITALS: int = int(os.getenv("MAX_HOSPITALS", "20"))