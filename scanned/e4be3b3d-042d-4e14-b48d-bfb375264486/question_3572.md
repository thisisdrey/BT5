# Q3572: Buffer reuse across writes in PermitInterface

## Question
Can an attacker observe or influence residual bytes from a previous packet through the buffer handling in `PermitInterface` (wfp/wfp_windows.go) using a zero-length inner payload?

## Target
- File/function: `wfp/wfp_windows.go` -> `PermitInterface` (declared at wfp/wfp_windows.go:201)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: a zero-length inner payload; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Alternate long and short packets and inspect the exact bytes handed to the device.
- Invariant to test: Each write covers exactly the current packet's bytes with no residue from prior packets.
- Expected Immunefi impact: Cross-tunnel data leakage between peers of the same node.
- Fast validation: Unit test alternating packet sizes and asserting `PermitInterface` writes exactly the expected slice.
