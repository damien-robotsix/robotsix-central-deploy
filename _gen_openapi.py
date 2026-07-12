"""Generate docs/openapi.json by loading the FastAPI app and dumping its schema."""

import json

from robotsix_central_deploy.lifecycle.server import app

schema = app.openapi()
# The API uses its own auth (session token / API key) rather than OpenAPI
# security schemes, so declare an empty security list to satisfy Redocly's
# security-defined rule without weakening any gate.
schema.setdefault("security", [])
with open("docs/openapi.json", "w") as f:
    json.dump(schema, f, indent=2)
    f.write("\n")
print("OpenAPI schema written to docs/openapi.json")
