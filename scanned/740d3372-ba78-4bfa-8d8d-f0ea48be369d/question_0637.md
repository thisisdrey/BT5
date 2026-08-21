# Q0637: Signature verification gap for the IsCA flag in MarshalPublicKeyToPEM

## Question
Can an unprivileged attacker craft a certificate where the IsCA flag is not covered by the bytes actually signature-checked in `MarshalPublicKeyToPEM` (cert/pem.go), so the field is attacker-chosen on an otherwise valid certificate?

## Target
- File/function: `cert/pem.go` -> `MarshalPublicKeyToPEM` (declared at cert/pem.go:127)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the IsCA flag; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Take a legitimately CA-signed certificate, mutate the IsCA flag, and see whether `MarshalPublicKeyToPEM` still verifies it.
- Invariant to test: The signature verification input covers every field that any authorization decision later reads.
- Expected Immunefi impact: Certificate forgery: attacker grants itself arbitrary groups, networks, or validity while chaining to a real CA.
- Fast validation: Unit test mutating the IsCA flag on a signed fixture and asserting `MarshalPublicKeyToPEM` returns a verification error.
