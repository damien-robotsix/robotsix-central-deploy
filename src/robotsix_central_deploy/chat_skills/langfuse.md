# Langfuse — Read-only Trace & Observation API

You have access to the **Langfuse public API** through the central-deploy
proxy, which injects credentials server-side.  **Never** attempt to call
Langfuse directly — always route requests through the proxy so
authentication is handled for you.

Your role is **read-only diagnosis**: query traces, observations, and
sessions to understand runtime behaviour.  You must **never** attempt
write, ingest, or mutation operations.

---

## Making Requests

All Langfuse API calls use this pattern:

```
GET /chat/proxy/langfuse/{path}?project={project}&...other params...
```

- ``{path}`` — the Langfuse API path (see endpoints below).
- ``project`` — which Langfuse project's credentials to use:
  - ``chat`` (default) — the robotsix-chat traces project.
  - ``cognee`` — the cognee traces project.

The proxy adds HTTP Basic Auth using credentials stored in the deploy
EnvStore.  You never see or handle credentials.

### Example: list recent traces for the chat project

```
GET /chat/proxy/langfuse/api/public/traces?project=chat&page=1&limit=10&orderBy=timestamp.desc
```

---

## Endpoints

### GET /api/public/traces

List and search traces.

| Query param     | Type     | Notes                                           |
|-----------------|----------|-------------------------------------------------|
| ``page``        | int      | 1-based (default 1)                            |
| ``limit``       | int      | Page size                                      |
| ``userId``      | string   |                                                 |
| ``name``        | string   | Trace name                                      |
| ``sessionId``   | string   |                                                 |
| ``tags``        | string   | Comma-separated                                 |
| ``version``     | string   |                                                 |
| ``release``     | string   |                                                 |
| ``environment`` | string   |                                                 |
| ``fromTimestamp`` | ISO 8601 | Start of time window                        |
| ``toTimestamp``   | ISO 8601 | End of time window                          |
| ``orderBy``     | string   | ``field.asc`` / ``field.desc`` (fields: ``id``, ``timestamp``, ``name``, ``userId``, ``release``, ``version``, ``public``, ``bookmarked``, ``sessionId``) |
| ``fields``      | string   | ``core``, ``io``, ``scores``, ``observations``, ``metrics`` |
| ``filter``      | JSON     | Advanced filter (takes precedence over simple filters) |

Response: JSON object with ``data`` (list of trace objects) and ``meta``
(pagination info: ``page``, ``limit``, ``totalItems``, ``totalPages``).

---

### GET /api/public/observations (v1, deprecated)

List observations.  Prefer **v2** below for new queries.

| Query param          | Type   | Notes                              |
|----------------------|--------|------------------------------------|
| ``page``             | int    | 1-based                            |
| ``limit``            | int    | Default 1000                       |
| ``userId``           | string |                                    |
| ``name``             | string |                                    |
| ``type``             | string | ``GENERATION`` / ``SPAN`` / ``EVENT`` |
| ``traceId``          | string |                                    |
| ``parentObservationId`` | string |                                |
| ``fromStartTime``    | ISO 8601 |                                 |
| ``toStartTime``      | ISO 8601 |                                 |
| ``version``          | string |                                    |
| ``environment``      | string |                                    |

---

### GET /api/public/v2/observations (recommended)

Cursor-based pagination.

| Query param       | Type     | Notes                                      |
|-------------------|----------|--------------------------------------------|
| ``cursor``        | string   | Opaque cursor from previous response       |
| ``limit``         | int      | Default 50, max 1000                       |
| ``fromStartTime`` | ISO 8601 | **Required** to bound the request          |
| ``toStartTime``   | ISO 8601 | **Required** to bound the request          |
| ``filter``        | JSON     | Advanced filter (same syntax as traces)    |
| ``name``          | string   |                                            |
| ``type``          | string   | ``GENERATION`` / ``SPAN`` / ``EVENT``       |
| ``traceId``       | string   |                                            |
| ``userId``        | string   |                                            |
| ``environment``   | string   |                                            |
| ``version``       | string   |                                            |

Full-text search on ``input``/``output`` columns uses the ``matches``
operator (NOT ``contains``).

---

### GET /api/public/sessions

List sessions.

| Query param       | Type     | Notes                              |
|-------------------|----------|------------------------------------|
| ``page``          | int      | 1-based                            |
| ``limit``         | int      |                                    |
| ``fromTimestamp`` | ISO 8601 |                                    |
| ``toTimestamp``   | ISO 8601 |                                    |
| ``environment``   | string   | Repeatable (``?environment=prod&environment=dev``) |

---

## Safety Rules

1. **Read-only** — Only ``GET`` requests.  Never POST/PUT/PATCH/DELETE.
2. **No PII exfiltration** — Do not echo raw trace/observation data into
   chat unless directly relevant.  Summarise; don't dump.
3. **Time-bound queries** — Always use ``fromTimestamp``/``toTimestamp``
   or ``fromStartTime``/``toStartTime`` to avoid scanning the entire
   history.
4. **Paginate** — Respect ``limit`` and follow ``page`` or ``cursor``.
   Don't request more than 50 items at a time.
5. **Credentials are invisible** — Auth is injected by the proxy.
   Never ask for or display public/secret keys.
6. **Use the proxy** — Always prefix paths with
   ``/chat/proxy/langfuse/``.  Never call Langfuse directly.
