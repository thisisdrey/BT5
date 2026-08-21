# Q1789: Verification result caching in TBSCertificate.Sign

## Question
Does `TBSCertificate.Sign` (cert/sign.go) cache or memoize verification keyed on a duplicated or out-of-order ASN.1 field in a way an attacker can poison so a later, different certificate reuses a cached 'valid' result?

## Target
- File/function: `cert/sign.go` -> `TBSCertificate.Sign` (declared at cert/sign.go:49)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a duplicated or out-of-order ASN.1 field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Submit a valid certificate, then one sharing the cache key but differing in content.
- Invariant to test: Any verification cache is keyed on the full certificate bytes and never on a partial or attacker-shaped subset.
- Expected Immunefi impact: Certificate verification bypass through cache-key collision.
- Fast validation: Unit test priming the cache via `TBSCertificate.Sign` then verifying a colliding-key certificate, asserting a fresh verification runs.
