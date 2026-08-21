# Q1000: Non-constant-time comparison in CipherStateAESGCM.EncryptDanger

## Question
Does `CipherStateAESGCM.EncryptDanger` (noiseutil/aesgcm.go) compare authentication tags, keys, or a duplicated counter inside the window in a data-dependent way an attacker can time remotely?

## Target
- File/function: `noiseutil/aesgcm.go` -> `CipherStateAESGCM.EncryptDanger` (declared at noiseutil/aesgcm.go:24)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: a duplicated counter inside the window; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send many packets with tags differing at progressively later bytes and measure response timing.
- Invariant to test: All secret-dependent comparisons use constant-time primitives.
- Expected Immunefi impact: Tag or key recovery through a remote timing oracle, breaking tunnel integrity.
- Fast validation: Statistical timing test over `CipherStateAESGCM.EncryptDanger` across tag prefixes, asserting no measurable correlation.
