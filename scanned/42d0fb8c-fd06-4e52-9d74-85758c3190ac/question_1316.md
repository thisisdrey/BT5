# Q1316: Signature verification gap for the UnsafeNetworks field in ip2int

## Question
Can an unprivileged attacker craft a certificate where the UnsafeNetworks field is not covered by the bytes actually signature-checked in `ip2int` (cert/cert_v1.go), so the field is attacker-chosen on an otherwise valid certificate?

## Target
- File/function: `cert/cert_v1.go` -> `ip2int` (declared at cert/cert_v1.go:483)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the UnsafeNetworks field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Take a legitimately CA-signed certificate, mutate the UnsafeNetworks field, and see whether `ip2int` still verifies it.
- Invariant to test: The signature verification input covers every field that any authorization decision later reads.
- Expected Immunefi impact: Certificate forgery: attacker grants itself arbitrary groups, networks, or validity while chaining to a real CA.
- Fast validation: Unit test mutating the UnsafeNetworks field on a signed fixture and asserting `ip2int` returns a verification error.
