### Title
Replay-window check/decrypt/update TOCTOU allows a single replayed packet to be delivered twice - (File: connection_state.go)

### Summary
`ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` split anti-replay enforcement into three separate steps — `window.Check`, then AEAD decryption, then `window.Update` — with the mutex released between each step. This mirrors the reported bug class: a status check is performed, the "action" is taken, and only afterward is state updated, leaving a window where the check can be satisfied twice before the update closes it.

### Finding Description
`Decrypt` first takes `decryptLock`, calls `cs.window.Check(l, messageCounter)`, and releases the lock immediately: [1](#0-0) 

Only after the lock is released does it perform the actual AEAD decryption via `cs.dKey.DecryptDanger`, and only after decryption succeeds does it re-acquire the lock and call `cs.window.Update` to actually mark the counter as consumed: [2](#0-1) 

`VerifyRelay` has the identical structure for relay frames: [3](#0-2) 

`Check` only tests whether a counter has already been recorded; it does not itself mark the counter as seen — only `Update` does that: [4](#0-3) 

Because the packet-processing path on a `ConnectionState` can be invoked concurrently for the same hostinfo (multiple reader queues / goroutines feeding `handleOutsideMessagePacket` / relay packet handling for the same UDP socket), an attacker positioned to capture and replay a single legitimate ciphertext packet (e.g. a network-path attacker, no valid certificate needed since the packet is just bytes being retransmitted) can fire the same packet twice in rapid succession. Both copies can pass `Check` before either has called `Update`, because the lock guarding `Check` is dropped before decryption starts. Both then successfully decrypt (decryption itself does not depend on the anti-replay window state) and only the second `Update` call will detect and reject the duplicate — after the first copy has already been decrypted and handed to the tun device (or, for `VerifyRelay`, after the relay has already treated the frame as authentic and forwarded/accepted it once). This exactly parallels the reported flaw: the guard (`on_ride`/window bit) is checked before the state-changing action, but is not updated atomically with that check, so the same request can be "accepted" more than once.

Notably, the CHANGELOG documents a related but distinct fix ("Lock replay window updates so concurrent readers can't corrupt it. (#1802)") and ("Advance the replay window on relayed packets so a relay drops replayed frames instead of re-forwarding them. (#1751)"), which addressed corruption/no-update bugs but did not close the Check/Decrypt/Update race window itself — the check and the state mutation remain two separate critical sections with unprotected work (decryption) sandwiched in between. [5](#0-4) 

### Impact Explanation
This allows duplicate acceptance/processing of a single captured ciphertext frame in a narrow race window — a form of replay bypass. For the data-plane `Decrypt` path this means a single captured packet can be delivered twice to the tun device (duplicate injected traffic), and for `VerifyRelay` a relay can be tricked into (mis)processing/forwarding the same relay frame more than once during the race, which is precisely the kind of decrypt/relay "duplicate active session" behavior called out in the report (state check and update not atomic, enabling more than one "success" from a single request).

### Likelihood Explanation
Exploitation requires (a) the ability to capture/replay a valid ciphertext packet (no CA-signed certificate needed — this is pure wire-level replay, not requiring cert possession) and (b) winning a tight race between two decrypt calls landing on the same `ConnectionState.window` before either `Update` runs. This is a genuine but narrow timing race; it is more reliably triggerable under load or multiple parallel reader routines processing the outside queue, similar to how the existing e2e tests (`TestRelayReplayProtection`) specifically probe this "replay must be dropped" property for the relay path.

### Recommendation
Make the check-decrypt-update sequence atomic with respect to the replay window: hold `decryptLock` across `Check`, `DecryptDanger`, and `Update` for a given message counter (or use an atomic "reserve" primitive — e.g., a single call that checks-and-marks the counter before decryption starts, then rolls back only the mark if decryption fails) so no two concurrent decrypt/verify calls for the same counter can both pass the check before the window is updated.

### Proof of Concept
1. Establish a tunnel between two nebula nodes so a `ConnectionState` with a live `window` exists.
2. Capture a single legitimate outside UDP message-plane packet (or relay frame) in flight.
3. Re-inject the identical raw UDP packet to the receiving node's socket twice, back-to-back, from two goroutines/sockets simultaneously (simulating a race), before the first `Decrypt`/`VerifyRelay` call's `Update` step commits.
4. Observe that both invocations pass `window.Check` (since neither has yet executed `window.Update`), both successfully call `DecryptDanger` on the same ciphertext/counter, and only the second `Update` call rejects the duplicate as `ErrAlreadySeen` — after the first copy has already been decrypted and, in the `Decrypt` path, written to the tun device, or in the `VerifyRelay` path, already accepted as authentic by the relay logic. [6](#0-5) [4](#0-3)

### Citations

**File:** connection_state.go (L61-82)
```go
func (cs *ConnectionState) Decrypt(l *slog.Logger, messageCounter uint64, out []byte, packet []byte, nb []byte) ([]byte, error) {
	var err error
	cs.decryptLock.Lock()
	result := cs.window.Check(l, messageCounter)
	cs.decryptLock.Unlock()
	if !result {
		return nil, ErrAlreadySeen
	}

	out, err = cs.dKey.DecryptDanger(out, packet[:header.Len], packet[header.Len:], messageCounter, nb)
	if err != nil {
		return nil, err
	}

	cs.decryptLock.Lock()
	result = cs.window.Update(l, messageCounter)
	cs.decryptLock.Unlock()
	if !result {
		return nil, ErrAlreadySeen
	}
	return out, nil
}
```

**File:** connection_state.go (L85-107)
```go
func (cs *ConnectionState) VerifyRelay(l *slog.Logger, messageCounter uint64, packet []byte, nb []byte) error {
	cs.decryptLock.Lock()
	result := cs.window.Check(l, messageCounter)
	cs.decryptLock.Unlock()
	if !result {
		return ErrAlreadySeen
	}

	signedPayload := packet[:len(packet)-cs.dKey.Overhead()]
	signatureValue := packet[len(packet)-cs.dKey.Overhead():]
	_, err := cs.dKey.DecryptDanger(nil, signedPayload, signatureValue, messageCounter, nb)
	if err != nil {
		return err
	}

	cs.decryptLock.Lock()
	result = cs.window.Update(l, messageCounter)
	cs.decryptLock.Unlock()
	if !result {
		return ErrAlreadySeen
	}

	return nil
```

**File:** bits.go (L134-150)
```go
// Check returns true if i is within (or way out in front of) the window, and not a replay
func (b *Bits) Check(l *slog.Logger, i uint64) bool {
	// If i is the next number, return true.
	if i > b.current {
		return true
	}

	if b.strictlyWithinWindow(i) {
		return !b.get(i)
	}

	// Not within the window
	if l.Enabled(context.Background(), slog.LevelDebug) {
		l.Debug("rejected a packet (top)", "current", b.current, "incoming", i)
	}
	return false
}
```

**File:** CHANGELOG.md (L79-81)
```markdown
- Advance the replay window on relayed packets so a relay drops replayed frames instead of re-forwarding them. (#1751)
- Fix a race in relay state handling. (#1753)
- Lock replay window updates so concurrent readers can't corrupt it. (#1802)
```
