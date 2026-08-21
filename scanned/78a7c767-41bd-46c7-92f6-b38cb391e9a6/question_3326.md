# Q3326: MTU/split write handling in addInterfaceFilter

## Question
Can an MTU-sized packet forcing a split write at or beyond the MTU cause `addInterfaceFilter` (wfp/wfp_windows.go) to write a truncated, mis-split, or oversized frame to the device?

## Target
- File/function: `wfp/wfp_windows.go` -> `addInterfaceFilter` (declared at wfp/wfp_windows.go:292)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: an MTU-sized packet forcing a split write; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send packets sized exactly at MTU-1, MTU, and MTU+1 and inspect device writes.
- Invariant to test: Frames written to the device are always complete and within the device MTU.
- Expected Immunefi impact: Node crash or corrupted delivery of attacker-influenced traffic into the host stack.
- Fast validation: Table-driven unit test over MTU boundary sizes against `addInterfaceFilter`.
