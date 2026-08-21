# Q3982: Buffer reuse across writes in addInterfaceFilter

## Question
Can an attacker observe or influence residual bytes from a previous packet through the buffer handling in `addInterfaceFilter` (wfp/wfp_windows.go) using a packet destined at the tun device's own address?

## Target
- File/function: `wfp/wfp_windows.go` -> `addInterfaceFilter` (declared at wfp/wfp_windows.go:292)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: a packet destined at the tun device's own address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Alternate long and short packets and inspect the exact bytes handed to the device.
- Invariant to test: Each write covers exactly the current packet's bytes with no residue from prior packets.
- Expected Immunefi impact: Cross-tunnel data leakage between peers of the same node.
- Fast validation: Unit test alternating packet sizes and asserting `addInterfaceFilter` writes exactly the expected slice.
