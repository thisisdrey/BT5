# Q1260: Buffer reuse across writes in ipWithin

## Question
Can an attacker observe or influence residual bytes from a previous packet through the buffer handling in `ipWithin` (overlay/route.go) using an MTU-sized packet forcing a split write?

## Target
- File/function: `overlay/route.go` -> `ipWithin` (declared at overlay/route.go:312)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: an MTU-sized packet forcing a split write; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Alternate long and short packets and inspect the exact bytes handed to the device.
- Invariant to test: Each write covers exactly the current packet's bytes with no residue from prior packets.
- Expected Immunefi impact: Cross-tunnel data leakage between peers of the same node.
- Fast validation: Unit test alternating packet sizes and asserting `ipWithin` writes exactly the expected slice.
