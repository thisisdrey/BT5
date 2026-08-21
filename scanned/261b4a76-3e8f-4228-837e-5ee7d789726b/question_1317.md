# Q1317: Signature verification gap for the NotBefore/NotAfter window in int2ip

## Question
Can an unprivileged attacker craft a certificate where the NotBefore/NotAfter window is not covered by the bytes actually signature-checked in `int2ip` (cert/cert_v1.go), so the field is attacker-chosen on an otherwise valid certificate?

## Target
- File/function: `cert/cert_v1.go` -> `int2ip` (declared at cert/cert_v1.go:490)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the NotBefore/NotAfter window; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Take a legitimately CA-signed certificate, mutate the NotBefore/NotAfter window, and see whether `int2ip` still verifies it.
- Invariant to test: The signature verification input covers every field that any authorization decision later reads.
- Expected Immunefi impact: Certificate forgery: attacker grants itself arbitrary groups, networks, or validity while chaining to a real CA.
- Fast validation: Unit test mutating the NotBefore/NotAfter window on a signed fixture and asserting `int2ip` returns a verification error.
