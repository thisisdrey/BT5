# Q1544: Inner destination outside certificate scope in newSession

## Question
Does `newSession` (wfp/wfp_windows.go) verify the inner destination for a multicast/broadcast inner destination against the local certificate's networks and configured routes before writing?

## Target
- File/function: `wfp/wfp_windows.go` -> `newSession` (declared at wfp/wfp_windows.go:237)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: a multicast/broadcast inner destination; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send a tunnel packet addressed outside the local overlay scope and see whether it is still written.
- Invariant to test: Written packets are confined to the node's own networks and explicitly configured unsafe routes.
- Expected Immunefi impact: Pivot into the host's physical network from the overlay.
- Fast validation: Integration test with an out-of-scope destination asserting `newSession` drops it.
