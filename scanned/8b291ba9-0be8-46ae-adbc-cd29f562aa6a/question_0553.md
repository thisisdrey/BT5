# Q0553: Signature verification gap for an empty Networks list in certificateV2.UnsafeNetworks

## Question
Can an unprivileged attacker craft a certificate where an empty Networks list is not covered by the bytes actually signature-checked in `certificateV2.UnsafeNetworks` (cert/cert_v2.go), so the field is attacker-chosen on an otherwise valid certificate?

## Target
- File/function: `cert/cert_v2.go` -> `certificateV2.UnsafeNetworks` (declared at cert/cert_v2.go:125)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: an empty Networks list; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Take a legitimately CA-signed certificate, mutate an empty Networks list, and see whether `certificateV2.UnsafeNetworks` still verifies it.
- Invariant to test: The signature verification input covers every field that any authorization decision later reads.
- Expected Immunefi impact: Certificate forgery: attacker grants itself arbitrary groups, networks, or validity while chaining to a real CA.
- Fast validation: Unit test mutating an empty Networks list on a signed fixture and asserting `certificateV2.UnsafeNetworks` returns a verification error.
