# Q0413: Signature verification gap for an oversized length prefix in CAPool.verify

## Question
Can an unprivileged attacker craft a certificate where an oversized length prefix is not covered by the bytes actually signature-checked in `CAPool.verify` (cert/ca_pool.go), so the field is attacker-chosen on an otherwise valid certificate?

## Target
- File/function: `cert/ca_pool.go` -> `CAPool.verify` (declared at cert/ca_pool.go:210)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: an oversized length prefix; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Take a legitimately CA-signed certificate, mutate an oversized length prefix, and see whether `CAPool.verify` still verifies it.
- Invariant to test: The signature verification input covers every field that any authorization decision later reads.
- Expected Immunefi impact: Certificate forgery: attacker grants itself arbitrary groups, networks, or validity while chaining to a real CA.
- Fast validation: Unit test mutating an oversized length prefix on a signed fixture and asserting `CAPool.verify` returns a verification error.
