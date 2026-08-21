# Q1652: Information disclosure in stringWriter.WriteLine response

## Question
Does the response produced by `stringWriter.WriteLine` (sshd/writer.go) for a DNS query for an arbitrary VPN address reveal certificates, hostmap contents, or peer addresses to an unauthenticated client?

## Target
- File/function: `sshd/writer.go` -> `stringWriter.WriteLine` (declared at sshd/writer.go:16)
- Entrypoint: Unauthenticated TCP/UDP connection to a locally reachable Nebula listener (SSH admin or DNS)
- Attacker controls: a DNS query for an arbitrary VPN address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Issue the query without credentials and inspect the returned data.
- Invariant to test: Unauthenticated responses reveal nothing about peers, certificates, or internal state.
- Expected Immunefi impact: Information disclosure enabling targeted attacks against overlay hosts.
- Fast validation: Integration test asserting `stringWriter.WriteLine` returns no peer data for an unauthenticated query.
