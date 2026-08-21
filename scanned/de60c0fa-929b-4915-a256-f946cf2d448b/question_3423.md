# Q3423: Parser resource blow-up in unmarshalArgon2Parameters

## Question
Can the NotBefore/NotAfter window make `unmarshalArgon2Parameters` (cert/crypto.go) allocate, recurse, or loop proportionally to attacker-declared sizes while parsing an untrusted certificate?

## Target
- File/function: `cert/crypto.go` -> `unmarshalArgon2Parameters` (declared at cert/crypto.go:229)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the NotBefore/NotAfter window; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Submit a certificate with huge declared lengths or deeply nested structures in the handshake payload.
- Invariant to test: Certificate parsing has hard bounds on size, element count, and nesting, checked before allocation.
- Expected Immunefi impact: Remote memory exhaustion or CPU exhaustion of a node from a single unauthenticated handshake.
- Fast validation: Fuzz `unmarshalArgon2Parameters` with size-declaring mutations and assert bounded allocation and no stack overflow.
