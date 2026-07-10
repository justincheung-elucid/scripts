#!/usr/bin/env bash
# Activates the Poetry-managed Python virtualenv used by ~/git/EVServer.
# Must be sourced, not executed: `source activate_evserver_env.sh`

VENV_PATH="$(cd ~/git/EVServer && poetry env info --path 2>/dev/null)"

if [ -z "$VENV_PATH" ]; then
    echo "Could not resolve EVServer's poetry virtualenv. Is poetry installed and configured in ~/git/EVServer?" >&2
    return 1 2>/dev/null || exit 1
fi

source "$VENV_PATH/bin/activate"
