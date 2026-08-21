# Q0007: Signature verification gap for the Groups field in readOptionalASN1Boolean

## Question
Can an unprivileged attacker craft a certificate where the Groups field is not covered by the bytes actually signature-checked in `readOptionalASN1Boolean` (cert/asn1.go), so the field is attacker-chosen on an otherwise valid certificate?

## Target
- File/function: `cert/asn1.go` -> `readOptionalASN1Boolean` (declared at cert/asn1.go:10)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the Groups field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Take a legitimately CA-signed certificate, mutate the Groups field, and see whether `readOptionalASN1Boolean` still verifies it.
- Invariant to test: The signature verification input covers every field that any authorization decision later reads.
- Expected Immunefi impact: Certificate forgery: attacker grants itself arbitrary groups, networks, or validity while chaining to a real CA.
- Fast validation: Unit test mutating the Groups field on a signed fixture and asserting `readOptionalASN1Boolean` returns a verification error.
