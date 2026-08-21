# Q2805: Information disclosure in session.createTerm response

## Question
Does the response produced by `session.createTerm` (sshd/session.go) for an unauthenticated command line reveal certificates, hostmap contents, or peer addresses to an unauthenticated client?

## Target
- File/function: `sshd/session.go` -> `session.createTerm` (declared at sshd/session.go:109)
- Entrypoint: Unauthenticated TCP/UDP connection to a locally reachable Nebula listener (SSH admin or DNS)
- Attacker controls: an unauthenticated command line; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Issue the query without credentials and inspect the returned data.
- Invariant to test: Unauthenticated responses reveal nothing about peers, certificates, or internal state.
- Expected Immunefi impact: Information disclosure enabling targeted attacks against overlay hosts.
- Fast validation: Integration test asserting `session.createTerm` returns no peer data for an unauthenticated query.
