#!/usr/bin/env bash
#
# Entry point for the TACIT artifact-evaluation experiments.
#
#   ./run.sh all [--force]               every experiment: shared preparation, then all in parallel
#   ./run.sh <experiment> [flags]        one experiment, all its steps, resuming past done ones
#   ./run.sh lua_fusion --list           show the plan and exit
#   ./run.sh lua_fusion --force          redo every step
#   ./run.sh lua_fusion --to driver      host-only preparation; --from fpga for the rest
#
# Experiments live in experiments/<name>/<name>.py, each a self-contained linear
# script over the common layer (paths, shell, uartlog, firesim, steps). They share
# the step contract in steps.py, which is what lets all.py drive them uniformly.
# No argument means lua_fusion.
#
# This script exists only to set up the environment, which is the one thing that
# cannot be done from Python: the four scripts below mutate the shell (PATH,
# RISCV, conda, Xilinx), and Python cannot source a shell script. Everything after
# the handoff -- steps, resume, logging, reporting -- lives in the experiment's script.
set -uo pipefail

AE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CY=$(cd "$AE/../.." && pwd)
[ -f "$CY/env.sh" ] || { echo "not a chipyard tree: $CY (no env.sh)" >&2; exit 1; }

# Three traps live in the next six lines, all of them silent when violated:
#   - conda's riscv-tools hook and sourceme-manager reference unset variables, so
#     -u has to stand down while they run;
#   - sourceme-manager.sh does a relative ./env.sh, so it MUST be sourced with
#     sims/firesim as the working directory, not deploy/;
#   - none of this may be sourced through a pipe -- the subshell would take the
#     environment with it and leave the caller with nothing.
set +u
# shellcheck disable=SC1090,SC1091
source "$CY/.conda-env/etc/profile.d/conda.sh"
source "$CY/env.sh"
cd "$CY/sims/firesim" && source ./sourceme-manager.sh --skip-ssh-setup >/dev/null 2>&1
[ -f /ecad/tools/xilinx/Vitis/2021.1/settings64.sh ] && \
  source /ecad/tools/xilinx/Vitis/2021.1/settings64.sh >/dev/null 2>&1
set -u

# first argument names the experiment when it is not a flag
EXP=lua_fusion
if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then EXP=$1; shift; fi
if [ "$EXP" = all ]; then
  cd "$AE"
  exec "$CY/.conda-env/bin/python3" "$AE/all.py" "$@"
fi
SCRIPT="$AE/experiments/$EXP/$EXP.py"
[ -f "$SCRIPT" ] || { echo "no experiment '$EXP'. Available: $(ls "$AE/experiments")" >&2; exit 1; }
cd "$AE"
exec "$CY/.conda-env/bin/python3" "$SCRIPT" "$@"
