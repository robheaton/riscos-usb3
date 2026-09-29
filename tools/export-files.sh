#!/bin/sh
# Write every file changed on branch usb3 in the two reference clones into
# patched-files/, keeping the source-tree layout, ready to copy to RISC OS
# and drop over the matching files in the USBDriver / XHCIDriver checkouts.
#
#   tools/export-files.sh          the branch tip as it is (currently the safe
#                                  configuration: 4 of the 5 root ports exposed)
#   tools/export-files.sh 5port    same, but with all four SuperSpeed root ports
#                                  exposed -- the configuration that crashes
#                                  USBDriver at boot, for crash capture
set -e
cd "$(dirname "$0")/.."
rm -rf patched-files
for spec in USBDriver:df856fc XHCIDriver:74a4f4d; do
    d=${spec%%:*}
    base=${spec##*:}
    for f in $(git -C "$d" diff --name-only "$base" usb3); do
        mkdir -p "patched-files/$d/$(dirname "$f")"
        git -C "$d" show "usb3:$f" > "patched-files/$d/$f"
    done
done
find patched-files -type f | sort

if [ "$1" = "5port" ]; then
    f=patched-files/XHCIDriver/c/xhci
    sed -i -e 's/^#define XHCIDRIVER_RH_SS_SKIP .*/#define XHCIDRIVER_RH_SS_SKIP 0/' \
           -e 's/^#define XHCIDRIVER_RH_EXTRA_SS .*/#define XHCIDRIVER_RH_EXTRA_SS 4/' "$f"
    echo "5port: $(grep -c '^#define XHCIDRIVER_RH_SS_SKIP 0$' "$f") SKIP=0, $(grep -c '^#define XHCIDRIVER_RH_EXTRA_SS 4$' "$f") EXTRA_SS=4 (both must be 1)"
fi
