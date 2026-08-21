# Q1987: Constraint check applied to wrong field in TBSCertificate.Sign

## Question
Does `TBSCertificate.Sign` (cert/sign.go) validate an empty Networks list against the certificate that actually signed the session, or against a differently-sourced copy an attacker can influence?

## Target
- File/function: `cert/sign.go` -> `TBSCertificate.Sign` (declared at cert/sign.go:49)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: an empty Networks list; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Provide divergent copies of the certificate data and trace which one `TBSCertificate.Sign` reads.
- Invariant to test: Every authorization check reads the single verified certificate object bound to the session.
- Expected Immunefi impact: Authorization bypass using an unverified copy of the identity data.
- Fast validation: Unit test with divergent verified/unverified copies asserting `TBSCertificate.Sign` reads the verified one.
