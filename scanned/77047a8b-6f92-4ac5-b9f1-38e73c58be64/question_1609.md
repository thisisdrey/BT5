# Q1609: Concurrent access to counter/window state in NewCipherStateChaChaPoly

## Question
Can an attacker use parallel packets touching the message counter to race `NewCipherStateChaChaPoly` (noiseutil/chachapoly.go) so two goroutines observe the same counter as unused?

## Target
- File/function: `noiseutil/chachapoly.go` -> `NewCipherStateChaChaPoly` (declared at noiseutil/chachapoly.go:19)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: the message counter; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send duplicate counters simultaneously across multiple receive workers.
- Invariant to test: Counter/window updates are atomic against all concurrent receive workers.
- Expected Immunefi impact: Replay acceptance or nonce reuse under load, breaking tunnel integrity.
- Fast validation: `-race` concurrency test hammering `NewCipherStateChaChaPoly` with duplicate counters and asserting exactly one acceptance.
