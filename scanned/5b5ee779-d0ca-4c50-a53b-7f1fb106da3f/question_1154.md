# Q1154: Signature verification gap for a v1 certificate presented where v2 is expected in addASN1IntBytes

## Question
Can an unprivileged attacker craft a certificate where a v1 certificate presented where v2 is expected is not covered by the bytes actually signature-checked in `addASN1IntBytes` (cert/p256/p256.go), so the field is attacker-chosen on an otherwise valid certificate?

## Target
- File/function: `cert/p256/p256.go` -> `addASN1IntBytes` (declared at cert/p256/p256.go:113)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a v1 certificate presented where v2 is expected; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Take a legitimately CA-signed certificate, mutate a v1 certificate presented where v2 is expected, and see whether `addASN1IntBytes` still verifies it.
- Invariant to test: The signature verification input covers every field that any authorization decision later reads.
- Expected Immunefi impact: Certificate forgery: attacker grants itself arbitrary groups, networks, or validity while chaining to a real CA.
- Fast validation: Unit test mutating a v1 certificate presented where v2 is expected on a signed fixture and asserting `addASN1IntBytes` returns a verification error.
