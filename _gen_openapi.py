"""Generate docs/openapi.json by loading the FastAPI app and dumping its schema."""

import json

from robotsix_central_deploy.lifecycle.server import app

schema = app.openapi()
with open("docs/openapi.json", "w") as f:
    json.dump(schema, f, indent=2)
print("OpenAPI schema written to docs/openapi.json")
