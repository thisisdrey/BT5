# Q2243: Unauthenticated command reachable in sshPrintRelays

## Question
Can a client that has not authenticated reach the handler logic in `sshPrintRelays` (ssh.go) by sending an unauthenticated command line?

## Target
- File/function: `ssh.go` -> `sshPrintRelays` (declared at ssh.go:898)
- Entrypoint: Unauthenticated TCP/UDP connection to a locally reachable Nebula listener (SSH admin or DNS)
- Attacker controls: an unauthenticated command line; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Connect to the listener and issue the request before completing authentication.
- Invariant to test: Every state-changing or information-returning handler requires completed authentication.
- Expected Immunefi impact: Unauthenticated control of, or information disclosure from, a running Nebula node.
- Fast validation: Integration test issuing the request pre-auth against `sshPrintRelays` and asserting refusal.
