# Q2973: Inner destination outside certificate scope in winTun.Read

## Question
Does `winTun.Read` (overlay/tun_windows.go) verify the inner destination for a packet destined at the tun device's own address against the local certificate's networks and configured routes before writing?

## Target
- File/function: `overlay/tun_windows.go` -> `winTun.Read` (declared at overlay/tun_windows.go:258)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: a packet destined at the tun device's own address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send a tunnel packet addressed outside the local overlay scope and see whether it is still written.
- Invariant to test: Written packets are confined to the node's own networks and explicitly configured unsafe routes.
- Expected Immunefi impact: Pivot into the host's physical network from the overlay.
- Fast validation: Integration test with an out-of-scope destination asserting `winTun.Read` drops it.
