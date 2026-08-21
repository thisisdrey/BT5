# Q1004: Non-constant-time comparison in CipherStateChaChaPoly.DecryptDanger

## Question
Does `CipherStateChaChaPoly.DecryptDanger` (noiseutil/chachapoly.go) compare authentication tags, keys, or the key rotation/rekey boundary in a data-dependent way an attacker can time remotely?

## Target
- File/function: `noiseutil/chachapoly.go` -> `CipherStateChaChaPoly.DecryptDanger` (declared at noiseutil/chachapoly.go:38)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: the key rotation/rekey boundary; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send many packets with tags differing at progressively later bytes and measure response timing.
- Invariant to test: All secret-dependent comparisons use constant-time primitives.
- Expected Immunefi impact: Tag or key recovery through a remote timing oracle, breaking tunnel integrity.
- Fast validation: Statistical timing test over `CipherStateChaChaPoly.DecryptDanger` across tag prefixes, asserting no measurable correlation.
