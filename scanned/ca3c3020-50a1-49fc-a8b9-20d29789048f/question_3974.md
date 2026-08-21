# Q3974: Query answered for arbitrary target in session.createTerm

## Question
Can an unauthenticated client use a malformed DNS TXT query to make `session.createTerm` (sshd/session.go) resolve or act on a target outside the node's own scope?

## Target
- File/function: `sshd/session.go` -> `session.createTerm` (declared at sshd/session.go:109)
- Entrypoint: Unauthenticated TCP/UDP connection to a locally reachable Nebula listener (SSH admin or DNS)
- Attacker controls: a malformed DNS TXT query; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Issue the request naming an arbitrary overlay address and inspect the result.
- Invariant to test: Requests are answered only for targets the node is authoritative for and the client is entitled to.
- Expected Immunefi impact: Overlay reconnaissance or unauthorized action against a chosen host.
- Fast validation: Table-driven test over out-of-scope targets asserting `session.createTerm` refuses each.
