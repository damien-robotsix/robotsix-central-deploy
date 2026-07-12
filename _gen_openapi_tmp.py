"""Generate docs/openapi.json by loading the FastAPI app and dumping its schema."""
import json
import sys
sys.path.insert(0, "src")
from robotsix_central_deploy.lifecycle.server import app

schema = app.openapi()
schema.setdefault("security", [])
schema.setdefault(
    "servers",
    [{"url": "http://localhost:8100", "description": "Local development server"}],
)
schema.setdefault("info", {}).setdefault("license", {"name": "MIT"})
with open("docs/openapi.json", "w") as f:
    json.dump(schema, f, indent=2)
    f.write("\n")
print("OpenAPI schema written to docs/openapi.json")
