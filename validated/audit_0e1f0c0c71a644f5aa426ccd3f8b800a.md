### Title
Non-atomic Check-then-Update on the replay window allows concurrent duplicate packets to bypass replay protection - (File: `connection_state.go`)

### Summary
`ConnectionState.Decrypt` (and `VerifyRelay`) split the anti-replay check into two separately-locked critical sections around the decryption step, mirroring the CashManager root cause: a piece of state (`epochDuration` / here, the replay window `Bits`) is read in one step and mutated in a second, later step, without the two being treated as a single atomic transition. Just as `setEpochDuration` could be front-run or back-run by `transitionEpoch`, two concurrent packet-processing paths handling the exact same `messageCounter` can both pass the `Check` step before either has performed the `Update` step, because the lock is released between them.

### Finding Description
`ConnectionState.Decrypt` does:
```go
cs.decryptLock.Lock()
result := cs.window.Check(l, messageCounter)
cs.decryptLock.Unlock()
if !result { return nil, ErrAlreadySeen }

out, err = cs.dKey.DecryptDanger(...)
...

cs.decryptLock.Lock()
result = cs.window.Update(l, messageCounter)
cs.decryptLock.Unlock()
``` [1](#0-0) 

`Check` only inspects whether the counter has already been marked; `Update` is the step that actually marks it seen. [2](#0-1)  The two locked sections are independent (lock is dropped in between to perform the AEAD decrypt), so if two goroutines call `Decrypt` concurrently with the same `messageCounter` (e.g. an attacker replays a captured ciphertext packet to the same tunnel while the original is still in flight, or the packet arrives twice on the wire and is processed by two different reader routines), both can pass `Check` before either calls `Update`. This is the same class of bug as `CashManager.setEpochDuration`: a value (window state) is read, an external event is allowed to interleave, and the value is written later based on stale information — the order of interleaving determines whether the second duplicate is accepted or rejected. `VerifyRelay` has the identical pattern for relay frames. [3](#0-2) 

Nebula's own changelog acknowledges this general bug class exists in the replay-window code path ("Advance the replay window on relayed packets...", "Lock replay window updates so concurrent readers can't corrupt it"), showing this exact area has previously needed hardening. [4](#0-3) 

### Impact Explanation
If exploitable, this allows a remote attacker who has captured one valid ciphertext packet (no valid certificate required — pure duplication/replay of previously-observed traffic) to have it processed twice by decrypting/forwarding it again, defeating the AEAD replay window's core guarantee. For relay nodes this can cause duplicate forwarding of a relayed frame (this is the exact issue the changelog fix for "advance the replay window on relayed packets" targeted), and for terminal nodes it can result in duplicate application-level packet delivery to the tun device.

### Likelihood Explanation
Requires the packet-processing pipeline to actually run `Decrypt`/`VerifyRelay` concurrently for the same `ConnectionState` from more than one goroutine at the same instant (e.g., multiple reader routines under `listen.routines`, or a duplicate frame arriving via two paths such as direct + relay). I was not able to fully confirm within this investigation whether the current `readOutsidePackets`/reader-routine architecture ever dispatches two packets belonging to the same `hostinfo.ConnectionState` to separate goroutines simultaneously in the shipped code, so the concurrency precondition needs explicit verification (e.g., checking `f.readers`/listener fan-out and whether packets for a single index are always serialized onto one routine before this can be treated as fully exploitable).

### Recommendation
Hold `decryptLock` for the entire Check → Decrypt → Update sequence (or perform the window `Update` immediately/atomically upon a successful `Check`, before releasing the lock to do decryption, with a rollback path on decrypt failure) so the "check, then act" is a single atomic transition, analogous to invoking `transitionEpoch` before `setEpochDuration` in the referenced report.

### Proof of Concept
Not independently reproduced against the live binary in this session; the analog is based on static analysis of `connection_state.go`'s two separately-locked critical sections and the historical changelog entries referencing races in this exact subsystem. A concrete PoC would need to (1) confirm two goroutines can concurrently call `Decrypt` with an identical `messageCounter` for the same `ConnectionState`, and (2) demonstrate both return `nil, err == nil` (i.e., both accept) rather than the second returning `ErrAlreadySeen`.

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

**File:** connection_state.go (L84-108)
```go
// VerifyRelay verifies AEAD protected (but not encrypted) relay frames. packet must be length-checked by the caller.
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
}
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
