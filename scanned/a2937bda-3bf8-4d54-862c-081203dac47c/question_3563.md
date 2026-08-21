# Q3563: Information disclosure in SSHServer.ClearAuthorizedKeys response

## Question
Does the response produced by `SSHServer.ClearAuthorizedKeys` (sshd/server.go) for an unauthenticated command line reveal certificates, hostmap contents, or peer addresses to an unauthenticated client?

## Target
- File/function: `sshd/server.go` -> `SSHServer.ClearAuthorizedKeys` (declared at sshd/server.go:120)
- Entrypoint: Unauthenticated TCP/UDP connection to a locally reachable Nebula listener (SSH admin or DNS)
- Attacker controls: an unauthenticated command line; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Issue the query without credentials and inspect the returned data.
- Invariant to test: Unauthenticated responses reveal nothing about peers, certificates, or internal state.
- Expected Immunefi impact: Information disclosure enabling targeted attacks against overlay hosts.
- Fast validation: Integration test asserting `SSHServer.ClearAuthorizedKeys` returns no peer data for an unauthenticated query.
