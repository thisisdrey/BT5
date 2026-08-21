# Q1253: Non-constant-time comparison in nistCurve.DHLen

## Question
Does `nistCurve.DHLen` (noiseutil/nist.go) compare authentication tags, keys, or the boringcrypto vs stdlib path in a data-dependent way an attacker can time remotely?

## Target
- File/function: `noiseutil/nist.go` -> `nistCurve.DHLen` (declared at noiseutil/nist.go:57)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: the boringcrypto vs stdlib path; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send many packets with tags differing at progressively later bytes and measure response timing.
- Invariant to test: All secret-dependent comparisons use constant-time primitives.
- Expected Immunefi impact: Tag or key recovery through a remote timing oracle, breaking tunnel integrity.
- Fast validation: Statistical timing test over `nistCurve.DHLen` across tag prefixes, asserting no measurable correlation.
