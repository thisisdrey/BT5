# Q2086: Constraint check applied to wrong field in findDuplicatePrefix

## Question
Does `findDuplicatePrefix` (cert/sign.go) validate the Networks field against the certificate that actually signed the session, or against a differently-sourced copy an attacker can influence?

## Target
- File/function: `cert/sign.go` -> `findDuplicatePrefix` (declared at cert/sign.go:160)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the Networks field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Provide divergent copies of the certificate data and trace which one `findDuplicatePrefix` reads.
- Invariant to test: Every authorization check reads the single verified certificate object bound to the session.
- Expected Immunefi impact: Authorization bypass using an unverified copy of the identity data.
- Fast validation: Unit test with divergent verified/unverified copies asserting `findDuplicatePrefix` reads the verified one.
