# Q3136: CA constraint not enforced by IsNormalized

## Question
Does `IsNormalized` (cert/p256/p256.go) enforce the issuing CA's constraints on the IsCA flag, or can a subordinate certificate claim more than its issuer permits?

## Target
- File/function: `cert/p256/p256.go` -> `IsNormalized` (declared at cert/p256/p256.go:25)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the IsCA flag; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Sign a leaf claiming networks/groups broader than the CA's own constraints and verify it.
- Invariant to test: A leaf certificate's authority is always the intersection of its own claims and every issuer's constraints.
- Expected Immunefi impact: Privilege escalation across CA boundaries, granting overlay access beyond the delegated scope.
- Fast validation: Unit test verifying an over-claiming leaf through `IsNormalized` and asserting rejection or clamping.
