### Title
Anti-replay window check/update race allows a captured packet to be replayed once during the check-then-act gap - (File: `connection_state.go`)

### Summary
`ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` implement anti-replay protection with two separate calls into the `Bits` sliding window: `window.Check()` gates whether decryption should even be attempted, and `window.Update()` (called only after a successful AEAD decrypt) is what actually marks the counter as seen. Like the ERC4626 report where `previewDeposit`/`convertToShares` and `previewMint` were expected to agree but implemented divergent logic for the same state transition, here `Check` and `Update` are two logically-coupled operations on the same replay window that are expected to behave as a single atomic "test-and-set", but the code does not hold the lock across both calls, so they can observe/apply stale state relative to each other.

### Finding Description
In `connection_state.go`:

```go
cs.decryptLock.Lock()
result := cs.window.Check(l, messageCounter)
cs.decryptLock.Unlock()
if !result { return nil, ErrAlreadySeen }

out, err = cs.dKey.DecryptDanger(...)   // no lock held here
...

cs.decryptLock.Lock()
result = cs.window.Update(l, messageCounter)
cs.decryptLock.Unlock()
if !result { return nil, ErrAlreadySeen }
``` [1](#0-0) 

The same pattern is repeated for relay frames in `VerifyRelay`: [2](#0-1) 

`window.Check` only reads the window bitmap (via `strictlyWithinWindow`/`get`) and does not mark the counter as consumed; only `window.Update` mutates the bitmap for that counter: [3](#0-2) [4](#0-3) 

Because `decryptLock` is released between the `Check` call and the `Update` call (with the AEAD decrypt running unlocked in between), two goroutines processing the same message counter concurrently (e.g. the UDP listener workers, since Nebula processes inbound packets on multiple readers/routines) can both execute `Check` before either has executed `Update`. Both will see the counter as not-yet-seen, both will successfully decrypt (AEAD decryption of a duplicated ciphertext succeeds since AES-GCM/ChaCha20-Poly1305 verification is independent of the replay window), and only afterward does the loser of the `Update` race get rejected. The winner's already-decrypted plaintext has already been (or is about to be) delivered to the TUN device / relay path before the loser's rejection is even computed — so a duplicated on-the-wire packet can be accepted and processed twice in the race window, defeating the anti-replay guarantee for that one packet.

### Impact Explanation
This breaks the intended one-shot guarantee of the nonce/counter-based anti-replay window: an on-path attacker who can duplicate an already-observed, encrypted Nebula packet (a classic replay primitive, requiring no CA-signed certificate — only interception/duplication capability) can, within the narrow concurrent-processing window, cause the same application-layer datagram to be delivered twice to the destination tun interface or forwarded twice through a relay. This is a genuine, if narrow, replay-protection bypass rather than a full authentication bypass, matching the "traffic decryption/forgery/replay" impact category.

### Likelihood Explanation
The race requires the two duplicate packets to be processed by concurrent goroutines close enough in time that both complete `Check` before either completes `Update`; this is a timing-dependent condition, so successful replay of a given single packet is probabilistic rather than deterministic. Nonetheless, an attacker fully controls when they inject a duplicate onto the wire and can retry, making the race practically triggerable over time, unlike the report's original bug (which is deterministic 100% of the time). This is why the finding is presented as a race/TOCTOU issue rather than an unconditional bypass, and the likelihood should be judged accordingly lower than the reference finding's "High" severity.

### Recommendation
Hold `decryptLock` across the whole check-decrypt-mark sequence (or perform an atomic "reserve" of the counter in `Check` itself, rolling it back only if decryption subsequently fails) so that `Check` and `Update` for a given counter form a single atomic operation with no unlocked gap in which a duplicate can race through decryption.

### Proof of Concept
Conceptual PoC (timing-dependent, requires two goroutines racing the same `ConnectionState`):
```go
// attacker captures one legitimate encrypted packet with counter N and
// injects it twice, timed so both copies reach the UDP handler concurrently
go conn.handleInboundPacket(dupPacketCopy1) // goroutine A
go conn.handleInboundPacket(dupPacketCopy2) // goroutine B

// Both A and B call cs.Decrypt(l, N, ...):
//  - A: window.Check(N) -> true (lock released)
//  - B: window.Check(N) -> true (lock released, bit for N still unset)
//  - A: DecryptDanger succeeds, delivers plaintext to TUN
//  - B: DecryptDanger succeeds, delivers plaintext to TUN  <-- duplicate delivery
//  - A: window.Update(N) -> true, marks bit
//  - B: window.Update(N) -> false, ErrAlreadySeen (too late, already delivered)
```
Exact reproduction would require instrumenting `cs.decryptLock` with an artificial delay between `Check` and `DecryptDanger` to reliably win the race in a test harness, confirming the double-delivery described in `Decrypt`. [1](#0-0)

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
