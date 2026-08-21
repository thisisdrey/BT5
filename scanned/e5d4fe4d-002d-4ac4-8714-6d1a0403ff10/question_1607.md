# Q1607: Concurrent access to counter/window state in CipherStateAESGCM.EncryptDanger

## Question
Can an attacker use parallel packets touching the nonce construction to race `CipherStateAESGCM.EncryptDanger` (noiseutil/aesgcm.go) so two goroutines observe the same counter as unused?

## Target
- File/function: `noiseutil/aesgcm.go` -> `CipherStateAESGCM.EncryptDanger` (declared at noiseutil/aesgcm.go:24)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: the nonce construction; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send duplicate counters simultaneously across multiple receive workers.
- Invariant to test: Counter/window updates are atomic against all concurrent receive workers.
- Expected Immunefi impact: Replay acceptance or nonce reuse under load, breaking tunnel integrity.
- Fast validation: `-race` concurrency test hammering `CipherStateAESGCM.EncryptDanger` with duplicate counters and asserting exactly one acceptance.
