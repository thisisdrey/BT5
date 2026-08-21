# Q3584: CA constraint not enforced by certificateV2.MarshalPublicKeyPEM

## Question
Does `certificateV2.MarshalPublicKeyPEM` (cert/cert_v2.go) enforce the issuing CA's constraints on a self-signed certificate, or can a subordinate certificate claim more than its issuer permits?

## Target
- File/function: `cert/cert_v2.go` -> `certificateV2.MarshalPublicKeyPEM` (declared at cert/cert_v2.go:117)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a self-signed certificate; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Sign a leaf claiming networks/groups broader than the CA's own constraints and verify it.
- Invariant to test: A leaf certificate's authority is always the intersection of its own claims and every issuer's constraints.
- Expected Immunefi impact: Privilege escalation across CA boundaries, granting overlay access beyond the delegated scope.
- Fast validation: Unit test verifying an over-claiming leaf through `certificateV2.MarshalPublicKeyPEM` and asserting rejection or clamping.
