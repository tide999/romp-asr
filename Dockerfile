# ROMP: typed safety policy DSL and trusted tool execution for agents.
#
# Reviewer entry points; none of them require building from source:
#
#   docker run --rm romp-asr                 # usage
#   docker run --rm romp-asr pytest          # the 144 automated checks
#   docker run --rm romp-asr policy-demo     # state-effect admission walkthrough
#   docker run --rm romp-asr agent-demo      # reference Agent, scripted model
#
# For the live model, mount your own host config and pass the key by name:
#
#   docker run --rm -e ROMP_ACCEPTANCE_KEY=... \
#     -v "$PWD/host.json:/work/host.json:ro" \
#     romp-asr agent-demo-live run --config /work/host.json --task "..."
#
# Dependency versions match the environment reported in Section 4.2 of the paper,
# so a reviewer reproduces the reported results without installing anything.

FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="romp-asr" \
      org.opencontainers.image.version="0.3.0" \
      org.opencontainers.image.description="Typed safety policy DSL and trusted tool execution for agents"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# PyYAML is the optional YAML front end; pytest runs the check suite.
RUN pip install --no-cache-dir "pytest==9.1.1" "PyYAML==6.0.3"

WORKDIR /opt/romp

# Install the package before copying sources so this layer caches on source edits.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps -e .

COPY docker-entrypoint.sh /usr/local/bin/romp-entrypoint
RUN chmod +x /usr/local/bin/romp-entrypoint

COPY tests ./tests
COPY examples ./examples
COPY experiments ./experiments

# The file backend needs POSIX dir_fd unlink, which any Linux image provides.
# That is why the deletion half of the demo runs here but not on native Windows.
ENTRYPOINT ["romp-entrypoint"]
CMD ["help"]