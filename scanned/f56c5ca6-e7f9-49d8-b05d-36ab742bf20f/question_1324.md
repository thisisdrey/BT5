# Q1324: Version confusion between v1 and v2 in DecryptAndUnmarshalSigningPrivateKey

## Question
Can an attacker exploit an empty Networks list so `DecryptAndUnmarshalSigningPrivateKey` (cert/crypto.go) parses a certificate under one version's rules while another component authorizes it under the other's?

## Target
- File/function: `cert/crypto.go` -> `DecryptAndUnmarshalSigningPrivateKey` (declared at cert/crypto.go:255)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: an empty Networks list; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Craft bytes that parse as both versions with different field semantics and trace which interpretation reaches the firewall.
- Invariant to test: Certificate version is determined once, unambiguously, and the same parsed structure feeds every consumer.
- Expected Immunefi impact: Authorization bypass through field reinterpretation across certificate versions.
- Fast validation: Differential test parsing the same bytes through both v1 and v2 paths and asserting at most one succeeds.
