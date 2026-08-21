# Q1684: Panic on malformed certificate in TBSCertificate.SignWith

## Question
Can a malformed certificate containing a duplicated or out-of-order ASN.1 field panic `TBSCertificate.SignWith` (cert/sign.go) during handshake processing?

## Target
- File/function: `cert/sign.go` -> `TBSCertificate.SignWith` (declared at cert/sign.go:77)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a duplicated or out-of-order ASN.1 field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Fuzz the certificate parser with structurally invalid ASN.1/protobuf reachable from the handshake payload.
- Invariant to test: Certificate parsing never panics; all malformed input yields errors.
- Expected Immunefi impact: Single-packet remote denial of service of any node that accepts handshakes.
- Fast validation: Go fuzz target over `TBSCertificate.SignWith` seeded with valid certificates, asserting zero panics.
