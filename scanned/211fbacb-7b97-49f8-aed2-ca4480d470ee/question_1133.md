# Q1133: Non-constant-time comparison in nistCurve.GenerateKeypair

## Question
Does `nistCurve.GenerateKeypair` (noiseutil/nist.go) compare authentication tags, keys, or a duplicated counter inside the window in a data-dependent way an attacker can time remotely?

## Target
- File/function: `noiseutil/nist.go` -> `nistCurve.GenerateKeypair` (declared at noiseutil/nist.go:32)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: a duplicated counter inside the window; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send many packets with tags differing at progressively later bytes and measure response timing.
- Invariant to test: All secret-dependent comparisons use constant-time primitives.
- Expected Immunefi impact: Tag or key recovery through a remote timing oracle, breaking tunnel integrity.
- Fast validation: Statistical timing test over `nistCurve.GenerateKeypair` across tag prefixes, asserting no measurable correlation.
