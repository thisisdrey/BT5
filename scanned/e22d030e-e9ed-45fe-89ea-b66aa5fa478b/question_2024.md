# Q2024: Concurrent access to counter/window state in nistCurve.DHName

## Question
Can an attacker use parallel packets touching a counter that wraps past the maximum to race `nistCurve.DHName` (noiseutil/nist.go) so two goroutines observe the same counter as unused?

## Target
- File/function: `noiseutil/nist.go` -> `nistCurve.DHName` (declared at noiseutil/nist.go:68)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: a counter that wraps past the maximum; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send duplicate counters simultaneously across multiple receive workers.
- Invariant to test: Counter/window updates are atomic against all concurrent receive workers.
- Expected Immunefi impact: Replay acceptance or nonce reuse under load, breaking tunnel integrity.
- Fast validation: `-race` concurrency test hammering `nistCurve.DHName` with duplicate counters and asserting exactly one acceptance.
