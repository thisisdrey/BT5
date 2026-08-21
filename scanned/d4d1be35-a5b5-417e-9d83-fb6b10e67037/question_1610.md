# Q1610: Concurrent access to counter/window state in CipherStateChaChaPoly.EncryptDanger

## Question
Can an attacker use parallel packets touching the nonce construction to race `CipherStateChaChaPoly.EncryptDanger` (noiseutil/chachapoly.go) so two goroutines observe the same counter as unused?

## Target
- File/function: `noiseutil/chachapoly.go` -> `CipherStateChaChaPoly.EncryptDanger` (declared at noiseutil/chachapoly.go:23)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: the nonce construction; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send duplicate counters simultaneously across multiple receive workers.
- Invariant to test: Counter/window updates are atomic against all concurrent receive workers.
- Expected Immunefi impact: Replay acceptance or nonce reuse under load, breaking tunnel integrity.
- Fast validation: `-race` concurrency test hammering `CipherStateChaChaPoly.EncryptDanger` with duplicate counters and asserting exactly one acceptance.
