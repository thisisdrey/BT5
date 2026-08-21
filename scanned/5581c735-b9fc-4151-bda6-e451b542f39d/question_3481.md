# Q3481: Information disclosure in SSHServer.ClearTrustedCAs response

## Question
Does the response produced by `SSHServer.ClearTrustedCAs` (sshd/server.go) for a flood of half-open connections reveal certificates, hostmap contents, or peer addresses to an unauthenticated client?

## Target
- File/function: `sshd/server.go` -> `SSHServer.ClearTrustedCAs` (declared at sshd/server.go:114)
- Entrypoint: Unauthenticated TCP/UDP connection to a locally reachable Nebula listener (SSH admin or DNS)
- Attacker controls: a flood of half-open connections; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Issue the query without credentials and inspect the returned data.
- Invariant to test: Unauthenticated responses reveal nothing about peers, certificates, or internal state.
- Expected Immunefi impact: Information disclosure enabling targeted attacks against overlay hosts.
- Fast validation: Integration test asserting `SSHServer.ClearTrustedCAs` returns no peer data for an unauthenticated query.
