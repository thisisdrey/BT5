# Q1158: Replay window bypass in cipherFn.CipherName

## Question
Can an attacker replay a captured tunnel packet so `cipherFn.CipherName` (noiseutil/boring.go) accepts it, using the cipher selection (AES-GCM vs ChaChaPoly) to slide or reset the acceptance window?

## Target
- File/function: `noiseutil/boring.go` -> `cipherFn.CipherName` (declared at noiseutil/boring.go:45)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: the cipher selection (AES-GCM vs ChaChaPoly); the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Capture an authenticated packet and resend it after driving the window via other traffic.
- Invariant to test: A message counter is accepted at most once per key, and the window never resets while the key lives.
- Expected Immunefi impact: Traffic replay into an established tunnel: duplicated inside-network actions from an attacker with no keys.
- Fast validation: Unit test feeding a duplicate counter into `cipherFn.CipherName` and asserting rejection, including at window edges.
