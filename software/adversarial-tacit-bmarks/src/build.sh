#!/bin/bash
# FireMarshal host-init: build every benchmark family ELF into build/.
set -e
cd "$(dirname "$0")"
make all
