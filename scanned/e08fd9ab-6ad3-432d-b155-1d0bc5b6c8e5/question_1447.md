# Q1447: Parser resource blow-up in comparePrefix

## Question
Can an oversized length prefix make `comparePrefix` (cert/sign.go) allocate, recurse, or loop proportionally to attacker-declared sizes while parsing an untrusted certificate?

## Target
- File/function: `cert/sign.go` -> `comparePrefix` (declared at cert/sign.go:151)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: an oversized length prefix; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Submit a certificate with huge declared lengths or deeply nested structures in the handshake payload.
- Invariant to test: Certificate parsing has hard bounds on size, element count, and nesting, checked before allocation.
- Expected Immunefi impact: Remote memory exhaustion or CPU exhaustion of a node from a single unauthenticated handshake.
- Fast validation: Fuzz `comparePrefix` with size-declaring mutations and assert bounded allocation and no stack overflow.
