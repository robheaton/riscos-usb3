# What's actually blocking USB3 in RISC OS

Source: live clones of the ROOL gitlab repos (see README for clone commands),
inspected 2026-09-23. Commit hashes below are what was current at that time.

## TL;DR

An xHCI host-controller driver **already exists and works** (`XHCIDriver`,
v0.32, last touched Oct 2024, ported from NetBSD `xhci.c` rev 1.29 / 2015).
It runs real Pi4 and Titanium hardware today. But it **deliberately forces
every SuperSpeed port back down to USB2/1 speed at init time**, because the
core `USBDriver` module it plugs into is still running NetBSD's **2004/2005**
`usbdi`/`usbdivar` code — a full decade before USB3, xHCI, or multi-tier hub
topology existed as concepts. The gap isn't "write an xHCI driver," it's
"the thing the xHCI driver reports its devices to doesn't have a data model
for USB3 devices."

## The smoking gun

`XHCIDriver/c/xhci`, function `xhci_init()`:

```c
/* XXX Low/Full/High speeds for now */
sc->sc_bus.usbrev = USBREV_2_0;
...
#ifdef RISCOS
    /* Disable any SuperSpeed ports since no USB3 yet */
    for (i = 0; i < sc->sc_ss_port_count; i++) {
        int port = XHCI_PORTSC(sc->sc_ss_port_start + i);
        /* Disable port then force RxDetect state to redo link as UBS2 or USB1 */
        xhci_op_write_4(sc, port, XHCI_PS_PLS_SET(5) | XHCI_PS_PED);
    }
#endif
```

The driver already parses the xHCI extended capabilities to find which
physical ports are paired SuperSpeed/USB2 ports (`sc_ss_port_start`,
`sc_ss_port_count` — this is real, correct xHCI protocol-capability parsing),
then immediately disables the SS side and forces those ports to renegotiate
as USB2. This is a deliberate, marked `#ifdef RISCOS` local patch on top of
the imported NetBSD code — i.e. someone at ROOL already did the analysis and
inserted a guard rail rather than ship a half-working SS path.

## Version skew between the two halves of the stack

| Component | NetBSD source vintage | Evidence |
|---|---|---|
| `XHCIDriver` (`c/xhci`) | 2015 (`xhci.c,v 1.29`) | `$NetBSD` ident tag |
| `USBDriver` core (`usb_subr.c`) | 2005 (`v 1.122`) | `$NetBSD` ident tag |
| `USBDriver` core (`uhub.c`) | 2005 (`v 1.74`) | `$NetBSD` ident tag |
| `USBDriver` core (`usbdivar.h`) | 2005 (`v 1.73`) | `$NetBSD` ident tag |

NetBSD's own xHCI/USB3 support didn't land until years after 2005, so the
core has never seen a USB3-aware `usbdi`. Concretely, in `usbdivar.h`:

```c
#define USBREV_UNKNOWN	0
#define USBREV_PRE_1_0	1
#define USBREV_1_0	2
#define USBREV_1_1	3
#define USBREV_2_0	4
#define USBREV_STR { "unknown", "pre 1.0", "1.0", "1.1", "2.0" }
```

There is no `USBREV_3_0`. This is why `xhci_init()` hardcodes
`usbrev = USBREV_2_0` — there's nothing else it could set it to.

Interestingly, `usb_subr.c:1232` already has one lone SuperSpeed-aware
branch:

```c
/* 4.8.2.1 */
if (speed == USB_SPEED_SUPER)
    USETW(dev->def_ep_desc.wMaxPacketSize, (1 << dd->bMaxPacketSize));
else
    USETW(dev->def_ep_desc.wMaxPacketSize, dd->bMaxPacketSize);
```

(USB3 encodes the control endpoint's max packet size as `2^n` in
`bMaxPacketSize0`, rather than the literal byte count USB1/2 use — this is
handling that.) `USB_SPEED_SUPER` (`= 4`) is defined in `usb.h`. So this one
spot was hand-patched in at some point, presumably in anticipation of SS
support, but it's an island — nothing upstream of it can ever produce
`speed == USB_SPEED_SUPER` today because `XHCIDriver` never reports it.

## What's missing from the core (`USBDriver`), concretely

Grepped for and found **no trace of**, anywhere in `USBDriver`:

- `USBREV_3_0` — no USB3 revision value to assign
- BOS descriptor (`UDESC_BOS`, class code `0x0F`) — needed for `wSpeedsSupported`,
  U1/U2 exit latency, and other SuperSpeed device capability data
- SuperSpeed hub descriptor type (`UDESC_SS_HUB = 0x2A`) — `uhub.c` only ever
  requests `UDESC_HUB` (`0x29`, the USB1/2 format). USB3 hubs use a
  differently-shaped descriptor (fixed 2-byte DeviceRemovable field instead
  of the USB2 variable-length bitmap, different hub characteristics bits,
  mandatory single-TT-equivalent behaviour since SS hubs don't have TTs)
- SuperSpeed Endpoint Companion Descriptor (`UDESC_ENDPOINT_SS_COMP = 0x30`)
  — carries `bMaxBurst` and stream count; without parsing it, per-endpoint
  burst/stream configuration can't be passed down to `XHCIDriver`'s
  `xhci_configure_endpoint` path
- Any bulk stream support (`xhci.c` has no `streams` handling either — grepped,
  zero hits)

## What is *not* missing / lower risk than expected

- The xHCI hardware itself does the hard part of USB3 topology (route
  strings, slot/device contexts, per-tier addressing) — that's
  `XHCIDriver`'s job and it's already a reasonably current, working port.
  Removing the port-downgrade hack does not require reimplementing xHCI.
- Extended-capability parsing to find the SS/HS port pairing
  (`sc_ss_port_start`/`sc_ss_port_count`) is already correct in `XHCIDriver`.
- USB3 PCIe xHCI controllers are explicitly supported already
  (`8d9ee35 Add support for XHCI that exists on a PCI bus`), so hardware
  reach (Titanium, PCIe cards) isn't blocked.

## Rough shape of the work, in dependency order

1. **Bring `usbdivar.h`'s revision enum and `USBREV_STR` up to date** —
   trivial on its own, but is the flag day marker for "the core knows USB3
   exists."
2. **BOS descriptor fetch + parse** during `usbd_new_device()` for
   SuperSpeed devices (gate on `speed == USB_SPEED_SUPER`, which `usb_subr.c`
   already tests for in one place).
3. **SuperSpeed Endpoint Companion Descriptor parsing** in the config
   descriptor walk, alongside the existing endpoint descriptor parsing —
   needed before any SS bulk/isoc endpoint can be configured with correct
   burst size.
4. **`uhub.c`: branch to `UDESC_SS_HUB` when the hub device's speed is
   SuperSpeed**, and adapt the descriptor struct handling (it currently
   assumes the USB2 `usb_hub_descriptor_t` shape unconditionally).
5. **Remove the `#ifdef RISCOS` port-downgrade block in `xhci_init()`**,
   set `sc_bus.usbrev` conditionally per port pairing instead of hardcoding
   `USBREV_2_0`, and let SS ports link at SS speed.
6. **Stream support** (bulk endpoints only) — can genuinely be deferred;
   most USB3 mass storage / UAS devices will enumerate and do basic bulk I/O
   without it, just without the multi-outstanding-command benefit streams
   give UAS.
7. Testing matrix: Pi4 (VL805, PCIe rev of xHCI) and Titanium (whichever
   xHCI silicon that is) both already boot `XHCIDriver`, so hardware-in-loop
   testing is available without new procurement.

Steps 1–4 are what the bounty's "integration of the latest revision of the
NetBSD sources" line is really asking for — not a wholesale resync of the
*entire* USB stack to modern NetBSD (which would be a much bigger, riskier
undertaking and would also drag in unrelated 20 years of NetBSD churn), but
specifically backfilling the USB3-era `usbdi` concepts that are missing.
A targeted backport of just the relevant NetBSD commits (BOS/SS-companion-
descriptor parsing, SS hub descriptor handling, `USBREV_3_0`) is very
plausibly a smaller job than "resync everything," and doesn't require
touching `OHCIDriver`/`EHCIDriver`/`MUSBDriver`/`DWCDriver` at all since
they can't produce `USB_SPEED_SUPER` devices in the first place.

## Phase 0 findings (2026-09-24): the change is additive, not a restructure

Read `usbd_fill_iface_data()` / `usbd_find_edesc()` in `usb_subr.c` in full.
The endpoint-descriptor walk is a generic "step by `bLength`, stop on
`UDESC_INTERFACE` or zero length" loop — it already tolerates unknown
descriptor types sitting between endpoint descriptors (it just steps over
them). Adding a check, right after an endpoint descriptor is found, for a
following `UDESC_ENDPOINT_SS_COMP (0x30)` descriptor is a local addition to
that one loop, not a restructure.

The only structural change needed is a new field on `struct usbd_endpoint`
(`usbdivar.h`) to hold a pointer to the parsed companion descriptor —
currently just `{ edesc, refcnt, datatoggle }`, nothing SS-related.

And there's a second matching stub on the consumer side, in `XHCIDriver`'s
`xhci_configure_endpoint()`:

```c
XHCI_EPCTX_1_MAXB_SET(0)   /* hardcoded, every endpoint type, every call site */
```

Max burst is unconditionally zero. This is exactly the value the SS
companion descriptor's `bMaxBurst` field should feed — another sign this
was left as a deliberate stub waiting for the core to supply the data,
not an oversight.

**DeviceFS boundary — already fine.** `build/c/usbmodule` (the RISC OS
glue/frontend) formats the displayed device speed via a message-file lookup:
`sprintf(speed, "Spd%d", udev->speed)` → looked up in
`build/Resources/UK/Messages`, which already has:

```
Spd1:Low
Spd2:Full
Spd3:High
Spd4:Super
Spd?:Unknown
```

`Spd4:Super` already exists. The glue layer just echoes `dev->speed`
through with no speed-specific logic — nothing to change here.

## Correction: more USB3 constant plumbing already exists than first found

A closer read of `dev/usb/h/usb` turned up several more USB3-related
`#define`s already present — `UDESC_SSHUB` (0x2a, correct name — not
`UDESC_SS_HUB` as first guessed), `UDPROTO_SSHUB` (0x03), `UPS_SUPER_SPEED`
(0x0600, the SuperSpeed port-status link-speed value), and
`USB_3_MAX_CTRL_PACKET` (512). None of these predate 2005 in upstream
NetBSD, so like `USB_SPEED_SUPER`, they're RISC OS-local additions layered
onto the 2005 base — **but grepping the `.c` files, none of the four are
referenced anywhere.** Defined, never consumed. Same pattern as the one
`USB_SPEED_SUPER` check in `usb_subr.c`: someone has been dropping in
constants in anticipation of USB3 work, without wiring up the logic that
would use them.

Net effect on scope: slightly less new code needed than first estimated —
the hub-descriptor-type and port-status constants for SS are already
correctly defined and just need consuming code. Still confirmed absent
(no definition anywhere): the SS Endpoint Companion Descriptor struct and
the BOS descriptor struct — those need adding from scratch.

## Patch 2: SuperSpeed hub descriptor + SET_HUB_DEPTH

Cross-checked against real upstream NetBSD `uhub.c` (fetched from
`NetBSD/src` trunk) before writing this, rather than guessing the shape.
Two things it does, both required for any non-root SS hub to work at all:

1. **`usbd_get_hub_desc` equivalent, inlined into `uhub_attach`**: for a
   non-root hub (`dev->depth != 0`) at `USB_SPEED_SUPER`, request
   `UDESC_SSHUB` instead of `UDESC_HUB` — issuing the USB2 hub descriptor
   request to a real SS hub is invalid per spec and can stall its control
   pipe. The new `usb_hub_ss_descriptor_t` (fixed 12 bytes, capped at 15
   ports, matches NetBSD's struct exactly) is fetched then normalised
   field-by-field into the existing `usb_hub_descriptor_t hubdesc` local,
   so every line downstream of the fetch (port counting, `UHD_NOT_REMOV`,
   `hub->hubdesc = hubdesc`, power-up delay calc) needs no changes at all.
2. **`UR_SET_HUB_DEPTH`**: a USB3-only class request with no 2.0 equivalent.
   A non-root SS hub has to be told its own tier so it can correctly
   decrement route strings for whatever's plugged into it. Sent once, right
   after the hub struct is allocated, gated on the same
   `speed == USB_SPEED_SUPER && depth != 0` condition.

Root hubs are excluded from both (`dev->depth != 0` guard) — matches
upstream, and sidesteps the question of what `XHCIDriver`'s root hub
emulation currently expects, which hasn't been checked yet.

Verified by reconstruction: extracted both patches, applied them in order
from a clean checkout of the four touched files, and byte-diffed the result
against the actual edited working tree — identical in all four files.

## Remaining open question

- What NetBSD source tree/tag is the best "donor" for the BOS/SS-hub/
  SS-companion-descriptor parsing code — pulling from a NetBSD version
  close to 2015 (matching `XHCIDriver`'s vintage) to minimize unrelated
  diff noise, rather than the latest NetBSD, is probably the pragmatic
  choice. Not yet picked.

## Build verification (2026-09-24)

Patches 1 and 2 build clean under the native DDE on the CM4 (RISC OS 5.31).
No compiler complaints about the `memset`/`memcpy` usage added to `uhub.c`
(the one thing I couldn't verify without a compiler) or anything else.

## Hardware verification (2026-09-24): no regression

Rebuilt ROM booted fine on the CM4, USB (keyboard/mouse/storage) works
normally. Confirms patches 1+2 are a genuine no-op for non-SS devices, not
just "compiles clean" — the enumeration path they touch (endpoint-descriptor
walk, hub descriptor fetch) is exercised by every USB device, and nothing
regressed. Cleared to move on to the actual speed-unlock patch.

## Patch 3 scoping (2026-09-24): the port-downgrade hack is not the only blocker

Started tracing what removing the `xhci_init()` port-downgrade block would
actually require, to see whether `sc_bus.usbrev` needs to change too.
Found a materially bigger problem than expected.

**`sc_bus.usbrev` itself is a red herring — leave it alone.** It's read
exactly once in the whole stack: `USB_ATTACH(usb)` in `dev/usb/c/usb`,
purely to classify the *root hub's own* nominal speed at bus-attach time.
Its `switch` has no `USBREV_3_0` case and a `default:` that's a hard
`ATTACH_ERROR` — so naively setting it to 3.0 would kill the root hub
outright. Good news: nothing else touches it. Per-device speed flows
entirely through the separate `speed` argument threaded through
`usbd_new_device()`/`dev->speed`, independent of `usbrev`. So patch 3
should leave `usbrev = USBREV_2_0` alone and only remove the port-disable
loop.

**But root-hub port status emulation never reports SuperSpeed at all.**
`XHCIDriver`'s control-transfer handler fakes hub-class responses for the
root hub (since xHCI root ports aren't a real USB hub device). The relevant
code (`c/xhci` ~line 3050-3160):

- The fake root hub descriptor reports `bNbrPorts = sc->sc_hs_port_count`
  — only the HS/USB2 port grouping. The paired SS ports
  (`sc_ss_port_start`/`sc_ss_port_count`, correctly parsed at init) are
  never exposed as root hub ports at all.
- `GET_STATUS` for a port always reads
  `XHCI_PORTSC(sc->sc_hs_port_start - 1 + index)` — exclusively the HS-side
  PORTSC register of the pair. The SS-side PORTSC is never read here.
- The speed-decode switch (`XHCI_PS_SPEED_GET(v)`) only has cases for
  1/2/3 (FS/LS/HS); no case 4 (SuperSpeed) — even if it did read the SS
  register, it has nowhere to put the answer.

Removing the port-downgrade hack in `xhci_init()` lets an SS device
actually **link** at SuperSpeed electrically, but the root hub's own fake
port-status response would still never tell the rest of the stack that
happened. This is the actual blocker for anything plugged straight into
the Pi4/Titanium's own sockets — bigger and different from the init-time
hack.

**Two attach code paths exist; only one is live on RISC OS.** `xhci.c`
carries a complete, upstream-NetBSD-style `xhci_new_device()` with
correctly-looking SS handling throughout (route/rhport math for SS ports,
slot setup with real speed, the MaxPacketSize0-as-exponent quirk) — but
it's compiled out (`#else` of `#ifdef RISCOS`) and dead on this platform.
RISC OS instead uses a 3-hook split (`xhci_new_device_pre/_addr/_post`,
backed by `xhci_new_device_common`) that calls into the generic
`usbd_new_device()` in `usb_subr.c` instead of owning the whole sequence.
`xhci_new_device_common` hardcodes the SS route string to `0`
unconditionally:

```c
err = xhci_init_slot(dev, slot, 0 /* USB3 route */, rhport);
```

`0` is actually correct for a device plugged directly into a root port
(no intermediate hub tiers to encode), but wrong for anything behind an
external SS hub — the loop above it walks up to find the root port number
but throws away each intermediate hub's own port number, which is exactly
what a non-zero route string needs to encode. Haven't yet confirmed how
much this actually matters in practice (need to check `xhci_init_slot`'s
signature — there appear to be two different signatures used in the dead
vs. live code paths, not yet reconciled) or how big a fix it is.

**Net effect on scope**: "flip the switch" is now at least two changes,
not one — (a) stop root hub GET_STATUS/descriptor emulation from hiding
the SS ports and add the missing speed-4 case, on top of (b) the original
port-downgrade removal — plus a probably-separate, lower-priority fix for
route strings behind external SS hubs. Haven't started writing code for
any of this yet; stopped to report scope growth before continuing.

## Patch 3 (root-port-direct SS only, as agreed)

Three changes, split as patches USBDriver/0003 and XHCIDriver/0002:

1. **`xhci_init()`**: removed the `#ifdef RISCOS` port-downgrade loop
   entirely. SS ports now link at whatever speed the device negotiates.
2. **New helper `xhci_rhport_reg(sc, index)`** in `XHCIDriver`: root hub
   port emulation is keyed by one logical port number, but a physical port
   pair has two xHCI register sets (HS side, SS side). This checks the SS
   side's CCS (connect status) bit first and returns that register if a
   device is linked there, else falls back to the HS side — replacing
   three separate call sites in the root-hub control-request emulation
   (`CLEAR_FEATURE`, `GET_STATUS`, `SET_FEATURE`) that previously read the
   HS-side register unconditionally.
3. **`GET_STATUS` speed decode**: added the missing `case 4: i =
   UPS_SUPER_SPEED` (previously only had cases for FS/LS/HS).
4. **`uhub.c`'s `uhub_explore()` speed decode**: found and fixed a real
   bug while adding the `USB_SPEED_SUPER` case — `UPS_HIGH_SPEED`,
   `UPS_LOW_SPEED`, `UPS_SUPER_SPEED` are not independent flag bits, they're
   a 2-bit field (`0x0600 = 0x0400|0x0200`), so `UPS_SUPER_SPEED` was a
   strict superset of `UPS_HIGH_SPEED`'s bit. The naive fix (just adding an
   `else if (status & UPS_SUPER_SPEED)` arm) would never have been reached
   — `if (status & UPS_HIGH_SPEED)` matches first and wrongly classifies
   any SS device as `USB_SPEED_HIGH`. Fixed by checking the full 2-bit
   field against `UPS_SUPER_SPEED` exactly, before the single-bit checks.

**Deliberately out of scope, per agreed decision**: the external-SS-hub
route-string bug in `xhci_new_device_common()` (hardcoded route=0). Devices
plugged straight into the Pi4/Titanium's own root ports should now be
reported as SuperSpeed correctly; devices behind an external SS hub are
unaffected by this patch (they'll behave as before, no worse).

Verified by full reconstruction: applied all five patches in sequence
(USBDriver 0001-0003, XHCIDriver 0001-0002) to clean checkouts of all five
touched files, byte-diffed against the actual working tree — identical.

**Not yet verified against real NetBSD or real hardware.** This patch is
materially riskier than 1+2 — it's the first one that changes observable
behavior rather than being inert, and I designed `xhci_rhport_reg()` from
first-principles reasoning about the xHCI port-pairing model rather than
copying a verified reference implementation (unlike patches 1+2, which
were checked against upstream NetBSD source directly). Needs a real
SuperSpeed device plugged into the CM4 to know if this actually works.

## Patch 3 build + regression verification (2026-09-24)

Builds clean, boots, and existing USB2 devices still work normally on the
CM4. Confirms `xhci_rhport_reg()` and the root hub emulation changes are
at least not breaking the HS/FS/LS path they now share logic with.
Still need a real SuperSpeed device plugged into the CM4's own USB3 port
to know whether the actual goal — SS devices reporting `Super` via
`*USBDevInfo` instead of being silently downgraded — is achieved.

## Correction: current test hardware has no XHCI controller at all

The CM4 board in use has no USB3 (xHCI) ports — only the DWC2 OTG
controller. `*USBDevices` confirmed this: every device including the test
flash drive shows up on Bus 1 (`Synopsys DWC OTG root hub`); no Bus 2
(`XHCIDriver`'s root hub) appears at all. All hardware testing done so far
(patches 1-3, multiple boots, keyboard/mouse/hub/storage all working) has
exercised the DWC2/USB2 path only — a genuinely useful regression test
for the `USBDriver` core changes (patches 1-3 all touch shared code paths
DWC2 devices go through too), but has not touched `XHCIDriver` at runtime
at all, so none of patch 3's actual SS-enablement logic has been exercised
on real hardware yet.

Real xHCI hardware (a USB3-capable Pi4 or similar) is needed to test any
of that. Paused here pending access to such hardware.

## Patch 4 (XHCIDriver/0003): route string for SS devices behind external hubs

Fills in the gap deliberately deferred when scoping patch 3
("root-port-direct SS only"). `xhci_new_device_common()` was passing a
hardcoded `route = 0` to `xhci_init_slot()` regardless of topology depth
— correct only for a device plugged directly into a root port.

Added `xhci_route_string()`: walks the `dev->myhub` chain, collecting each
hub's `powersrc->portno` (the port the next thing down is plugged into),
then packs them into the 20-bit Slot Context Route String field per xHCI
spec 4.3.3 / USB3 spec 8.9 — tier 1 (the hub nearest the root) in the
least significant nibble, deeper tiers in successive nibbles. Capped at 5
tiers (20 bits / 4 bits-per-nibble), matching the spec's own limit.

Worked through the nibble ordering carefully by hand-tracing a two-hub-deep
example, since walking the hub chain bottom-up (from the device towards
the root) naturally produces port numbers in the *reverse* of the order
they need to be packed in (deepest hub first, but tier 1 needs to land in
the least-significant nibble) — a first draft got this backwards, caught
by the trace before writing it down as a patch.

Gated on `dev->speed == USB_SPEED_SUPER`: route strings are an SS-hub-only
xHCI concept, meaningless for USB2 devices (which use hub+TT addressing
instead), so non-SS devices keep getting `route = 0` same as before.

Deliberately developed and verified (patch-apply reconstruction, all three
XHCIDriver patches in sequence) without touching the root-port-direct SS
path patch 3 already changed, so as not to conflate two untested behaviors
before patch 3 gets its first real xHCI hardware test. Not yet tested on
any hardware — needs an external SuperSpeed hub with a device behind it,
which is a rarer test setup than a bare USB3 flash drive.

## Diagnostic build (2026-09-25): Pi 400 shows High speed on both USB3 ports

Real hardware test on a Pi 400 (not the CM4 — different xHCI implementation,
`VIA XHCI root hub` rather than the CM4's controller): `*usbdevices` shows
Bus 2 (XHCI) present and working, but a `VIA Labs USB2.0 Hub` (device 3) is
always enumerated, and the SSK USB3.2 flash drive always reports
`Speed: High` regardless of which of the two USB3 ports it's plugged into
— identical device numbering both times (hub=3, flash drive=4, mouse
dongle=5, keyboard=6).

Couldn't determine from `*usbdevices`/`*usbbuses` alone whether the flash
drive is topologically a child of that USB2.0 hub (in which case `High` is
the *correct* answer — real hardware limitation, nothing to fix) or a
sibling directly on the root hub (in which case it's a bug in patch 3/4).
No built-in RISC OS command exposes hub/device parent-child topology.

Added temporary (not committed as a real patch — for diagnosis only)
unconditional `printf` instrumentation:
- In `xhci_init()`: dumps `sc_hs_port_start/count` and
  `sc_ss_port_start/count` once at boot, to sanity-check the extended
  capability parsing on this specific (VIA) xHCI implementation.
- In `xhci_rhport_reg()`: dumps both the SS-side and HS-side `PORTSC`
  register values (and their CCS bits) every time a root hub port is
  queried, which happens on every `*usbdevices`/`*usbdevinfo` call — so
  output appears directly alongside those commands' own output, no special
  log capture needed.

Waiting on a rebuild + `*usbdevinfo 4` capture to see the raw register
ground truth.

## Diagnostic v2 (2026-09-25): my printf approach didn't work, switched strategy

Learned two things from the failed attempt to capture `xhci_rhport_reg`'s
printf output:

1. `*usbdevices`/`*usbdevinfo` only print already-cached data from when a
   device was originally discovered — they never re-trigger a root hub
   port query. Asking to rerun them after the diagnostic build was based
   on a wrong assumption; my mistake.
2. RISC OS on this Pi doesn't show a visible boot text console (no splash
   screen text before the desktop), and hotplug (unplug/replug) doesn't
   visibly trigger anything either — so there was no window in which the
   `printf`-based diagnostic could ever be observed, even though the code
   almost certainly *did* run once already, during the original boot-time
   discovery that got the device classified as `High` in the first place.

Switched strategy to something self-contained in `USBDriver` (a codebase
already proven to build and produce visible `*command` output reliably in
a desktop Task Window), instead of anything in `XHCIDriver`:

- Added a temporary `diag_pstatus` field to `struct usbd_device`
  (`usbdivar.h`) — zero-initialised for free, since the struct is already
  `memset` on allocation.
- `uhub_explore()` now stashes the raw `wPortStatus` word it decoded
  `speed` from into the newly-created device's `diag_pstatus`, right after
  `usbd_new_device()` returns (`up->device` is already valid by then).
- `*USBDevInfo <n>`'s existing handler (`command_devinfo` in
  `build/c/usbmodule`) now prints that raw value as an extra
  `Raw port status (diag)` line.

This sidesteps the whole console-visibility problem: the value is captured
once at the device's original discovery (whenever that was) and stays
readable any time afterward through a command already proven to work.
If the low byte's bits 9:10 read `11` (i.e. `0x0600`/`0x0400`-ish region,
watch for `& 0x0600 == 0x0600`), the hardware genuinely reported
SuperSpeed and the decode bug is still downstream somewhere; if those bits
read `10` (`0x0400`, plain `UPS_HIGH_SPEED`), the port never linked at SS
at the hardware level at all — real electrical/negotiation limitation, not
a decode bug.

## Real-hardware result and a walked-back conclusion (2026-09-25)

`Raw port status (diag)` on the Pi 400 came back `0x0503`: bit 0 (connect),
bit 1 (enabled), bit 8 (power), and bit 10 only of the speed field — the
exact `UPS_HIGH_SPEED` pattern (`0x0400`), not `UPS_SUPER_SPEED` (`0x0600`,
which needs bits 9 *and* 10). A clean, unambiguous "genuinely High Speed"
status word, not a decode error.

But this status came from a *real* hub device (`VIA Labs USB2.0 Hub`, per
the `!USBDescriptors` topology tree, likely the VL805's own embedded
USB2-only hub function — "VIA Labs" is literally the VL805's manufacturer)
— not from `xhci_rhport_reg()` at all. That code only runs for devices
attached directly to the fake root hub; this device is a grandchild via a
real intervening hub, so none of patches 3/4 were exercised by this test.
Initially read this as "nothing to fix, real hardware limit" and said so.

**That conclusion doesn't survive the follow-up test.** Repeated on a Pi 4
(different board, though same VL805 chip family) with the flash drive in
a blue USB3 port: identical topology shape — still routed through an
internal `USB2.0 Hub` node. Pi4's blue ports are documented (Raspberry Pi
forums) as wiring SuperSpeed lanes *directly* to the xHCI root hub,
bypassing the internal hub entirely — only the HS/FS/LS *fallback*
signaling for those same physical ports still goes through that hub. That
matters: a USB3 device whose SS link training fails for any reason falls
back to HS-only operation, and when it does, it's indistinguishable from a
genuinely-2.0-only port in the topology tree — both show "behind the
internal hub, reporting High". Seeing this exact shape on two different
real controllers is a real signal that SS link training probably isn't
completing, not proof the boards are hardware-limited.

Un-answered so far: is the flash drive/cable itself flaky, or is
`xhci_init()`'s change insufficient to actually establish an SS link
(vs. just "no longer actively preventing" one)? Asked for a second
USB3 device/cable to rule out a device-specific issue.

## Diagnostic v3: root hub's own advertised port count

Went back to an earlier, still-unconfirmed hypothesis: `XHCIDriver`'s fake
root hub descriptor reports `bNbrPorts = sc->sc_hs_port_count` — only the
HS-grouped port range. If `hs_port_count` and `ss_port_count` differ on
real hardware (plausible: an embedded 2.0 hub consolidating several
physical ports' HS lines into one upstream port, while only some of those
same physical ports have genuine SS lanes wired separately), the root hub
could be silently under-reporting its own port count, making any SS-only
port structurally invisible to `uhub_explore()` regardless of anything
`xhci_rhport_reg()` does.

Extended the same working diagnostic technique (no printf, stash into
existing structs, read back via `*USBDevInfo`) to check this directly:
- `struct usbd_bus` (`usbdivar.h`) gets four new temporary fields:
  `diag_hs_start/count`, `diag_ss_start/count`.
- `xhci_init()` populates them right where the (unreachable) printf used
  to be try to report them.
- `command_devinfo` (`USBDriver`) now prints them for any device, plus a
  `Hub port count (diag)` line showing `hub->hubdesc.bNbrPorts` for any
  hub-class device queried — including the root hub itself, since it's
  device-numbered like any other hub in `*usbdevices`.

Next: run `*usbdevinfo` on the root hub entry itself (`VIA XHCI root hub`,
device 2 in the `*usbdevices` listing) to see its self-reported port
count against the real `hs_count`/`ss_count` split.
