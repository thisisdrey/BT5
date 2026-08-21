# Q0513: MTU/split write handling in ioctl

## Question
Can a packet destined at the tun device's own address at or beyond the MTU cause `ioctl` (overlay/tun_notwin.go) to write a truncated, mis-split, or oversized frame to the device?

## Target
- File/function: `overlay/tun_notwin.go` -> `ioctl` (declared at overlay/tun_notwin.go:8)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: a packet destined at the tun device's own address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send packets sized exactly at MTU-1, MTU, and MTU+1 and inspect device writes.
- Invariant to test: Frames written to the device are always complete and within the device MTU.
- Expected Immunefi impact: Node crash or corrupted delivery of attacker-influenced traffic into the host stack.
- Fast validation: Table-driven unit test over MTU boundary sizes against `ioctl`.
