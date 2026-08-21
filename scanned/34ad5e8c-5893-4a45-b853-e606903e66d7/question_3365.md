# Q3365: Buffer reuse across writes in Interface.Close

## Question
Can an attacker observe or influence residual bytes from a previous packet through the buffer handling in `Interface.Close` (interface.go) using a route with an attacker-influenced metric?

## Target
- File/function: `interface.go` -> `Interface.Close` (declared at interface.go:555)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: a route with an attacker-influenced metric; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Alternate long and short packets and inspect the exact bytes handed to the device.
- Invariant to test: Each write covers exactly the current packet's bytes with no residue from prior packets.
- Expected Immunefi impact: Cross-tunnel data leakage between peers of the same node.
- Fast validation: Unit test alternating packet sizes and asserting `Interface.Close` writes exactly the expected slice.
