# Q1137: Buffer reuse across writes in Route.Equal

## Question
Can an attacker observe or influence residual bytes from a previous packet through the buffer handling in `Route.Equal` (overlay/route.go) using a multicast/broadcast inner destination?

## Target
- File/function: `overlay/route.go` -> `Route.Equal` (declared at overlay/route.go:27)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: a multicast/broadcast inner destination; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Alternate long and short packets and inspect the exact bytes handed to the device.
- Invariant to test: Each write covers exactly the current packet's bytes with no residue from prior packets.
- Expected Immunefi impact: Cross-tunnel data leakage between peers of the same node.
- Fast validation: Unit test alternating packet sizes and asserting `Route.Equal` writes exactly the expected slice.
