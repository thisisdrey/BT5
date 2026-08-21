# Q1258: Buffer reuse across writes in parseRoutes

## Question
Can an attacker observe or influence residual bytes from a previous packet through the buffer handling in `parseRoutes` (overlay/route.go) using an inner packet destined outside the certificate's networks?

## Target
- File/function: `overlay/route.go` -> `parseRoutes` (declared at overlay/route.go:70)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: an inner packet destined outside the certificate's networks; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Alternate long and short packets and inspect the exact bytes handed to the device.
- Invariant to test: Each write covers exactly the current packet's bytes with no residue from prior packets.
- Expected Immunefi impact: Cross-tunnel data leakage between peers of the same node.
- Fast validation: Unit test alternating packet sizes and asserting `parseRoutes` writes exactly the expected slice.
