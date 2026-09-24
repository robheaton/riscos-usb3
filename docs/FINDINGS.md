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

## Remaining open question

- What NetBSD source tree/tag is the best "donor" for the BOS/SS-hub/
  SS-companion-descriptor parsing code — pulling from a NetBSD version
  close to 2015 (matching `XHCIDriver`'s vintage) to minimize unrelated
  diff noise, rather than the latest NetBSD, is probably the pragmatic
  choice. Not yet picked.
