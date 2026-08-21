# Q1001: Non-constant-time comparison in CipherStateAESGCM.DecryptDanger

## Question
Does `CipherStateAESGCM.DecryptDanger` (noiseutil/aesgcm.go) compare authentication tags, keys, or the key rotation/rekey boundary in a data-dependent way an attacker can time remotely?

## Target
- File/function: `noiseutil/aesgcm.go` -> `CipherStateAESGCM.DecryptDanger` (declared at noiseutil/aesgcm.go:39)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: the key rotation/rekey boundary; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send many packets with tags differing at progressively later bytes and measure response timing.
- Invariant to test: All secret-dependent comparisons use constant-time primitives.
- Expected Immunefi impact: Tag or key recovery through a remote timing oracle, breaking tunnel integrity.
- Fast validation: Statistical timing test over `CipherStateAESGCM.DecryptDanger` across tag prefixes, asserting no measurable correlation.
