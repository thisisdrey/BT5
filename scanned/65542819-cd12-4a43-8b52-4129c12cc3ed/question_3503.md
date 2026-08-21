# Q3503: CA constraint not enforced by certificateV2.NotBefore

## Question
Does `certificateV2.NotBefore` (cert/cert_v2.go) enforce the issuing CA's constraints on a duplicated or out-of-order ASN.1 field, or can a subordinate certificate claim more than its issuer permits?

## Target
- File/function: `cert/cert_v2.go` -> `certificateV2.NotBefore` (declared at cert/cert_v2.go:109)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a duplicated or out-of-order ASN.1 field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Sign a leaf claiming networks/groups broader than the CA's own constraints and verify it.
- Invariant to test: A leaf certificate's authority is always the intersection of its own claims and every issuer's constraints.
- Expected Immunefi impact: Privilege escalation across CA boundaries, granting overlay access beyond the delegated scope.
- Fast validation: Unit test verifying an over-claiming leaf through `certificateV2.NotBefore` and asserting rejection or clamping.
