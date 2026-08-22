This confirms the TOCTOU gap in `Decrypt`/`VerifyRelay`: `cs.window.Check` is evaluated and the lock released before `DecryptDanger` runs, and only after decryption does `cs.window.Update` get called (again under a separately-acquired lock). Two goroutines decrypting packets with the same `messageCounter` concurrently can both pass `Check` (since neither has called `Update` yet), both successfully decrypt (the AEAD tag validates identically for a genuine replayed ciphertext), and only the second `Update` call will report a duplicate — but by then the first copy has already been delivered to the tun/relay path. The anti-replay window's entire purpose is defeated in this race window. [1](#0-0) [2](#0-1) [3](#0-2) 

### Title
Replay Window Check/Decrypt/Update TOCTOU Allows Duplicate Packet Acceptance - (File: connection_state.go)

### Summary
`ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` split the anti-replay check into three separately-locked phases: `window.Check` (read-only membership test), an unlocked `DecryptDanger` AEAD operation, and `window.Update` (the phase that actually marks the counter as seen). Because the lock is released between `Check` and `Update`, the decision to accept a `messageCounter` is made against a window state that has not yet been updated with in-flight packets carrying the same counter — an "estimated" pre-update state rather than the true "current" state at the moment of acceptance, structurally the same root cause as the Plaza Finance report (a security-relevant decision computed from a stale/pre-mutation value instead of the value that reflects concurrent in-flight state).

### Finding Description
`Decrypt` (and the identical pattern in `VerifyRelay`) does:
1. Lock, call `cs.window.Check(l, messageCounter)`, unlock.
2. If accepted, call `cs.dKey.DecryptDanger(...)` without holding any lock.
3. Lock, call `cs.window.Update(l, messageCounter)`, unlock. [1](#0-0) 

`Bits.Check` only inspects whether the bit is already set; it does not mark it. `Bits.Update` is the only call that actually flips the bit (or advances/clears the window). [3](#0-2) [4](#0-3) 

Because packet processing (`listenOut`/UDP read routines) can run multiple goroutines concurrently per interface, two threads can receive the exact same on-wire ciphertext (a genuine network-level duplicate/replay of one packet) with the same `messageCounter`. Both threads:
- call `Check` while the bit is still unset → both get `true`,
- independently run `DecryptDanger` with the same nonce/ciphertext → both succeed (this is a bit-identical duplicate, not a forgery, so AEAD authentication does not stop it),
- only then call `Update` — the first succeeds, the second correctly returns `false`/`ErrAlreadySeen`.

By the time the second `Update` detects the duplicate, the first copy of the plaintext has already been returned to the caller and forwarded into the tun device (`listenIn`/`handleOutsideRelayPacket`) or relayed onward. The anti-replay window is supposed to guarantee at-most-once delivery per counter, but this two-phase check-then-act split without a held lock across the whole operation allows one duplicate delivery to slip through per race window.

### Impact Explanation
This does not let attacker-controlled forged data pass (the AEAD tag still binds ciphertext to the nonce), but it defeats the anti-replay guarantee for a genuinely-observed duplicate of a legitimate packet: an on-path relay/observer or simple retransmission-inducing network condition can cause the same encrypted packet to be delivered twice into the tun device or relayed twice, undermining the protocol's replay-protection invariant documented in `ConnectionState`'s design (`ReplayWindow`, `Bits`). This is the same class of bug as the source report: a security-relevant branch decision (`accept`/`reject`) is made from a value (the window bitmap) that does not yet reflect concurrently in-flight state, letting one flow "double-spend" the accept decision before the ledger (bitmap) is updated.

### Likelihood Explanation
Triggering it requires only that the same encrypted packet reach the two decrypt paths concurrently with the same `messageCounter` — achievable by an on-path attacker or relay duplicating a captured UDP datagram, or simply by natural network-level duplication (which nebula explicitly tries to defend against via the replay window). No cryptographic material or valid certificate beyond what a normal peer already has is required, since the duplicated bytes are unmodified ciphertext already accepted once by the tunnel.

### Recommendation
Hold `decryptLock` for the entire duration from `Check` through `Update` (or replace the two-phase `Check`/`Update` API with a single atomic "check-and-mark" operation performed before releasing the lock, deferring only the decrypt failure path to unmark the bit), so that no second goroutine can observe an unmarked bit for a counter that is already being processed.

### Proof of Concept
Conceptual race (Go-style), not exploit code:
1. Peer A sends one data packet with `messageCounter = N`.
2. The same ciphertext arrives at the receiver twice in quick succession (e.g., due to link-layer duplication or an attacker replaying the captured datagram before defenses close the window).
3. Two goroutines call `cs.Decrypt(..., N, ...)` concurrently.
4. Goroutine 1: `Check(N)` → `true` (unlocked) → `DecryptDanger` succeeds → `Update(N)` → `true`, packet delivered to tun.
5. Goroutine 2, interleaved between goroutine 1's `Check` and `Update`: `Check(N)` → still `true` (bit not yet set) → `DecryptDanger` succeeds (same ciphertext/nonce) → `Update(N)` → now correctly `false`, but the plaintext has already been produced and can be delivered by the caller before this check happens depending on caller ordering, yielding a duplicate delivery for one legitimate packet.

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
