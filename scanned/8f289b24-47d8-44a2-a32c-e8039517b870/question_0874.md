# Q0874: Non-constant-time comparison in NewCipherStateChaChaPoly

## Question
Does `NewCipherStateChaChaPoly` (noiseutil/chachapoly.go) compare authentication tags, keys, or a counter far ahead of the window in a data-dependent way an attacker can time remotely?

## Target
- File/function: `noiseutil/chachapoly.go` -> `NewCipherStateChaChaPoly` (declared at noiseutil/chachapoly.go:19)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: a counter far ahead of the window; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send many packets with tags differing at progressively later bytes and measure response timing.
- Invariant to test: All secret-dependent comparisons use constant-time primitives.
- Expected Immunefi impact: Tag or key recovery through a remote timing oracle, breaking tunnel integrity.
- Fast validation: Statistical timing test over `NewCipherStateChaChaPoly` across tag prefixes, asserting no measurable correlation.
