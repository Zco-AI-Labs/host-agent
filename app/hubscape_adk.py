import sys
from app.core import hubscape_adk

# Alias module so import hubscape_adk directly references app.core.hubscape_adk
sys.modules['hubscape_adk'] = hubscape_adk
