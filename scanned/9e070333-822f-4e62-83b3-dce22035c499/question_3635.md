# Q3635: Non-constant-time comparison in aeadCipher.Encrypt

## Question
Does `aeadCipher.Encrypt` (noiseutil/boring.go) compare authentication tags, keys, or the message counter in a data-dependent way an attacker can time remotely?

## Target
- File/function: `noiseutil/boring.go` -> `aeadCipher.Encrypt` (declared at noiseutil/boring.go:74)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: the message counter; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send many packets with tags differing at progressively later bytes and measure response timing.
- Invariant to test: All secret-dependent comparisons use constant-time primitives.
- Expected Immunefi impact: Tag or key recovery through a remote timing oracle, breaking tunnel integrity.
- Fast validation: Statistical timing test over `aeadCipher.Encrypt` across tag prefixes, asserting no measurable correlation.
