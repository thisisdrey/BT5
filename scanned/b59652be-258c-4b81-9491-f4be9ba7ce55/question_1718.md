# Q1718: Concurrent access to counter/window state in CipherStateAESGCM.Overhead

## Question
Can an attacker use parallel packets touching the cipher selection (AES-GCM vs ChaChaPoly) to race `CipherStateAESGCM.Overhead` (noiseutil/aesgcm.go) so two goroutines observe the same counter as unused?

## Target
- File/function: `noiseutil/aesgcm.go` -> `CipherStateAESGCM.Overhead` (declared at noiseutil/aesgcm.go:51)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: the cipher selection (AES-GCM vs ChaChaPoly); the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send duplicate counters simultaneously across multiple receive workers.
- Invariant to test: Counter/window updates are atomic against all concurrent receive workers.
- Expected Immunefi impact: Replay acceptance or nonce reuse under load, breaking tunnel integrity.
- Fast validation: `-race` concurrency test hammering `CipherStateAESGCM.Overhead` with duplicate counters and asserting exactly one acceptance.
