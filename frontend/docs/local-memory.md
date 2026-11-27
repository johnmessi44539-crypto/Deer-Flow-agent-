# Local compilation memory

The Next.js configuration enables `webpackMemoryOptimizations` and disables
entry preloading to reduce compilation and startup memory peaks. These settings
are supported by the documentation shipped with this checkout's Next.js package.
Compilation may take longer on the first request.

The local evaluation Compose overlay limits the frontend container to 3 GiB,
allows 1 GiB of swap, and limits each Node.js old-generation heap to 2048 MiB.
The remaining space is needed by native allocations and launcher processes.
It appends frontend logs and records the launcher exit code so a restart does
not erase the previous failure. Docker memory and Windows commit capacity must
still have sufficient headroom; a successful startup message alone does not
verify that a workspace route can compile.

Next.js 16.4.5 restarts its development worker after a request when used V8 heap
exceeds 80 percent of the heap limit. The earlier 1536 MiB setting caused
restarts immediately after workspace compilation on this machine. The 2048 MiB
setting leaves room under the container limit for native allocations while
retaining Next.js's restart protection.

The evaluation overlay starts the Gateway without automatic reload and reuses
its installed dependency environment with `uv run --no-sync`. Restart the
Gateway after installing or upgrading an extension. This removes the filesystem
watcher from long evaluation runs; use the normal development entrypoint when
you need reload or a locked dependency sync.

For repeated evaluation sessions, the local `build-production.ps1` helper builds
the existing installed frontend offline in a separate container. Its configuration
uses one build worker, Webpack's build worker and memory optimizations. The
container is limited to 4 GiB plus 2 GiB of swap, with a 3072 MiB old-generation
heap. It copies explicit source directories and never copies dotenv files.
The helper retains the build log and container on failure and does not retry.

A successful build becomes the local `deerflow-eval-frontend:local-v0.2` image.
The `compose.production.yaml` overlay removes development mounts and runs
`pnpm start`, with a 1536 MiB container limit and a 1024 MiB heap. It retains
Gateway authentication and the nginx entry point on port 2026. Frontend logs
for this mode are available through `docker logs deer-flow-frontend`.

The local `start.cmd` prefers this image when it exists. Pass `dev` to select
the development frontend explicitly. Changes to core frontend source require
a new production build; installed extension browser assets still load from the
Gateway after extension upgrade and Gateway restart. The first-build helper
refuses to overwrite an existing image or build container, so inspect and retain
the earlier result before preparing another build.

On 2026-11-27, this local production frontend completed the real evaluation
workflow in the in-app browser: three built-in Agent cases and one imported
case passed, alongside the offline controls, comparison and JSON downloads.
After those requests, the frontend used about 126 MiB according to Docker
stats, compared with about 2.7 GiB in development mode. The initial login HTML
request took about 1.2 seconds and the workspace about 0.55 seconds; a warm
workspace request took about 0.04 seconds. These are observations on this
machine, not a concurrency benchmark.

The startup helper uses PowerShell `Start-Sleep` while waiting for Docker.
Windows `timeout` rejects redirected stdin, which previously produced errors
when the helper was launched by an automated terminal.
