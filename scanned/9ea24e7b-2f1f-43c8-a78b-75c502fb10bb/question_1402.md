# Q1402: Inner destination outside certificate scope in disabledTun.Networks

## Question
Does `disabledTun.Networks` (overlay/tun_disabled.go) verify the inner destination for an MTU-sized packet forcing a split write against the local certificate's networks and configured routes before writing?

## Target
- File/function: `overlay/tun_disabled.go` -> `disabledTun.Networks` (declared at overlay/tun_disabled.go:52)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: an MTU-sized packet forcing a split write; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send a tunnel packet addressed outside the local overlay scope and see whether it is still written.
- Invariant to test: Written packets are confined to the node's own networks and explicitly configured unsafe routes.
- Expected Immunefi impact: Pivot into the host's physical network from the overlay.
- Fast validation: Integration test with an out-of-scope destination asserting `disabledTun.Networks` drops it.
