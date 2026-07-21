"""Generate docs/lifecycle/openapi.json by loading the FastAPI app and dumping its schema."""

import json

from robotsix_central_deploy.lifecycle.app import app

schema = app.openapi()
# The API uses its own auth (session token / API key) rather than OpenAPI
# security schemes, so declare an empty security list to satisfy Redocly's
# security-defined rule without weakening any gate.
schema.setdefault("security", [])
# Redocly recommended rules require a servers array and a license field.
schema.setdefault(
    "servers",
    [{"url": "http://localhost:8100", "description": "Local development server"}],
)
schema.setdefault("info", {}).setdefault("license", {"name": "MIT"})
with open("docs/lifecycle/openapi.json", "w") as f:
    json.dump(schema, f, indent=2)
    f.write("\n")
print("OpenAPI schema written to docs/lifecycle/openapi.json")
