# Q1033: Replay window bypass in cipherFn.Cipher

## Question
Can an attacker replay a captured tunnel packet so `cipherFn.Cipher` (noiseutil/boring.go) accepts it, using the replay bit window to slide or reset the acceptance window?

## Target
- File/function: `noiseutil/boring.go` -> `cipherFn.Cipher` (declared at noiseutil/boring.go:44)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: the replay bit window; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Capture an authenticated packet and resend it after driving the window via other traffic.
- Invariant to test: A message counter is accepted at most once per key, and the window never resets while the key lives.
- Expected Immunefi impact: Traffic replay into an established tunnel: duplicated inside-network actions from an attacker with no keys.
- Fast validation: Unit test feeding a duplicate counter into `cipherFn.Cipher` and asserting rejection, including at window edges.
