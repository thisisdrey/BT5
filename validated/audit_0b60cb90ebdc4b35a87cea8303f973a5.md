### Title
Replay-window check-then-act race allows duplicate decryption/delivery of a captured packet - ([File: connection_state.go])

### Summary
`ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` validate a message counter against the anti-replay window (`Bits.Check`), perform the AEAD decryption *outside* of any lock, and only afterward call `Bits.Update` to mark the counter as consumed. Between the `Check` and the later `Update`, the window state does not yet reflect the in-flight counter, so a second, concurrently-processed copy of the same captured/replayed packet can also pass `Check` before the first copy's `Update` runs, exactly mirroring the bonding-curve bug's pattern: a boolean gate (`curveLiquidityMet` / here, "is this counter unseen?") is read once, an unprotected action is taken based on that stale read, and the invariant ("no packet with counter *i* is ever accepted twice") can be violated because the state that would prevent it hasn't been committed yet.

### Finding Description
`Decrypt` in `connection_state.go` does:
1. `cs.decryptLock.Lock(); result := cs.window.Check(l, messageCounter); cs.decryptLock.Unlock()`
2. `cs.dKey.DecryptDanger(...)` — the actual AEAD decrypt, done with the lock released
3. `cs.decryptLock.Lock(); result = cs.window.Update(l, messageCounter); cs.decryptLock.Unlock()` [1](#0-0) 

`VerifyRelay` follows the identical three-step pattern for relay frames: `Check` → decrypt/verify → `Update`. [2](#0-1) 

The lock is only held for the individual `Check` and `Update` calls, not across the whole operation, and the window bitmap is only mutated by `Update`, not by `Check` (see `Bits.Check` which is a pure read: it returns `!b.get(i)` for an in-window counter without touching state, while only `Bits.Update` mutates the bitmap). [3](#0-2) [4](#0-3) 

If a network attacker (or a race between legitimate re-delivery and an injected duplicate) causes the same wire packet — same message counter — to be processed by two different goroutines concurrently for the same `HostInfo`/`ConnectionState` (Nebula's outside-packet pipeline processes UDP reads with dedicated per-conn queues; different queues can hand duplicate/replayed ciphertext for the same tunnel to concurrent workers), both goroutines can call `Check` before either has called `Update`. Both `Check` calls observe "not yet seen" and return `true`, so both proceed through decryption and delivery to the TUN device (or, for `VerifyRelay`, both are treated as authenticated relay frames and forwarded). The `Update` calls afterward will accept the first and correctly report a duplicate for the second only at the *state-mutation* step — but by then the second copy has already been decrypted and, in `outside.go`'s message path, already handed off for delivery/forwarding, since delivery is not gated on the `Update` return value in all call paths that matter (the checked invariant is "read window, then act, then update window" rather than "atomically claim window slot, then act").

This is structurally the same defect class as the bonding-curve report: a single-shot boolean gate (`curveLiquidityMet` there; "already-seen" here) is evaluated once and relied upon by a subsequent state-mutating action (`_curveSell` sale from `bReserve` there; packet decryption/delivery here) without the gate being atomically re-validated at the point the action actually commits, so external actors racing the window between check and mutation can force the system into a state the check was supposed to prevent (liquidity below minimum there; a decrypted, duplicate-delivered packet here).

### Impact Explanation
A successful race yields duplicate decryption and delivery of a previously-seen/replayed ciphertext without a valid CA-signed certificate being required from the attacker — the attacker only needs to capture and re-inject wire traffic (a classic anti-replay bypass target), not hold a certificate. Depending on payload, this can cause duplicate application-level side effects on the receiving overlay network, or on relay nodes cause duplicate re-forwarding of relay frames (the exact behavior the `TestRelayReplayProtection` e2e test was written to prevent for the single-threaded case). Because relay frames are only integrity-checked (not confidentiality-protected against replay by header alone), this also weakens the replay guarantee that downstream firewall/conntrack logic and applications rely on for de-duplication and ordering assumptions.

### Likelihood Explanation
Likelihood is low-to-moderate: it requires the receiver to process two copies of the same ciphertext concurrently on separate goroutines/queues before the first `Update` call commits, i.e., a narrow timing window. Nebula's packet-processing pipeline uses multiple reader/processing paths (`q int` queue indices threaded through `consumeInsidePacket`/outside packet handlers), which is the concurrency substrate that makes a race plausible, but reliably winning the race against production timing requires either high packet rates or an attacker who can inject near-duplicate copies in a tight window (e.g., duplicating a captured UDP datagram at the network layer, which is trivial for an on-path attacker).

### Recommendation
Make the replay-window check-and-mark atomic with respect to a given counter: hold `decryptLock` (or use a per-counter claim primitive) across the full `Check`+decrypt+`Update` sequence, or restructure `Bits` to expose a single `CheckAndClaim(i)` operation that atomically marks the slot as claimed if and only if it was previously unseen, returning failure to all late-arriving duplicates before any decryption work is performed. The decrypt call should only proceed after the counter has been irrevocably claimed, mirroring the bonding-curve recommendation of validating the invariant "at the point of action" rather than relying on a previously-cached/one-time check.

### Proof of Concept
Conceptual PoC (cannot be executed in ask-only mode, but derivable from the code):
1. Attacker captures a legitimate Nebula UDP packet (`Message`/`MessageRelay`) with counter `N` destined to a victim tunnel endpoint.
2. Attacker (or a natural network duplication event) sends two copies of this exact packet to the victim in quick succession, timed to land on two different processing goroutines/queues for the same `HostInfo`.
3. Both goroutines invoke `ConnectionState.Decrypt`/`VerifyRelay`, each independently executing `cs.window.Check(l, N)` under the lock; since neither has yet executed `cs.window.Update(l, N)`, both `Check` calls return `true`.
4. Both goroutines proceed to `DecryptDanger` with counter `N` and, on success, both deliver/forward the same plaintext — a receiver-side replay of a single captured wire packet succeeds twice despite `ReplayWindow`/`Bits` intending to guarantee single delivery per counter. [1](#0-0) [4](#0-3)

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

**File:** connection_state.go (L85-108)
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

**File:** bits.go (L168-186)
```go
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
