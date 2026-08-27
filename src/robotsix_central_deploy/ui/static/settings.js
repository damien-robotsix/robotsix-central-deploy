/* Mounts the fleet's shared config panel over central-deploy's own settings.
 *
 * The panel, the schema-driven inputs, the masked secrets, the changed-keys-
 * only save and the version history all come from robotsix-ui. Nothing in
 * this file re-implements any of it — that is the point. A component that
 * hand-rolls its settings form eventually reintroduces the bug where a masked
 * secret is posted back and overwrites the real credential.
 */
import { mountConfigPanel } from "/ui/static/robotsix-ui-vanilla.js";

const container = document.getElementById("settings-panel");
const notice = document.getElementById("restart-notice");

/* The CSRF cookie is httponly, so the token is handed to the page in a data
 * attribute instead. /config is deliberately not in the middleware's exempt
 * list: unlike the JSON API it is reached from the browser with only the SSO
 * cookie, which would otherwise ride along on a cross-site request. */
const csrfToken = container.dataset.csrfToken || "";

/* ---------------------------------------------------------------------------
 * Dict-as-text workaround — robotsix-ui renders any schema node of the form
 * {"type":"object"} WITHOUT "properties" (i.e. pydantic's dict[str, Any] with
 * additionalProperties: true) as a plain text input via buildTextRow, which
 * does String(value), turning {"level1":…} into "[object Object]" — a value
 * that fails the backend Pydantic validation on the next save.
 *
 * We wrap fetchImpl so that:
 *   • every config-fetch response (GET, PUT-result, rollback-result) has its
 *     open-ended dict values replaced with their JSON string representation
 *     before the panel renders them — the text input then holds valid JSON;
 *   • every PUT /config request has those JSON strings parsed back into
 *     objects before the server sees them.
 *
 * `scalarDicts` is populated lazily from the response schema — the first time
 * a config response arrives, any property that is {type:"object"} without
 * concrete properties and with a boolean additionalProperties is flagged.
 * ------------------------------------------------------------------------ */
const _scalarDicts = new Set();

/**
 * Return true when *propSchema* is a JSON-Schema "anything goes" dict —
 * i.e. the pydantic bound dict[str, Any] that the renderer cannot display
 * as a structured section (no properties) or keyed map (additionalProperties
 * must be a schema object, not a boolean).
 */
function _isOpenDictSchema(propSchema) {
  return (
    propSchema &&
    propSchema.type === "object" &&
    !propSchema.properties &&
    typeof propSchema.additionalProperties === "boolean"
  );
}

/** Populate `_scalarDicts` from the schema returned with a config payload. */
function _learnScalarDicts(schema) {
  if (!schema || !schema.properties) return;
  for (const [key, node] of Object.entries(schema.properties)) {
    if (_isOpenDictSchema(node)) _scalarDicts.add(key);
  }
}

/** Stringify every flagged dict value inside *config*. Returns a new object. */
function _stringifyDictValues(config) {
  let changed = false;
  const out = { ...config };
  for (const key of _scalarDicts) {
    const val = out[key];
    if (val !== null && typeof val === "object" && !Array.isArray(val)) {
      out[key] = JSON.stringify(val, null, 2);
      changed = true;
    }
  }
  return changed ? out : config;
}

/** Parse every flagged string-field back to an object inside *body*. */
function _parseDictValues(body) {
  let changed = false;
  const out = { ...body };
  for (const key of _scalarDicts) {
    if (typeof out[key] === "string") {
      try {
        const parsed = JSON.parse(out[key]);
        if (typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)) {
          out[key] = parsed;
          changed = true;
        }
      } catch { /* not valid JSON — leave as-is, validation will catch it */ }
    }
  }
  return changed ? out : body;
}

/**
 * Wrap a base fetch so that config responses and requests handle
 * open-ended dict fields correctly.
 */
function _wrapConfigFetch(baseFetch) {
  return async function (url, init) {
    /* ---- outbound: PUT /config request body ---- */
    let wrappedInit = init;
    if (init && init.method === "PUT" && typeof url === "string" && url.includes("/config")) {
      if (init.body && typeof init.body === "string") {
        try {
          const body = JSON.parse(init.body);
          const fixed = _parseDictValues(body);
          if (fixed !== body) {
            wrappedInit = { ...init, body: JSON.stringify(fixed) };
          }
        } catch { /* not JSON — pass through */ }
      }
    }

    const response = await baseFetch(url, wrappedInit);

    /* ---- inbound: config payloads from GET /config, PUT result, rollback ---- */
    const method = (init && init.method) || "GET";
    const isConfigResponse =
      (method === "GET" || method === "PUT" || method === "POST") &&
      typeof url === "string" &&
      url.includes("/config") &&
      response.ok;

    if (isConfigResponse) {
      try {
        const cloned = response.clone();
        const payload = await cloned.json();
        if (payload && payload.config && typeof payload.config === "object") {
          /* First response: learn which fields are open-ended dicts. */
          if (payload.schema && payload.schema.properties) {
            _learnScalarDicts(payload.schema);
          }
          const fixed = _stringifyDictValues(payload.config);
          if (fixed !== payload.config) {
            return new Response(JSON.stringify({ ...payload, config: fixed }), {
              status: response.status,
              statusText: response.statusText,
              headers: response.headers,
            });
          }
        }
      } catch { /* not JSON or unexpected shape — pass through */ }
    }

    return response;
  };
}

mountConfigPanel(container, {
  title: "Central Deploy Settings",
  fetchImpl: _wrapConfigFetch(fetch.bind(window)),
  headers: csrfToken ? { "x-csrftoken": csrfToken } : {},
  onSaved: () => {
    notice.hidden = false;
  },
});
