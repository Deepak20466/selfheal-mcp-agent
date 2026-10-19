# Independent FastAPI benchmark target

This small repository is an isolated target fixture for self-healing
experiments. It starts with 20 intentional API defects. The benchmark harness
copies it to a temporary directory, initializes a local-only Git repository,
runs all 20 regression tests, applies reference repairs, then verifies the
suite passes. It never contacts GitHub or deploys the app.

From the project root, run:

```powershell
python scripts/benchmark_fastapi.py validate
```

Install this fixture's dependencies separately to run it by hand:

```powershell
python -m pip install -r benchmarks/fastapi_failure_repo/requirements.txt
python -m pytest benchmarks/fastapi_failure_repo
```

The reference repairs validate the scenario fixtures only. They are not AI
repair results; AI diagnosis and repair metrics remain unmeasured until real
attempt records are supplied to `python scripts/benchmark_fastapi.py evaluate`.
