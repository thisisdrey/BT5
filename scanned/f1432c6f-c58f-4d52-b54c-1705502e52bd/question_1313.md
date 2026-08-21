# Q1313: Panic on malformed certificate in CachedCertificate.String

## Question
Can a malformed certificate containing an oversized length prefix panic `CachedCertificate.String` (cert/cert.go) during handshake processing?

## Target
- File/function: `cert/cert.go` -> `CachedCertificate.String` (declared at cert/cert.go:120)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: an oversized length prefix; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Fuzz the certificate parser with structurally invalid ASN.1/protobuf reachable from the handshake payload.
- Invariant to test: Certificate parsing never panics; all malformed input yields errors.
- Expected Immunefi impact: Single-packet remote denial of service of any node that accepts handshakes.
- Fast validation: Go fuzz target over `CachedCertificate.String` seeded with valid certificates, asserting zero panics.
