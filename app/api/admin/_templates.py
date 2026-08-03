import pathlib
from fastapi.templating import Jinja2Templates
from app.config import VERSION
from app.api.admin.session import get_session
from app.core.nav import NAV_GROUPS, ICONS as NAV_ICONS

_templates_dir = pathlib.Path(__file__).parent.parent.parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))
templates.env.globals["version"] = VERSION
templates.env.globals["get_session"] = get_session
templates.env.globals["nav_groups"] = NAV_GROUPS
templates.env.globals["nav_icons"] = NAV_ICONS
