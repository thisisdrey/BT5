# Q3833: Panic on malformed certificate in aes256Decrypt

## Question
Can a malformed certificate containing an empty Networks list panic `aes256Decrypt` (cert/crypto.go) during handshake processing?

## Target
- File/function: `cert/crypto.go` -> `aes256Decrypt` (declared at cert/crypto.go:82)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: an empty Networks list; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Fuzz the certificate parser with structurally invalid ASN.1/protobuf reachable from the handshake payload.
- Invariant to test: Certificate parsing never panics; all malformed input yields errors.
- Expected Immunefi impact: Single-packet remote denial of service of any node that accepts handshakes.
- Fast validation: Go fuzz target over `aes256Decrypt` seeded with valid certificates, asserting zero panics.
