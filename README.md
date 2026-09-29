# RISC OS USB3 investigation

Working out what it takes to get USB3/SuperSpeed running in RISC OS, and
trying it on real hardware. Not pursuing the ROOL bounty as such, just doing
the work.

**Status:** groundwork is in `USBDriver` and `XHCIDriver`, but no device has
yet been seen running at SuperSpeed and there is an open boot-time crash.
[docs/FINDINGS.md](docs/FINDINGS.md) starts with the current state (what is
established, what has been retracted, what is open); the rest of it is the
chronological log.

## Layout

| Path | What |
|---|---|
| `docs/FINDINGS.md` | Current state, then the full log |
| `patches/USBDriver/`, `patches/XHCIDriver/` | `git format-patch` series against ROOL's sources |
| `tools/export-files.sh` | Writes the changed files (whole files, source-tree layout) to `patched-files/` for copying to a RISC OS machine |
| `tools/armdis.py` | Disassembles a built module around a `*Where` offset (needs `pip install capstone`) |

`USBDriver/`, `XHCIDriver/` and `patched-files/` are local working copies and
are gitignored.

## Reproducing the source tree

The series applies on ROOL's unmodified sources at these commits:

| Component | Base commit |
|---|---|
| USBDriver | `df856fc` (2024-06-09) |
| XHCIDriver | `74a4f4d` (2024-10-29) |

Run from the root of this repo:

```bash
git clone https://gitlab.riscosopen.org/RiscOS/Sources/HWSupport/USB/USBDriver.git
git -C USBDriver checkout -b usb3 df856fc
git -C USBDriver am "$PWD"/patches/USBDriver/*.patch

git clone https://gitlab.riscosopen.org/RiscOS/Sources/HWSupport/USB/Controllers/XHCIDriver.git
git -C XHCIDriver checkout -b usb3 74a4f4d
git -C XHCIDriver am "$PWD"/patches/XHCIDriver/*.patch
```

## What is permanent and what is scaffolding

Commit subjects say which:

- `USB3: ...` is groundwork that is inert until something reports a
  SuperSpeed device, and has been built and run on hardware without
  regressions on USB2 devices.
- `EXPERIMENT: ...` and `DIAG (temporary): ...` are bisection and
  diagnostic scaffolding. They change hardware behaviour or add debug
  output, and must be dropped before anything is proposed upstream.

Nothing here has been compiled outside the RISC OS DDE; every change was
built and tried by hand on a CM4, a Pi 400 and a Pi 4. Get whole files onto
the RISC OS machine with `tools/export-files.sh`.
