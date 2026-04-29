"""Jinja2 templates singleton with registered filters.

Import `templates` from here in any module that needs to render HTML.
Filters are registered once at import time.
"""

import os

from fastapi.templating import Jinja2Templates

from dashboard.ui_helpers import fmt_dt, dt_lag, rel_time

templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(__file__), "templates")
)
templates.env.filters["fmt_dt"] = fmt_dt
templates.env.filters["dt_lag"] = dt_lag
templates.env.filters["rel_time"] = rel_time
