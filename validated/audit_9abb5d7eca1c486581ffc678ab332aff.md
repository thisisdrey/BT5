### Title
Non-atomic replay-window check-then-update in `ConnectionState.Decrypt`/`VerifyRelay` allows concurrent replay bypass - ([File: connection_state.go])

### Summary
`ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` validate an incoming message counter against the anti-replay window with `window.Check`, release the lock, perform the (unlocked) AEAD decryption, and only then re-acquire the lock to commit the counter with `window.Update`. Because the "check" and the "commit" are two separate critical sections rather than one atomic operation, concurrent packets carrying the same message counter can all pass the `Check` before any of them reaches `Update`. This mirrors the Carapace Sybil-withdrawal bug class: a resource-limiting check (STokens balance / replay-window bit) is validated but not locked/committed atomically, so multiple concurrent "requests" (duplicated withdrawal requests / duplicated ciphertext packets) can all pass the gate simultaneously before the shared state is updated.

### Finding Description
`Decrypt` and `VerifyRelay` in [1](#0-0)  both follow the pattern:

1. `decryptLock.Lock(); window.Check(...); decryptLock.Unlock()`
2. Perform AEAD decrypt/verify **without holding the lock**
3. `decryptLock.Lock(); window.Update(...); decryptLock.Unlock()`

`Bits.Check` at [2](#0-1)  only reads whether bit `i` is already set — it does not mark it. `Bits.Update` at [3](#0-2)  is the operation that actually sets the bit / advances the window and is the sole place duplicate detection is permanently recorded.

Because `Check` and `Update` are split across two independently-locked sections with unlocked, non-trivial work (AEAD decrypt) sandwiched between them, two or more goroutines processing packets carrying the identical `messageCounter` for the same tunnel can each:
- acquire the lock, call `Check`, see the bit unset, release the lock (all before any of them calls `Update`),
- proceed to decrypt (which will succeed, since AEAD decryption with a valid, previously-seen nonce/counter is deterministic and does not itself enforce uniqueness),
- and only afterward serialize on `Update`, at which point at most one will "win" the bit-set, but the others have already produced a valid decrypted plaintext.

This is directly analogous to the reported issue: the balance/replay check is performed without locking/committing the resource being checked, so multiple concurrent requests referencing the same underlying token (STokens balance / message counter) can all pass validation before the commit step executes, defeating the intended one-shot guarantee.

Nebula explicitly supports multiple concurrent UDP reader/processing routines (`listen.routines`), so packets — including a replayed copy of a previously captured legitimate ciphertext — can be dispatched to different goroutines and processed concurrently against the same `ConnectionState`, giving an attacker a realistic window in which to race the check-then-update gap. No CA-signed certificate is needed to mount this: a network-position attacker only needs to capture and duplicate an already-encrypted wire packet between two legitimate peers and re-inject copies (e.g., to multiple UDP listener queues) so they are picked up by different worker goroutines simultaneously.

### Impact Explanation
A successful race lets an attacker get a captured/replayed ciphertext frame decrypted and delivered to the tun device (or, via `VerifyRelay`, re-forwarded through a relay) more than once, defeating the anti-replay window that is supposed to guarantee each message counter is accepted exactly once. This directly undermines the "traffic decryption/forgery/replay" security guarantee of the transport: replayed application data can be re-injected into the tunnel/TUN device or re-forwarded through relays, which the changelog itself flags as a class of bug the project has previously fixed for the relay path (`Advance the replay window on relayed packets…`, `Lock replay window updates so concurrent readers can't corrupt it.`) — but the check/update split here is a residual, narrower TOCTOU gap that those fixes do not close, since the lock is still dropped between `Check` and `Update`.

### Likelihood Explanation
Requires: (1) the ability to capture a legitimate ciphertext (any network eavesdropper/MITM can do this, no cert required), and (2) the ability to deliver duplicate copies of that packet so they land on distinct goroutines processing the same `ConnectionState` concurrently, which is plausible given nebula's multi-routine UDP listener design (`listen.routines`) and the fact that `Decrypt`/`VerifyRelay` release the lock across the (relatively expensive) AEAD operation. This is a genuine, narrow race condition (not a design-level open door), so likelihood is moderate — it requires precise timing to win the race, but the window (an AEAD decrypt operation) is non-trivial and reproducible under load or with intentionally staggered duplicate injection.

### Recommendation
Make the replay-window check-and-commit atomic with respect to a given message counter: either hold `decryptLock` across the entire `Check` → `Decrypt` → `Update` sequence for a given counter, or reserve the counter (mark it provisionally consumed) inside the same locked critical section as `Check` before releasing the lock to perform decryption, rolling back the reservation only on decrypt failure. This closes the gap that currently allows concurrent duplicate packets to all pass `Check` before any of them commits via `Update`.

### Proof of Concept
1. Establish a tunnel between two nebula instances using `listen.routines` > 1 so packet processing is distributed across multiple goroutines.
2. Capture a single legitimate data-plane ciphertext packet with message counter `N` sent by peer A to peer B (e.g., using a passive network tap; no certificate needed).
3. Simultaneously inject two (or more) copies of this exact packet into peer B's UDP socket, timed so they are picked up by different reader routines.
4. Instrument/observe `ConnectionState.Decrypt` ( [4](#0-3) ): both goroutines call `window.Check(l, N)` before either calls `window.Update(l, N)`, since the lock is released between the two calls (`decryptLock.Lock(); ... Check ...; decryptLock.Unlock()` then decrypt then `decryptLock.Lock(); ... Update ...`). Both `Check` calls observe the bit for `N` unset and return `true`, allowing both goroutines to proceed to `DecryptDanger`, producing two independently decrypted (and, in the plain `Decrypt` path, forwarded-to-tun) copies of the same replayed message despite the anti-replay window's intended one-shot semantics.

### Citations

**File:** connection_state.go (L61-108)
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

**File:** bits.go (L168-263)
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

// updateSlow handles jumps, in-window backfill, dupes, and out-of-window.
func (b *Bits) updateSlow(l *slog.Logger, i uint64) bool {
	// If i is a jump, adjust the window, record lost, update current, and return true
	if i > b.current {
		end := i
		if end > b.current+b.length {
			end = b.current + b.length
		}
		count := end - b.current
		startPos := (b.current + 1) & b.lengthMask

		var lost int64
		if b.current >= b.length {
			// Steady state: every cleared slot is past warmup, so any unset
			// bit we evict is a lost packet from the previous cycle.
			wasSet := b.clearRange(startPos, count)
			lost = int64(count) - int64(wasSet)
		} else {
			// Warmup (the very first window). Some cleared slots represent
			// packets <= length where eviction is not "lost" in the usual
			// sense. This branch is taken at most once per connection so we
			// don't bother optimizing it.
			for n := b.current + 1; n <= end; n++ {
				if !b.get(n) && n > b.length {
					lost++
				}
			}
			b.clearRange(startPos, count)
		}

		// Anything past the new window can never be backfilled, so it's lost.
		if i > b.current+b.length {
			lost += int64(i - b.current - b.length)
		}
		b.lostCounter.Inc(lost)

		b.set(i)
		b.current = i
		return true
	}

	// If i is within the current window but below the current counter, check to see if it's a duplicate
	if b.strictlyWithinWindow(i) {
		pos := i & b.lengthMask
		word := pos >> 6
		mask := uint64(1) << (pos & 63)
		w := b.bits[word]
		if b.current == i || w&mask != 0 {
			if l.Enabled(context.Background(), slog.LevelDebug) {
				l.Debug("Receive window",
					"accepted", false,
					"currentCounter", b.current,
					"incomingCounter", i,
					"reason", "duplicate",
				)
			}
			b.dupeCounter.Inc(1)
			return false
		}

		b.bits[word] = w | mask
		return true
	}

	// In all other cases, fail and don't change current.
	b.outOfWindowCounter.Inc(1)
	if l.Enabled(context.Background(), slog.LevelDebug) {
		l.Debug("Receive window",
			"accepted", false,
			"currentCounter", b.current,
			"incomingCounter", i,
			"reason", "nonsense",
		)
	}
	return false
}
```
