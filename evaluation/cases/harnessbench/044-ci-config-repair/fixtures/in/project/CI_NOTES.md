# CI Notes

The project is intentionally small, but the workflow should be safe for normal pull requests and local test execution.

Requirements:
- Use `push`, `pull_request`, and `workflow_dispatch`.
- Do not use `pull_request_target`; this project accepts forked pull requests.
- Use `actions/checkout@v4` and `actions/setup-python@v5`.
- Use Python 3.10 and 3.11 in a matrix.
- Cache pip dependencies.
- Install from `requirements.txt` with `pip install -r requirements.txt`.
- Run `python -m pytest`.
- Run a lightweight import smoke command with `python -c`.
- Path filters must include app code, tests, requirements, workflow files, and this CI notes file.
- Use least-privilege permissions: repository contents should be read-only.
- Add a concurrency group so repeated pushes to the same branch cancel older CI runs.
- Set a job timeout so a hung test run does not consume minutes indefinitely.
- Keep the matrix explicit and do not let one Python version silently mask the other; `fail-fast: false` is preferred.
