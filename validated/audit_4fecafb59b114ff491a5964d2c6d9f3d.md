### Title
Check-then-Update race in ConnectionState.Decrypt allows duplicate decryption of a replayed packet with the same MessageCounter - (File: connection_state.go)

### Summary
`ConnectionState.Decrypt` calls `cs.window.Check` and `cs.window.Update` under separate, non-overlapping critical sections (`decryptLock.Lock()/Unlock()` around each call individually), with the AEAD decrypt (`DecryptDanger`) happening outside the lock in between. If two goroutines invoke `Decrypt` concurrently with the same `messageCounter` (e.g., a replayed UDP datagram delivered to two listener queues), both can pass `Check` before either calls `Update`, letting both successfully decrypt the same ciphertext.

### Finding Description
In `connection_state.go`, `Decrypt` is implemented as:
```go
cs.decryptLock.Lock()
result := cs.window.Check(l, messageCounter)
cs.decryptLock.Unlock()
...
out, err = cs.dKey.DecryptDanger(...)
...
cs.decryptLock.Lock()
result = cs.window.Update(l, messageCounter)
cs.decryptLock.Unlock()
``` [1](#0-0) 

`window.Check` is a read-only, non-mutating query against the replay bitmap [2](#0-1) , and the bitmap is only marked as "seen" inside `Update` [3](#0-2) . Because `Check` and `Update` are each individually locked but not locked together across the whole `Decrypt` call, two goroutines racing on the same `messageCounter` can both observe `Check == true` (neither has called `Update` yet), and both proceed to call `DecryptDanger` with the same key/nonce/counter. Nebula's UDP listener architecture supports multiple listener routines processing packets concurrently for the same `ConnectionState` (multi-queue UDP), so this race is reachable from ordinary duplicate/replayed attacker-injected UDP traffic without needing a malicious peer, certificate, or leaked keys — just the ability to send (or resend) a previously observed ciphertext to the victim.

The AEAD decrypt operation itself is not a nonce-reuse-for-encryption issue (the attacker isn't creating new ciphertext under a reused nonce; it's decrypting the same known ciphertext twice), but the anti-replay invariant "each message counter should be delivered to the application/tunnel at most once" is violated: the same packet can be decrypted and handed to the tun device twice, and depending on caller ordering, `Update` calls afterward may also return inconsistent/false results, but the duplicate delivery into the tunnel already occurred before that check runs.

### Impact Explanation
This breaks the anti-replay invariant that guarantees each message counter is processed exactly once. The scoped, concrete impact is duplicate processing of an attacker-replayed packet — i.e., double delivery of the same plaintext to the tun device/firewall pipeline. This maps to Nebula's "traffic decryption/forgery/replay" bounty category: a genuine replay bypass of the anti-replay window under concurrent processing, even though authentication of the individual packet (AEAD tag) still holds.

### Likelihood Explanation
Requires: (1) an attacker able to capture and resend (replay) a single previously observed ciphertext datagram, and (2) the two copies being processed by different goroutines/listener queues concurrently, racing between the `Check` and `Update` calls before the first `Update` commits. This is feasible under Nebula's multi-queue UDP listener model when packets are dispatched to multiple worker goroutines, and is reliably reproducible with a targeted concurrency test that pins timing (e.g., delaying `DecryptDanger`) rather than relying on real network timing.

### Recommendation
Hold `cs.decryptLock` for the entire duration of the check-decrypt-update sequence (or use a single atomic "check-and-provisionally-mark" step) so that `Check` and `Update` for a given `messageCounter` are atomic with respect to concurrent `Decrypt` calls. Concretely, acquire `decryptLock` once, call `Check`, and only release it after `Update` has run (including around `DecryptDanger`), or introduce a single `CheckAndReserve`-style method on `Bits` that marks the counter as pending at check-time under one lock and finalizes/rolls back after decrypt completes. Apply the same fix to `VerifyRelay`, which has the identical pattern.

### Proof of Concept
Add a concurrency test in `connection_state_test.go`:
1. Construct a `ConnectionState` with a real `dKey`/`eKey` pair and a fresh `Bits` window.
2. Encrypt one payload with `eKey` to get `packet` and note its `messageCounter`.
3. Spawn `N` (e.g., 8) goroutines that all call `cs.Decrypt(l, messageCounter, out, packet, nb)` concurrently with the identical `messageCounter` and `packet` bytes, optionally inserting a small artificial delay inside a wrapped `DecryptDanger`-equivalent to widen the race window.
4. Assert that only exactly one goroutine's call returns `(plaintext, nil)` and all others return `ErrAlreadySeen`.
5. Run with `go test -race -run TestConcurrentDecryptReplay` and repeat with `-count=100` to catch the race reliably; a failing build (more than one success) demonstrates the TOCTOU bypass of `cs.window.Check`/`cs.window.Update` serialization.

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
