#!/usr/bin/env bash
#
# One-shot setup of the TACIT artifact on a fresh FireSim manager instance:
#
#   scripts/tacit-ae/setup-ae.sh
#
# chipyard's toolchain + FireSim + FireMarshal setup, the SPEC submodule, the trace decoder, an
# AWS check, and SPEC CPU2017 installed from the artifact's ISO bucket into ~/spec2017/cpu2017
# (the path the spec_* experiments use; export SPEC_DIR to use another installation instead).
#
# Needs: conda and cargo on the PATH, ~/.aws credentials and ~/firesim.pem (both placed by the
# FireSim tutorial tooling), and read access to the ISO bucket (granted to every reviewer).
# Everything is idempotent: rerunning skips what is already there. About an hour the first
# time, almost all of it chipyard's build-setup. Run it inside tmux or screen.
set -euo pipefail

AE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CY=$(cd "$AE/../.." && pwd)
ISO_URI=s3://tacit-ae-spec2017-696925255345/cpu2017-1.1.9.iso
ISO_MD5=0878c70f8e51b65859276ec314ad9e0f
SPEC_ROOT=$HOME/spec2017
SPEC_DIR=${SPEC_DIR:-$SPEC_ROOT/cpu2017}   # an exported SPEC_DIR (your own installation) is honoured
for a in "$@"; do
  case $a in
    -h|--help) sed -n 2,12p "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $a" >&2; exit 2 ;;
  esac
done
banner() { printf '\n== %s\n' "$*"; }
need()   { command -v "$1" >/dev/null 2>&1 || { echo "$1 is not on the PATH -- $2" >&2; exit 1; }; }

cd "$CY"
# rustup installs cargo under ~/.cargo/bin, which non-interactive shells may not have on the PATH
[ -d "$HOME/.cargo/bin" ] && export PATH="$HOME/.cargo/bin:$PATH"
need conda "install Miniforge/Miniconda first"
need cargo "install rustup (https://rustup.rs), then rerun"

banner "0/5  host packages: tmux (one window per experiment in run.sh all), e2fsprogs (debugfs, to read guest images)"
for pkg in tmux e2fsprogs; do
  bin=$pkg; [ "$pkg" = e2fsprogs ] && bin=debugfs
  if command -v "$bin" >/dev/null 2>&1 || [ -x "/usr/sbin/$bin" ]; then
    echo "   $pkg present"
  elif command -v yum >/dev/null 2>&1; then sudo yum install -y "$pkg" >/dev/null && echo "   $pkg installed (yum)"
  elif command -v apt-get >/dev/null 2>&1; then sudo apt-get install -y "$pkg" >/dev/null && echo "   $pkg installed (apt)"
  else echo "   WARNING: $pkg missing and no known package manager"; fi
done

banner "1/5  chipyard build-setup: conda env, RISC-V toolchain, FireSim, FireMarshal (about an hour the first time)"
if [ -x .conda-env/bin/python3 ] && [ -f sims/firesim/sourceme-manager.sh ] && [ -x software/firemarshal/marshal ]; then
  echo "   already set up (.conda-env, sims/firesim, software/firemarshal present) -- skipping"
else
  ./build-setup.sh riscv-tools
fi

banner "2/5  SPEC submodule (skipped by chipyard's setup; holds the workloads and the SPEC build flow)"
git submodule update --init --recursive software/spec2017
echo "   software/spec2017 at $(git -C software/spec2017 rev-parse --short HEAD)"

banner "3/5  trace decoder"
( cd software/tacit_decoder && cargo build --release 2>&1 | tail -2 )
ls -la software/tacit_decoder/target/release/tacit-decoder | awk '{print "   " $NF, $5, "bytes"}'

banner "4/5  AWS side: identity, key file, the instance's tutorial tag"
set +u; source .conda-env/etc/profile.d/conda.sh; conda activate "$CY/.conda-env" >/dev/null 2>&1 || true; set -u
aws sts get-caller-identity --output text | awk '{print "   account " $1 "   " $2}'
[ -f "$HOME/firesim.pem" ] && echo "   ~/firesim.pem present" || echo "   WARNING: ~/firesim.pem missing -- the manager cannot reach its run farm without it"
TOKEN=$(curl -s -X PUT -m 2 "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 60" || true)
IID=$(curl -s -m 2 -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id || true)
if [ -n "$IID" ]; then
  TAG=$(aws ec2 describe-tags --filters "Name=resource-id,Values=$IID" "Name=key,Values=firesim-tutorial-username" --query 'Tags[0].Value' --output text 2>/dev/null || true)
  if [ -n "$TAG" ] && [ "$TAG" != None ]; then
    echo "   instance $IID tagged firesim-tutorial-username=$TAG: the manager will use key pair, VPC and security groups named after it"
  else
    echo "   instance $IID has no firesim-tutorial-username tag: the manager expects a key pair, VPC and security group named 'firesim'"
  fi
fi

banner "5/5  SPEC CPU2017"
if [ -f "$SPEC_DIR/shrc" ]; then
  echo "   installed at $SPEC_DIR -- skipping"
else
  mkdir -p "$SPEC_ROOT"
  ISO=$SPEC_ROOT/cpu2017-1.1.9.iso
  if [ ! -f "$ISO" ] || [ "$(md5sum "$ISO" | cut -d' ' -f1)" != "$ISO_MD5" ]; then
    echo "   downloading $ISO_URI (3.2 GB)"
    aws s3 cp "$ISO_URI" "$ISO" --no-progress
  fi
  echo "   md5 $(md5sum "$ISO" | cut -d' ' -f1)  (expected $ISO_MD5)"
  [ "$(md5sum "$ISO" | cut -d' ' -f1)" = "$ISO_MD5" ] || { echo "   ISO checksum mismatch" >&2; exit 1; }
  MNT=$(mktemp -d "$SPEC_ROOT/mnt.XXXX")
  sudo mount -o loop,ro "$ISO" "$MNT"
  trap 'sudo umount "$MNT" 2>/dev/null; rmdir "$MNT" 2>/dev/null' EXIT
  ( cd "$MNT" && ./install.sh -f -d "$SPEC_DIR" ) | tail -3
  sudo umount "$MNT"; rmdir "$MNT"; trap - EXIT
  [ -f "$SPEC_DIR/shrc" ] && echo "   installed at $SPEC_DIR" || { echo "   install did not produce $SPEC_DIR/shrc" >&2; exit 1; }
fi

banner "done"
cat <<MSG
   next, from $AE :
     ./run.sh lua_fusion --list     the plan for one experiment, nothing launched
     ./run.sh lua_fusion            one experiment end to end (about 30 min)
     ./run.sh all                   every experiment (SPEC on train, train, ref); attach with: tmux attach -t tacit-ae
MSG
