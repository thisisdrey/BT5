# Q3137: Version confusion between v1 and v2 in MarshalPublicKeyToPEM

## Question
Can an attacker exploit the issuer/CA fingerprint so `MarshalPublicKeyToPEM` (cert/pem.go) parses a certificate under one version's rules while another component authorizes it under the other's?

## Target
- File/function: `cert/pem.go` -> `MarshalPublicKeyToPEM` (declared at cert/pem.go:127)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the issuer/CA fingerprint; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Craft bytes that parse as both versions with different field semantics and trace which interpretation reaches the firewall.
- Invariant to test: Certificate version is determined once, unambiguously, and the same parsed structure feeds every consumer.
- Expected Immunefi impact: Authorization bypass through field reinterpretation across certificate versions.
- Fast validation: Differential test parsing the same bytes through both v1 and v2 paths and asserting at most one succeeds.
