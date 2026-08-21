# Q1065: Constraint check applied to wrong field in readOptionalASN1Byte

## Question
Does `readOptionalASN1Byte` (cert/asn1.go) validate a self-signed certificate against the certificate that actually signed the session, or against a differently-sourced copy an attacker can influence?

## Target
- File/function: `cert/asn1.go` -> `readOptionalASN1Byte` (declared at cert/asn1.go:33)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a self-signed certificate; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Provide divergent copies of the certificate data and trace which one `readOptionalASN1Byte` reads.
- Invariant to test: Every authorization check reads the single verified certificate object bound to the session.
- Expected Immunefi impact: Authorization bypass using an unverified copy of the identity data.
- Fast validation: Unit test with divergent verified/unverified copies asserting `readOptionalASN1Byte` reads the verified one.
