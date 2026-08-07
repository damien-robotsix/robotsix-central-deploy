Split the monolithic `tests` CI job into three parallel jobs (`lint`, `typecheck`, `test`) so that a failure in one (e.g. ruff) no longer hides results from the others (e.g. mypy, pytest).
