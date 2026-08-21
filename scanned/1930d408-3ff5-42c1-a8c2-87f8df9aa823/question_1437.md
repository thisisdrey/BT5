# Q1437: Signature verification gap for the Curve field in int2addr

## Question
Can an unprivileged attacker craft a certificate where the Curve field is not covered by the bytes actually signature-checked in `int2addr` (cert/cert_v1.go), so the field is attacker-chosen on an otherwise valid certificate?

## Target
- File/function: `cert/cert_v1.go` -> `int2addr` (declared at cert/cert_v1.go:501)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the Curve field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Take a legitimately CA-signed certificate, mutate the Curve field, and see whether `int2addr` still verifies it.
- Invariant to test: The signature verification input covers every field that any authorization decision later reads.
- Expected Immunefi impact: Certificate forgery: attacker grants itself arbitrary groups, networks, or validity while chaining to a real CA.
- Fast validation: Unit test mutating the Curve field on a signed fixture and asserting `int2addr` returns a verification error.
