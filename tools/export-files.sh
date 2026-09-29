#!/bin/sh
# Write every file changed on branch usb3 in the two reference clones into
# patched-files/, keeping the source-tree layout, ready to copy to RISC OS
# and drop over the matching files in the USBDriver / XHCIDriver checkouts.
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
