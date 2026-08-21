# Q0992: Inner destination outside certificate scope in Interface.activate

## Question
Does `Interface.activate` (interface.go) verify the inner destination for a multicast/broadcast inner destination against the local certificate's networks and configured routes before writing?

## Target
- File/function: `interface.go` -> `Interface.activate` (declared at interface.go:227)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: a multicast/broadcast inner destination; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send a tunnel packet addressed outside the local overlay scope and see whether it is still written.
- Invariant to test: Written packets are confined to the node's own networks and explicitly configured unsafe routes.
- Expected Immunefi impact: Pivot into the host's physical network from the overlay.
- Fast validation: Integration test with an out-of-scope destination asserting `Interface.activate` drops it.
