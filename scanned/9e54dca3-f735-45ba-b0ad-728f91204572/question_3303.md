# Q3303: Window edge off-by-one in Bits.Update

## Question
Is there an off-by-one at the boundary of the message counter in `Bits.Update` (bits.go) so the oldest or newest acceptable counter is mishandled?

## Target
- File/function: `bits.go` -> `Bits.Update` (declared at bits.go:168)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: the message counter; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Exercise counters exactly at window_low, window_low-1, window_high and window_high+1.
- Invariant to test: Window membership is exact at both edges, with duplicates rejected and legitimate in-order packets accepted.
- Expected Immunefi impact: Either replay acceptance (security) or dropping of legitimate traffic (availability) on every tunnel.
- Fast validation: Exhaustive unit test over the window boundary values against `Bits.Update`.
