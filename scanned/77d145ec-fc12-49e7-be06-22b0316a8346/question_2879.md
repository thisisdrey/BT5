# Q2879: Constraint check applied to wrong field in ErrInvalidCertificateProperties.Error

## Question
Does `ErrInvalidCertificateProperties.Error` (cert/errors.go) validate a self-signed certificate against the certificate that actually signed the session, or against a differently-sourced copy an attacker can influence?

## Target
- File/function: `cert/errors.go` -> `ErrInvalidCertificateProperties.Error` (declared at cert/errors.go:50)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a self-signed certificate; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Provide divergent copies of the certificate data and trace which one `ErrInvalidCertificateProperties.Error` reads.
- Invariant to test: Every authorization check reads the single verified certificate object bound to the session.
- Expected Immunefi impact: Authorization bypass using an unverified copy of the identity data.
- Fast validation: Unit test with divergent verified/unverified copies asserting `ErrInvalidCertificateProperties.Error` reads the verified one.
