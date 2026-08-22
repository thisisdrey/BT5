### Title
Anti-replay window Check/Update race allows duplicate packets to bypass replay protection - ([File: connection_state.go])

### Summary
`ConnectionState.Decrypt()` in `connection_state.go` splits the replay-window check-and-mark operation into two separately-locked steps, with the expensive AEAD decryption performed *between* them while the lock is released. This mirrors the reported reentrancy pattern in `FuroVesting.stopVesting()`, where a state-mutating side effect (`delete vests[vestId]`) was deferred until after an external, potentially re-entrant operation (`_transferToken()`), letting an attacker trigger the same effect twice before the guarding state was updated. Here, the guarding state is the anti-replay bitmap (`Bits`), and the "external operation" is the packet decrypt call — both windows of opportunity are structurally the same: check → do something slow/interruptible → update state.

### Finding Description
`Decrypt()` does:
1. `decryptLock.Lock(); result := window.Check(...); decryptLock.Unlock()`
2. `dKey.DecryptDanger(...)` (AEAD decrypt, done fully unlocked)
3. `decryptLock.Lock(); result = window.Update(...); decryptLock.Unlock()` [1](#0-0) 

`Bits.Check()` only *reads* whether the counter is already marked seen; it does not mark it. [2](#0-1) 
The mark only happens later in `Update()`. [3](#0-2) 

Because the lock is dropped between `Check` and `Update`, two calls to `Decrypt()` for the same `ConnectionState` with the same `messageCounter` (e.g., the same UDP datagram duplicated at the network layer, or replayed by a network-position/off-path attacker who can duplicate captured ciphertext before the legitimate packet's `Update` call commits) can both pass `Check` before either reaches `Update`. This is invoked directly from the outside-packet processing path, `hostinfo.ConnectionState.Decrypt(...)`, which is reachable by any attacker sending UDP traffic to the node, without needing a CA-signed certificate — it operates purely on the ciphertext/header of already-established tunnels and is exercised on every inbound data/control packet. [4](#0-3) 

This is structurally identical to the reported bug class: a guard (`vests[vestId]` / anti-replay bit) is checked, then an operation that can be duplicated/raced is performed (`_transferToken()` / `DecryptDanger`), and only afterward is the guard state persisted (`delete vests[vestId]` / `window.Update`). In both cases, an attacker can force the guarded operation to execute more than once for what should be a single logical event, because the state commit is deferred past the point where duplication/reentrance can occur.

### Impact Explanation
If exploitable, this allows bypass of Nebula's anti-replay protection, permitting duplicate delivery of decrypted application traffic to the TUN device (`f.readers[q].Write(out)` at [5](#0-4) ) or duplicate processing of control/lighthouse/test messages, i.e., remote state poisoning / traffic replay within an already-established tunnel. This does not bypass certificate/handshake authentication itself (an attacker still needs a captured/duplicated ciphertext from a live tunnel), but it does undermine one of the confidentiality/integrity guarantees (anti-replay) that Nebula's data plane relies on.

### Likelihood Explanation
Exploitation requires precise timing: the attacker (or network conditions) must deliver a duplicate UDP datagram to the node's listener such that two goroutines are executing `Decrypt()` concurrently for the same counter, and the duplicate's `Check()` call must land in the narrow unlocked window before the original's `Update()` runs. UDP duplication can occur naturally (network retransmission at lower layers, NIC/driver duplication) or be induced by an attacker capable of duplicating packets on the path (a classic replay/duplicate-packet primitive, no valid certificate required). Whether Nebula's I/O architecture actually processes packets for the *same* `hostinfo`/`ConnectionState` concurrently on multiple goroutines (multiple read queues) could not be conclusively confirmed from the available context — this determines whether the race is practically reachable at all, so likelihood is uncertain without further verification of the reader/queue concurrency model.

### Recommendation
Combine the anti-replay check-and-mark into a single atomic operation performed inside one critical section, or hold the `decryptLock` for the entire duration from `Check` through `Update` (accepting the cost of serializing decrypt calls), so that no other goroutine can observe an "unseen" counter as still unseen after another goroutine has begun processing it but before it has committed. Alternatively, restructure so the window bit is provisionally marked (reserved) at `Check` time and only rolled back on decrypt failure, closing the TOCTOU gap — analogous to applying check-effects-interactions before the "external call" (the decrypt operation).

### Proof of Concept
Could not be constructed/verified within available tooling — this requires confirming (a) whether the daemon's UDP read path can invoke `Decrypt()` concurrently for the same `ConnectionState` from multiple goroutines, and (b) whether an attacker can reliably deliver a duplicate ciphertext narrowly enough to land inside the unlocked window between `Check` and `Update`. Both points need dynamic/runtime verification (e.g., a Devin session with test execution) that isn't available here.

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

**File:** outside.go (L126-132)
```go
	out, err = hostinfo.ConnectionState.Decrypt(f.l, h.MessageCounter, out, packet, nb)
	if err != nil {
		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			hostinfo.logger(f.l).Debug("Failed to decrypt packet", "error", err, "from", via, "header", h)
		}
		return
	}
```

**File:** outside.go (L516-516)
```go
	_, err = f.readers[q].Write(out)
```
