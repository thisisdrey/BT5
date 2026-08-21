# Q1069: Parser resource blow-up in CachedCertificate.String

## Question
Can a v1 certificate presented where v2 is expected make `CachedCertificate.String` (cert/cert.go) allocate, recurse, or loop proportionally to attacker-declared sizes while parsing an untrusted certificate?

## Target
- File/function: `cert/cert.go` -> `CachedCertificate.String` (declared at cert/cert.go:120)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a v1 certificate presented where v2 is expected; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Submit a certificate with huge declared lengths or deeply nested structures in the handshake payload.
- Invariant to test: Certificate parsing has hard bounds on size, element count, and nesting, checked before allocation.
- Expected Immunefi impact: Remote memory exhaustion or CPU exhaustion of a node from a single unauthenticated handshake.
- Fast validation: Fuzz `CachedCertificate.String` with size-declaring mutations and assert bounded allocation and no stack overflow.
