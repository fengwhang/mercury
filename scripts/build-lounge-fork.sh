#!/usr/bin/env bash
# Compatibility wrapper for existing release-host automation.
exec bash "$(dirname "${BASH_SOURCE[0]}")/build-mlounge-fork.sh" "$@"
