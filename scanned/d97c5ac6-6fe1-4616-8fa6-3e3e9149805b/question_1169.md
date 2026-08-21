# Q1169: Unauthenticated command reachable in SSHServer.run

## Question
Can a client that has not authenticated reach the handler logic in `SSHServer.run` (sshd/server.go) by sending a malformed DNS TXT query?

## Target
- File/function: `sshd/server.go` -> `SSHServer.run` (declared at sshd/server.go:210)
- Entrypoint: Unauthenticated TCP/UDP connection to a locally reachable Nebula listener (SSH admin or DNS)
- Attacker controls: a malformed DNS TXT query; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Connect to the listener and issue the request before completing authentication.
- Invariant to test: Every state-changing or information-returning handler requires completed authentication.
- Expected Immunefi impact: Unauthenticated control of, or information disclosure from, a running Nebula node.
- Fast validation: Integration test issuing the request pre-auth against `SSHServer.run` and asserting refusal.
