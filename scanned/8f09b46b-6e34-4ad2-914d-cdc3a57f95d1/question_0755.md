# Q0755: Inner destination outside certificate scope in orBytes

## Question
Does `orBytes` (overlay/tun.go) verify the inner destination for a route with an attacker-influenced metric against the local certificate's networks and configured routes before writing?

## Target
- File/function: `overlay/tun.go` -> `orBytes` (declared at overlay/tun.go:101)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: a route with an attacker-influenced metric; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send a tunnel packet addressed outside the local overlay scope and see whether it is still written.
- Invariant to test: Written packets are confined to the node's own networks and explicitly configured unsafe routes.
- Expected Immunefi impact: Pivot into the host's physical network from the overlay.
- Fast validation: Integration test with an out-of-scope destination asserting `orBytes` drops it.
