Hardened log-injection, open-redirect, and path-injection CodeQL alerts:

- Standardised all ``logger.<level>(...)`` calls in ``chat.py`` and ``services.py`` on the canonical ``_sanitize_log`` helper (which strips both ``\n`` and ``\r``), replacing incomplete inline ``.replace("\n", "\\n")`` patterns that lacked ``\r`` stripping.
- Added same-line ``# codeql[py/url-redirection]`` suppressions on every user-derived ``RedirectResponse`` sink in ``ui/router.py`` that is already guarded by ``_safe_next`` (which rejects off-site URLs).
- Added a same-line ``# codeql[py/path-injection]`` suppression on the ``shutil.disk_usage(config.disk_path)`` call in ``health.py`` — ``disk_path`` is operator-configured via ``LifecycleConfig``, never request-derived.
- Added a same-line ``# codeql[py/path-injection]`` suppression on the ``os.path.realpath`` call in ``ui_static`` — the canonical ``realpath`` + ``startswith`` guard already prevents traversal.
