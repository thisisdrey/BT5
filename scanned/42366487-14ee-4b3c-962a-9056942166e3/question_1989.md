# Q1989: Constraint check applied to wrong field in comparePrefix

## Question
Does `comparePrefix` (cert/sign.go) validate the Groups field against the certificate that actually signed the session, or against a differently-sourced copy an attacker can influence?

## Target
- File/function: `cert/sign.go` -> `comparePrefix` (declared at cert/sign.go:151)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the Groups field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Provide divergent copies of the certificate data and trace which one `comparePrefix` reads.
- Invariant to test: Every authorization check reads the single verified certificate object bound to the session.
- Expected Immunefi impact: Authorization bypass using an unverified copy of the identity data.
- Fast validation: Unit test with divergent verified/unverified copies asserting `comparePrefix` reads the verified one.
