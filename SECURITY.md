# Security Policy

## Supported versions

Only the latest release (`main` branch) receives security fixes.
The deployed server is continuously delivered — there are no
backport or LTS branches.

## Reporting a vulnerability

**Do not open a public issue.** This service handles credentials
(API keys, secrets, Docker socket access), so vulnerabilities
must be disclosed privately.

To report a security issue:

1. Use GitHub's [private vulnerability reporting](https://github.com/damien-robotsix/robotsix-central-deploy/security/advisories/new)
   (preferred).
2. If that is unavailable, email the maintainer directly. Include
   a clear description of the issue, steps to reproduce, and any
   suggested mitigations.

You should receive an acknowledgment within **48 hours**.  The
maintainer will keep you updated on the fix timeline and will
credit you in the release notes (unless you prefer to remain
anonymous).

## Scope

Issues that are **in scope** for a security advisory:

- Authentication / authorization bypasses.
- Secret leakage (API keys, Fernet keys, env secrets exposed via
  logs or API responses).
- Container escape or privilege escalation through the Docker
  socket.
- Code injection via onboarded docker-compose repos or settings
  endpoints.

Issues that are **out of scope** (file a regular issue instead):

- Denial-of-service against a locally deployed instance.
- Theoretical attacks that require physical access to the host.
- Social engineering or phishing.

## Disclosure timeline

1. Reporter submits a vulnerability privately.
2. Maintainer acknowledges within 48 hours.
3. A fix is developed and tested.
4. The fix is deployed to the live server.
5. A public advisory is published (typically within 90 days of the
   initial report, or sooner if the fix ships earlier).
