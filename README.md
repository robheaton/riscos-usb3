# RISC OS USB3 investigation

Scoping what's actually required to get USB3/SuperSpeed working in RISC OS,
prompted by the long-stalled ROOL "Update and debug USB stack (Step 2)"
bounty. Not pursuing the bounty itself — this is about doing the work.

See [docs/FINDINGS.md](docs/FINDINGS.md) for the technical write-up.

`XHCIDriver/` and `USBDriver/` are unmodified clones of the ROOL sources
(gitignored here — reference material, not this repo's own work):

```bash
git clone https://gitlab.riscosopen.org/RiscOS/Sources/HWSupport/USB/USBDriver.git
git clone https://gitlab.riscosopen.org/RiscOS/Sources/HWSupport/USB/Controllers/XHCIDriver.git
```
