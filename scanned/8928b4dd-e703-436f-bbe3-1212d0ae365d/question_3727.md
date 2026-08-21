# Q3727: Information disclosure in SSHServer.AddAuthorizedKey response

## Question
Does the response produced by `SSHServer.AddAuthorizedKey` (sshd/server.go) for a malformed DNS TXT query reveal certificates, hostmap contents, or peer addresses to an unauthenticated client?

## Target
- File/function: `sshd/server.go` -> `SSHServer.AddAuthorizedKey` (declared at sshd/server.go:141)
- Entrypoint: Unauthenticated TCP/UDP connection to a locally reachable Nebula listener (SSH admin or DNS)
- Attacker controls: a malformed DNS TXT query; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Issue the query without credentials and inspect the returned data.
- Invariant to test: Unauthenticated responses reveal nothing about peers, certificates, or internal state.
- Expected Immunefi impact: Information disclosure enabling targeted attacks against overlay hosts.
- Fast validation: Integration test asserting `SSHServer.AddAuthorizedKey` returns no peer data for an unauthenticated query.
