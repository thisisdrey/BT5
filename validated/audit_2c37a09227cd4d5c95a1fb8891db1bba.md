### Title
Replay-window TOCTOU race in `ConnectionState.Decrypt`/`VerifyRelay` allows duplicate-counter packets to bypass replay protection - (File: connection_state.go)

### Summary
Nebula's per-tunnel replay protection (`Bits.Check`/`Bits.Update`) is checked and committed as two separate, separately-locked critical sections inside `ConnectionState.Decrypt` and `ConnectionState.VerifyRelay`. Between the `Check` call and the `Update` call, the lock is released and the (comparatively expensive) AEAD decrypt/verify runs unlocked. This mirrors the ERC777 "check happens against stale state before the state-changing operation completes" pattern from the external report: a state read is used to gate an action, but the actual state commit is deferred past a window in which the same check can be satisfied again for the same input.

### Finding Description
`Decrypt` locks `decryptLock`, calls `cs.window.Check(l, messageCounter)`, unlocks, performs `cs.dKey.DecryptDanger(...)` unlocked, then re-locks to call `cs.window.Update(l, messageCounter)`: [1](#0-0) 

The same pattern exists in `VerifyRelay`: [2](#0-1) 

`Bits.Check` only tests whether the counter's bit is unset (i.e., "not yet recorded"); it does not mark the bit itself. `Bits.Update` is the only call that actually sets the bit / advances the window: [3](#0-2) 

If two packets carrying the identical `messageCounter` (a captured/replayed ciphertext, or two copies delivered by the network/relay) are processed concurrently by two goroutines against the same `ConnectionState`, both goroutines can execute `Check` and both see the counter as "not yet seen," because neither has called `Update` yet. Both then proceed to decrypt (which will succeed identically for identical ciphertext) and both later call `Update`, with the second `Update` call recording the duplicate but the decrypted plaintext having already been accepted and handed off by the first caller — and depending on call ordering, the window state can also be corrupted since `Update`'s fast/slow paths are not idempotent guards against a duplicate `Update` from a stale `Check`.

Whether this is exploitable depends on whether decrypt/verify calls against a single `HostInfo`/`ConnectionState` can genuinely run concurrently (e.g., multiple listener routines, or a relay forwarding path racing with a direct decrypt path). The code comments elsewhere in the repo show the project is actively aware of and has previously fixed a closely related bug: "Lock replay window updates so concurrent readers can't corrupt it" (#1802) and "Advance the replay window on relayed packets so a relay drops replayed frames instead of re-forwarding them" (#1751), per the CHANGELOG: [4](#0-3) 

This shows the maintainers have already had to patch at least one replay-window concurrency/ordering defect, indicating the check-then-later-commit structure in `Decrypt`/`VerifyRelay` is a recognized weak point in this exact code path.

### Impact Explanation
A successful race allows a captured packet (or relayed frame) with a given counter to be accepted and decrypted more than once concurrently, defeating the anti-replay guarantee that Nebula's Noise-based transport is supposed to provide. This is a concrete "traffic decryption/forgery/replay" category impact: an attacker who can deliver two copies of the same ciphertext to a target (trivial on UDP, and especially easy through a relay forwarding path where multiple relay/forwarding goroutines may process frames for the same tunnel) could get duplicate acceptance of a replayed payload, which can be leveraged for traffic injection/duplication attacks against the overlay network, undermining the freshness guarantee AEAD nonces are supposed to enforce.

### Likelihood Explanation
Exploitability requires genuine concurrent invocation of `Decrypt`/`VerifyRelay` for the same `ConnectionState` — the report cannot fully confirm (given available context) whether Nebula's default single-goroutine-per-tunnel dispatch model, or its relay/listener architecture, permits two goroutines to race on the same `ConnectionState` simultaneously. Given prior fixes for replay-window races/relay-forwarding replay (#1751, #1802) already landed in this codebase, the underlying hazard class is plausible and has precedent, but confirming a currently-reachable race in the present code requires further investigation of the listener/relay dispatch model than could be completed in this pass.

### Recommendation
Hold `decryptLock` for the full duration of the check-decrypt-update sequence (or use a single atomic "reserve-then-confirm" primitive) so that `Check` and `Update` for a given counter are not separated by an unlocked window in which decryption of a duplicate can proceed. Alternatively, make `Check`+reservation atomic (mark the slot provisionally on `Check`, roll back on decrypt failure) so a second concurrent `Check` for the same counter cannot succeed until the first attempt's outcome is committed.

### Proof of Concept
Conceptual PoC (not runnable without the full harness):
1. Establish a tunnel and capture one valid ciphertext packet with counter `N` (as done in `TestRelayReplayProtection`, which already demonstrates constructing and replaying a captured frame): [5](#0-4) 
2. From two goroutines, simultaneously call `ConnectionState.Decrypt` (or deliver the packet twice near-simultaneously through the relay/listener dispatch paths) with the same captured packet/counter `N`.
3. If both goroutines' `window.Check` calls execute before either's `window.Update` call, both proceed to `DecryptDanger` and both report success, i.e., the duplicate is accepted twice instead of being rejected as `ErrAlreadySeen`, defeating replay protection: [1](#0-0)

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

**File:** bits.go (L134-186)
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

// Update has three branches:
//   - i == b.current+1: fast path; advance the cursor by one and lose-count
//     the slot we just stomped (only past warmup; see the i > b.length guard
//     below).
//   - i  >  b.current+1: jump path; clear all slots between current and i
//     (or up to a full window's worth, whichever is smaller) via clearRange,
//     then mark i. Two arms here: a warmup arm that handles the very first
//     window before the cursor has slid, and a steady-state arm that treats
//     every cleared empty slot as a lost packet.
//   - i  <= b.current: in-window check for duplicates; out-of-window otherwise.
//
// NewBits seeds bits[0]=1 so counter 0 looks "received" — Update never
// clears that marker during warmup (clearRange skips position 0 when
// startPos=1), and once b.current >= b.length the marker is no longer
// consulted. The marker prevents a fictitious "lost" hit on the first real
// counter.
func (b *Bits) Update(l *slog.Logger, i uint64) bool {
	// Fast path: i is the next expected counter. Split out so the function
	// stays small and avoids paying for the slow paths' slog argument-build
	// stack frame on every call. The bit read/test/write is inlined to
	// touch the backing word once.
	if i == b.current+1 {
		pos := i & b.lengthMask
		word := pos >> 6
		mask := uint64(1) << (pos & 63)
		w := b.bits[word]
		if i > b.length && w&mask == 0 {
			b.lostCounter.Inc(1)
		}
		b.bits[word] = w | mask
		b.current = i
		return true
	}
	return b.updateSlow(l, i)
}
```

**File:** CHANGELOG.md (L79-82)
```markdown
- Advance the replay window on relayed packets so a relay drops replayed frames instead of re-forwarding them. (#1751)
- Fix a race in relay state handling. (#1753)
- Lock replay window updates so concurrent readers can't corrupt it. (#1802)
- Reject malformed handshakes more reliably, including invalid ed25519 key lengths. (#1601, #1756)
```

**File:** e2e/tunnels_test.go (L377-382)
```go
// TestRelayReplayProtection asserts that a relay (forwarding-type) node rejects
// replayed relay frames. A captured relay frame, re-injected with the same
// message counter, must be dropped by the replay window rather than re-forwarded
// to the relay target. Before the fix, handleOutsideRelayPacket authenticated the
// frame but never advanced the replay window, so every replay was re-forwarded.
func TestRelayReplayProtection(t *testing.T) {
```
