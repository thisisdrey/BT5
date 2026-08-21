# Q1868: Inner destination outside certificate scope in nanotime

## Question
Does `nanotime` (wintun/tun.go) verify the inner destination for an MTU-sized packet forcing a split write against the local certificate's networks and configured routes before writing?

## Target
- File/function: `wintun/tun.go` -> `nanotime` (declared at wintun/tun.go:60)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: an MTU-sized packet forcing a split write; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send a tunnel packet addressed outside the local overlay scope and see whether it is still written.
- Invariant to test: Written packets are confined to the node's own networks and explicitly configured unsafe routes.
- Expected Immunefi impact: Pivot into the host's physical network from the overlay.
- Fast validation: Integration test with an out-of-scope destination asserting `nanotime` drops it.
