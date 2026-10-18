#!/bin/sh
# Reviewer-facing entrypoint. The image is useless without a way in, so the
# documented commands are first-class rather than paths a reviewer must guess.
set -e
cd /opt/romp

case "${1:-help}" in
  help)
    cat <<'USAGE'
ROMP reference image

  pytest                      run the automated checks (144 expected)
  policy-demo [args]          state-effect admission walkthrough
                              --pace N  seconds per line
  agent-demo [args]           reference Agent with a scripted model transport
                              offline; --pace N  seconds per line
  agent-demo-live [args]      reference Agent against a real hosted model
                              (needs the key named by key_env in your config)
  acceptance                  real-model acceptance harness (Section 4.3).
                              Mount a writable dir holding your .env:
                                mkdir -p out && cp .env out/
                                docker run --rm -v "$PWD/out:/work" romp-asr acceptance
                              Costs hosted-model tokens; results land in out/.
  examples/... <args>         run a file under examples/ directly
  <anything else>             executed verbatim

The local payment ledger and the temporary-file backends are the real ones. No
network call is made except in agent-demo-live and acceptance.
USAGE
    ;;
  pytest)
    shift
    exec python3 -m pytest "$@"
    ;;
  policy-demo)
    shift
    exec python3 examples/policy_v03_walkthrough.py "$@"
    ;;
  agent-demo)
    shift
    exec python3 examples/romp_agent_offline_demo.py "$@"
    ;;
  agent-demo-live)
    shift
    exec romp-agent "$@"
    ;;
  acceptance)
    # The real-model acceptance harness. It reads the key from <root>/.env and
    # resolves that root from its own location, so it is copied into a writable
    # tree whose parent holds the .env the reviewer mounts.
    shift
    work="${ROMP_WORKDIR:-/work}"
    mkdir -p "$work/experiments"
    rm -rf "$work/experiments/reference-agent"
    cp -R /opt/romp/experiments/reference-agent "$work/experiments/reference-agent"
    # One-shot by design: never overwrite preserved evidence or a consumed scope.
    rm -f "$work/experiments/reference-agent"/results.json \
          "$work/experiments/reference-agent"/audit.json \
          "$work/experiments/reference-agent"/manifest.json
    cd "$work/experiments/reference-agent"
    exec python3 accept.py "$@"
    ;;
  *)
    exec "$@"
    ;;
esac