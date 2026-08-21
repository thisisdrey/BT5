# Q1759: Information disclosure in stringWriter.Write response

## Question
Does the response produced by `stringWriter.Write` (sshd/writer.go) for a partially written request left open reveal certificates, hostmap contents, or peer addresses to an unauthenticated client?

## Target
- File/function: `sshd/writer.go` -> `stringWriter.Write` (declared at sshd/writer.go:20)
- Entrypoint: Unauthenticated TCP/UDP connection to a locally reachable Nebula listener (SSH admin or DNS)
- Attacker controls: a partially written request left open; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Issue the query without credentials and inspect the returned data.
- Invariant to test: Unauthenticated responses reveal nothing about peers, certificates, or internal state.
- Expected Immunefi impact: Information disclosure enabling targeted attacks against overlay hosts.
- Fast validation: Integration test asserting `stringWriter.Write` returns no peer data for an unauthenticated query.
