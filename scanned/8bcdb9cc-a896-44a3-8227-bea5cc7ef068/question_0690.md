# Q0690: CA constraint not enforced by findDuplicatePrefix

## Question
Does `findDuplicatePrefix` (cert/sign.go) enforce the issuing CA's constraints on the issuer/CA fingerprint, or can a subordinate certificate claim more than its issuer permits?

## Target
- File/function: `cert/sign.go` -> `findDuplicatePrefix` (declared at cert/sign.go:160)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the issuer/CA fingerprint; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Sign a leaf claiming networks/groups broader than the CA's own constraints and verify it.
- Invariant to test: A leaf certificate's authority is always the intersection of its own claims and every issuer's constraints.
- Expected Immunefi impact: Privilege escalation across CA boundaries, granting overlay access beyond the delegated scope.
- Fast validation: Unit test verifying an over-claiming leaf through `findDuplicatePrefix` and asserting rejection or clamping.
