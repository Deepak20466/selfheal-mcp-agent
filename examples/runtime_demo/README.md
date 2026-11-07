# Runtime error demo

This FastAPI app contains three deliberate defects in `main.py`: a nullable
profile field is dereferenced, an out-of-range product page indexes an empty
page, and an invalid order quantity raises `ValueError`. Sentinel middleware
captures exceptions with the runtime demo file and line number.

## Run the native processes

From the repository root, configure `.env` with a disposable PostgreSQL
database, an already-supported free AI provider, and GitHub write access to
the configured repository. Then run `scripts/run_runtime_demo.sh` or
`scripts/run_runtime_demo.ps1`. The script starts Selfheal's native service
processes through `selfheal up`, registers the demo app, starts this app on
port 8110, triggers selected defects, and waits for each healer job result.
The default trigger is the nullable profile route; pass `pagination` or
`quantity` to select another case, or `all` to run all three sequentially.

The worker opens a review PR only when provider inference, isolated tests, and
GitHub access all succeed. A recorded failure is shown as a failure; the demo
never claims a PR was opened when it was not. Stop Selfheal's service
processes with `selfheal down` after reviewing the job and PR.
Each runner generates a fresh Sentinel ingest token, synchronizes it to the
database, and passes it only to the local demo process.

The seeded regression expectations are in `examples/runtime_demo/demo_tests.py` and
currently fail until the corresponding defect is repaired. They are kept out
of default test discovery and are used explicitly by the app's isolated
pre-PR test command.
