# Q3998: Panic on malformed certificate in unmarshalArgon2Parameters

## Question
Can a malformed certificate containing the Curve field panic `unmarshalArgon2Parameters` (cert/crypto.go) during handshake processing?

## Target
- File/function: `cert/crypto.go` -> `unmarshalArgon2Parameters` (declared at cert/crypto.go:229)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the Curve field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Fuzz the certificate parser with structurally invalid ASN.1/protobuf reachable from the handshake payload.
- Invariant to test: Certificate parsing never panics; all malformed input yields errors.
- Expected Immunefi impact: Single-packet remote denial of service of any node that accepts handshakes.
- Fast validation: Go fuzz target over `unmarshalArgon2Parameters` seeded with valid certificates, asserting zero panics.
