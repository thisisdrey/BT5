# Q2967: Version confusion between v1 and v2 in unmarshalCertificateBlock

## Question
Can an attacker exploit the IsCA flag so `unmarshalCertificateBlock` (cert/pem.go) parses a certificate under one version's rules while another component authorizes it under the other's?

## Target
- File/function: `cert/pem.go` -> `unmarshalCertificateBlock` (declared at cert/pem.go:105)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the IsCA flag; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Craft bytes that parse as both versions with different field semantics and trace which interpretation reaches the firewall.
- Invariant to test: Certificate version is determined once, unambiguously, and the same parsed structure feeds every consumer.
- Expected Immunefi impact: Authorization bypass through field reinterpretation across certificate versions.
- Fast validation: Differential test parsing the same bytes through both v1 and v2 paths and asserting at most one succeeds.
