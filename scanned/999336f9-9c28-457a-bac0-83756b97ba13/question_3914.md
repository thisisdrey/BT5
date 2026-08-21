# Q3914: Panic on malformed certificate in deriveKey

## Question
Can a malformed certificate containing the Groups field panic `deriveKey` (cert/crypto.go) during handshake processing?

## Target
- File/function: `cert/crypto.go` -> `deriveKey` (declared at cert/crypto.go:129)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the Groups field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Fuzz the certificate parser with structurally invalid ASN.1/protobuf reachable from the handshake payload.
- Invariant to test: Certificate parsing never panics; all malformed input yields errors.
- Expected Immunefi impact: Single-packet remote denial of service of any node that accepts handshakes.
- Fast validation: Go fuzz target over `deriveKey` seeded with valid certificates, asserting zero panics.
