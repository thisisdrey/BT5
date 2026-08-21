# Q1252: Non-constant-time comparison in nistCurve.DH

## Question
Does `nistCurve.DH` (noiseutil/nist.go) compare authentication tags, keys, or the key rotation/rekey boundary in a data-dependent way an attacker can time remotely?

## Target
- File/function: `noiseutil/nist.go` -> `nistCurve.DH` (declared at noiseutil/nist.go:44)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: the key rotation/rekey boundary; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send many packets with tags differing at progressively later bytes and measure response timing.
- Invariant to test: All secret-dependent comparisons use constant-time primitives.
- Expected Immunefi impact: Tag or key recovery through a remote timing oracle, breaking tunnel integrity.
- Fast validation: Statistical timing test over `nistCurve.DH` across tag prefixes, asserting no measurable correlation.
