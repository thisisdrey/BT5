# Q1925: Concurrent access to counter/window state in nistCurve.DH

## Question
Can an attacker use parallel packets touching the replay bit window to race `nistCurve.DH` (noiseutil/nist.go) so two goroutines observe the same counter as unused?

## Target
- File/function: `noiseutil/nist.go` -> `nistCurve.DH` (declared at noiseutil/nist.go:44)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: the replay bit window; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send duplicate counters simultaneously across multiple receive workers.
- Invariant to test: Counter/window updates are atomic against all concurrent receive workers.
- Expected Immunefi impact: Replay acceptance or nonce reuse under load, breaking tunnel integrity.
- Fast validation: `-race` concurrency test hammering `nistCurve.DH` with duplicate counters and asserting exactly one acceptance.
