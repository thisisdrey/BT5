# Q3915: Panic on malformed certificate in joinNonceCiphertext

## Question
Can a malformed certificate containing the Networks field panic `joinNonceCiphertext` (cert/crypto.go) during handshake processing?

## Target
- File/function: `cert/crypto.go` -> `joinNonceCiphertext` (declared at cert/crypto.go:146)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the Networks field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Fuzz the certificate parser with structurally invalid ASN.1/protobuf reachable from the handshake payload.
- Invariant to test: Certificate parsing never panics; all malformed input yields errors.
- Expected Immunefi impact: Single-packet remote denial of service of any node that accepts handshakes.
- Fast validation: Go fuzz target over `joinNonceCiphertext` seeded with valid certificates, asserting zero panics.
