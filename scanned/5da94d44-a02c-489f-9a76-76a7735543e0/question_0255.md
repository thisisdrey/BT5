# Q0255: Unauthenticated command reachable in SSHServer.SetHostKey

## Question
Can a client that has not authenticated reach the handler logic in `SSHServer.SetHostKey` (sshd/server.go) by sending an oversized request line?

## Target
- File/function: `sshd/server.go` -> `SSHServer.SetHostKey` (declared at sshd/server.go:104)
- Entrypoint: Unauthenticated TCP/UDP connection to a locally reachable Nebula listener (SSH admin or DNS)
- Attacker controls: an oversized request line; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Connect to the listener and issue the request before completing authentication.
- Invariant to test: Every state-changing or information-returning handler requires completed authentication.
- Expected Immunefi impact: Unauthenticated control of, or information disclosure from, a running Nebula node.
- Fast validation: Integration test issuing the request pre-auth against `SSHServer.SetHostKey` and asserting refusal.
