### Title
Replay-window TOCTOU in `ConnectionState.Decrypt` allows a duplicated UDP packet to be decrypted and delivered twice - ([File: connection_state.go])

### Summary
`ConnectionState.Decrypt` releases `decryptLock` between the anti-replay `window.Check` and the anti-replay `window.Update`, so two concurrent calls with an identical `messageCounter` can both pass the check, both invoke `DecryptDanger`, and only fail to commit on the later `Update`. An attacker who can get a single legitimate ciphertext datagram delivered twice to the victim (UDP duplication/replay onto separate batch-reader goroutines) can therefore make the receiver decrypt and forward the same encrypted packet twice, defeating the intended single-delivery guarantee of the replay window.

### Finding Description
`Decrypt` takes the lock only around the check and only around the update, decrypting in between while unlocked: [1](#0-0) 

`window.Check` (`Bits.Check`) only inspects state — it does not mark the counter as seen: [2](#0-1) 

Marking only happens later in `Update`: [3](#0-2) 

Because `decryptLock` is dropped between `Check` and `Update`, two goroutines racing on the same `ConnectionState` with the same `messageCounter` can both observe `Check == true` before either calls `Update`, so both proceed to call `cs.dKey.DecryptDanger(...)` with the identical counter/nonce. Only when both later call `Update` does the second one get rejected (`ErrAlreadySeen`) — but by then the decryption (and whatever the caller does with the returned plaintext, e.g. writing to the TUN device / passing to the firewall) has already happened twice.

This is reachable from attacker-controlled input because the trigger condition is simply "the same ciphertext datagram arrives on two different reader goroutines close together in time." With a multi-reader (`batch > 1`) UDP configuration, `ListenOut` runs multiple goroutines that each call `readOutsidePackets` → `ConnectionState.Decrypt` concurrently on the same `hostinfo`. An unprivileged network attacker (who can already observe/duplicate/replay UDP datagrams per the threat model — spoofed source, no keys needed) can send the same previously-captured legitimate ciphertext datagram twice in quick succession; if the two copies land on two different reader goroutines, the race window is hit.

Decrypting the same ciphertext with the same nonce twice is not, by itself, a classical AEAD nonce-reuse confidentiality break (nonce reuse is a hazard for *encryption*, not decryption, and the attacker doesn't gain visibility into the plaintext from this). The actual security consequence is that the replay-window's core invariant — "each counter value is delivered to the application/tunnel exactly once" — is violated: the same decrypted packet can be forwarded/processed twice.

### Impact Explanation
This breaks the anti-replay guarantee that Nebula's data-plane replay window is specifically designed to provide, enabling duplicate delivery of a captured/replayed packet into the tunnel (double TUN write, double firewall evaluation) under a race condition. Depending on what traffic is carried inside the tunnel, duplicate delivery of non-idempotent payloads can have real downstream effects. This maps to the "traffic replay" impact category — it does not amount to key/plaintext disclosure or forgery, since the attacker needs a valid ciphertext they already possess (captured legitimate traffic) rather than being able to forge new ones.

### Likelihood Explanation
Requires: (1) a multi-reader UDP configuration (`batch > 1`, i.e., `q>1` reader goroutines) so `ListenOut` dispatches packets to concurrent goroutines sharing the same `hostinfo`/`ConnectionState`; (2) the attacker being able to get two copies of the exact same ciphertext datagram to the victim's socket close enough in time to land on two different reader goroutines. Both conditions are plausible for an on-path/spoofing attacker under the stated threat model, but the race window between `Check` and `DecryptDanger`+`Update` is narrow (a handful of instructions plus one AEAD decrypt), making reliable exploitation timing-sensitive and not guaranteed on every attempt — it is a genuine but probabilistic race, not deterministic.

### Recommendation
Close the TOCTOU window by holding `decryptLock` across the entire check-decrypt-update sequence (or restructure `Bits` to provide an atomic "reserve" operation, e.g. `Bits.Reserve(i)` that checks and marks the counter as seen under a single critical section before decryption is attempted, with a corresponding rollback/no-op if decryption later fails). The lock should not be released between `window.Check` and `window.Update` when they both apply to the same `messageCounter`.

### Proof of Concept
Add a concurrency test in `connection_state_test.go`:
1. Build a `ConnectionState` with a valid `dKey`/`window`, and a single valid ciphertext packet/counter pair (produced by legitimately encrypting with the matching `eKey`).
2. Spawn N (e.g., 8) goroutines that all call `cs.Decrypt(logger, counter, out, packet, nb)` concurrently with the *same* `counter` and `packet`, using a `sync.WaitGroup` and a barrier (e.g. a channel close) to maximize the race window.
3. Count successes (`err == nil`) via an atomic counter, and instrument/wrap `dKey` (or use a counting decorator around `DecryptDanger`) to count how many times decryption itself was actually invoked.
4. Assert: exactly one goroutine returns success and the rest return `ErrAlreadySeen`; assert the decrypt-invocation counter to detect whether it was invoked more than once (documenting the double-invocation bug even though only one caller ultimately gets a "success" result).
5. Run with `go test -race -run TestConnectionState_Decrypt_Race -count=100` to demonstrate the race is reproducible under `-race` and that `DecryptDanger` executes more than once per unique counter across repeated runs.

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
