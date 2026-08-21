# Q3900: Buffer reuse across writes in registerSublayer

## Question
Can an attacker observe or influence residual bytes from a previous packet through the buffer handling in `registerSublayer` (wfp/wfp_windows.go) using an MTU-sized packet forcing a split write?

## Target
- File/function: `wfp/wfp_windows.go` -> `registerSublayer` (declared at wfp/wfp_windows.go:268)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: an MTU-sized packet forcing a split write; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Alternate long and short packets and inspect the exact bytes handed to the device.
- Invariant to test: Each write covers exactly the current packet's bytes with no residue from prior packets.
- Expected Immunefi impact: Cross-tunnel data leakage between peers of the same node.
- Fast validation: Unit test alternating packet sizes and asserting `registerSublayer` writes exactly the expected slice.
