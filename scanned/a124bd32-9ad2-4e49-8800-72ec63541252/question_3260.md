# Q3260: Parser resource blow-up in deriveKey

## Question
Can an empty Networks list make `deriveKey` (cert/crypto.go) allocate, recurse, or loop proportionally to attacker-declared sizes while parsing an untrusted certificate?

## Target
- File/function: `cert/crypto.go` -> `deriveKey` (declared at cert/crypto.go:129)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: an empty Networks list; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Submit a certificate with huge declared lengths or deeply nested structures in the handshake payload.
- Invariant to test: Certificate parsing has hard bounds on size, element count, and nesting, checked before allocation.
- Expected Immunefi impact: Remote memory exhaustion or CPU exhaustion of a node from a single unauthenticated handshake.
- Fast validation: Fuzz `deriveKey` with size-declaring mutations and assert bounded allocation and no stack overflow.
