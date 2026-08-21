# Q0803: Curve/public-key mismatch in readOptionalASN1Boolean

## Question
Can an attacker present a v2 certificate presented where v1 is expected so `readOptionalASN1Boolean` (cert/asn1.go) verifies a signature using a curve or key length that does not match the key material actually embedded?

## Target
- File/function: `cert/asn1.go` -> `readOptionalASN1Boolean` (declared at cert/asn1.go:10)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a v2 certificate presented where v1 is expected; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Declare one curve while embedding a key/signature for another and observe verification.
- Invariant to test: Declared curve, key length, and signature algorithm must agree, and any mismatch is a hard failure.
- Expected Immunefi impact: Signature verification bypass leading to accepted forged certificates.
- Fast validation: Table-driven unit test over curve/key mismatches asserting `readOptionalASN1Boolean` errors on each.
