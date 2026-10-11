# Clean-room tier 2: run the same end-to-end suite on Linux, in a container.
#
# Tier 1 (`run_cleanroom.py` on the host) exercises the Windows paths the
# developer actually uses. This image exercises the *other* half of the path
# logic: mcptoon resolves `$HOME`/`$APPDATA`/XDG differently on Linux, and the
# host stubs it seeds are different files. Same assertions, different platform --
# so a path bug that only shows up off Windows is caught here, and a fix that
# only works on Windows is caught here too.
#
# Build + run:
#     docker build -f scripts/cleanroom/Dockerfile -t mcptoon-cleanroom .
#     docker run --rm mcptoon-cleanroom
#
# The container has no network and no GPU; the run needs neither. It proves the
# Linux path surface, not that a GUI host consumes the config (see README.md).
FROM python:3.12-slim

# git is not required by the product, but `skills doctor` uses it to report
# tracked deletions when a catalog is a git checkout -- include it so those
# code paths are exercised rather than skipped.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

# Zero runtime dependencies by design, so this is a pure editable install.
RUN python -m pip install --no-cache-dir -e . \
 && python -m mcptoon --version

ENV PYTHONUNBUFFERED=1
CMD ["python", "scripts/cleanroom/run_cleanroom.py"]
