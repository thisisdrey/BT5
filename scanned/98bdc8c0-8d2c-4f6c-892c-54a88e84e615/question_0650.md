# Q0650: Unauthenticated command reachable in sshListLighthouseMap

## Question
Can a client that has not authenticated reach the handler logic in `sshListLighthouseMap` (ssh.go) by sending a partially written request left open?

## Target
- File/function: `ssh.go` -> `sshListLighthouseMap` (declared at ssh.go:478)
- Entrypoint: Unauthenticated TCP/UDP connection to a locally reachable Nebula listener (SSH admin or DNS)
- Attacker controls: a partially written request left open; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Connect to the listener and issue the request before completing authentication.
- Invariant to test: Every state-changing or information-returning handler requires completed authentication.
- Expected Immunefi impact: Unauthenticated control of, or information disclosure from, a running Nebula node.
- Fast validation: Integration test issuing the request pre-auth against `sshListLighthouseMap` and asserting refusal.
