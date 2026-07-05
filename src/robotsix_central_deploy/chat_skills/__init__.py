"""Skill documents for external chat-agent components.

Each ``.md`` file in this package describes the API surface of a
non-Docker component that the chat agent can interact with.  The
content is seeded into the component registry at startup and served
directly by ``GET /chat/components`` without an HTTP probe.
"""
