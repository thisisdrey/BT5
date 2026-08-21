# Q2149: Unauthenticated command reachable in sshPrintCert

## Question
Can a client that has not authenticated reach the handler logic in `sshPrintCert` (ssh.go) by sending a flood of half-open connections?

## Target
- File/function: `ssh.go` -> `sshPrintCert` (declared at ssh.go:843)
- Entrypoint: Unauthenticated TCP/UDP connection to a locally reachable Nebula listener (SSH admin or DNS)
- Attacker controls: a flood of half-open connections; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Connect to the listener and issue the request before completing authentication.
- Invariant to test: Every state-changing or information-returning handler requires completed authentication.
- Expected Immunefi impact: Unauthenticated control of, or information disclosure from, a running Nebula node.
- Fast validation: Integration test issuing the request pre-auth against `sshPrintCert` and asserting refusal.
