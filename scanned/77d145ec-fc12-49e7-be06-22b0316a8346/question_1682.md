# Q1682: Curve/public-key mismatch in findDuplicatePrefix

## Question
Can an attacker present an empty Networks list so `findDuplicatePrefix` (cert/sign.go) verifies a signature using a curve or key length that does not match the key material actually embedded?

## Target
- File/function: `cert/sign.go` -> `findDuplicatePrefix` (declared at cert/sign.go:160)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: an empty Networks list; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Declare one curve while embedding a key/signature for another and observe verification.
- Invariant to test: Declared curve, key length, and signature algorithm must agree, and any mismatch is a hard failure.
- Expected Immunefi impact: Signature verification bypass leading to accepted forged certificates.
- Fast validation: Table-driven unit test over curve/key mismatches asserting `findDuplicatePrefix` errors on each.
