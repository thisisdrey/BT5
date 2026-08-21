# Q1841: Replay window bypass in Bits.Update

## Question
Can an attacker replay a captured tunnel packet so `Bits.Update` (bits.go) accepts it, using the key rotation/rekey boundary to slide or reset the acceptance window?

## Target
- File/function: `bits.go` -> `Bits.Update` (declared at bits.go:168)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: the key rotation/rekey boundary; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Capture an authenticated packet and resend it after driving the window via other traffic.
- Invariant to test: A message counter is accepted at most once per key, and the window never resets while the key lives.
- Expected Immunefi impact: Traffic replay into an established tunnel: duplicated inside-network actions from an attacker with no keys.
- Fast validation: Unit test feeding a duplicate counter into `Bits.Update` and asserting rejection, including at window edges.
