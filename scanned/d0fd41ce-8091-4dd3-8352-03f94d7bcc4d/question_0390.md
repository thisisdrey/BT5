# Q0390: Unauthenticated command reachable in session.handleRequests

## Question
Can a client that has not authenticated reach the handler logic in `session.handleRequests` (sshd/session.go) by sending a malformed DNS TXT query?

## Target
- File/function: `sshd/session.go` -> `session.handleRequests` (declared at sshd/session.go:63)
- Entrypoint: Unauthenticated TCP/UDP connection to a locally reachable Nebula listener (SSH admin or DNS)
- Attacker controls: a malformed DNS TXT query; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Connect to the listener and issue the request before completing authentication.
- Invariant to test: Every state-changing or information-returning handler requires completed authentication.
- Expected Immunefi impact: Unauthenticated control of, or information disclosure from, a running Nebula node.
- Fast validation: Integration test issuing the request pre-auth against `session.handleRequests` and asserting refusal.
