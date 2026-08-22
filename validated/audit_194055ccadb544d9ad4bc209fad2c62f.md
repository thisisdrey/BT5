### Title
Check/Update TOCTOU race in the anti-replay window permits a captured packet to be replayed before the replay bitmap is marked - (File: connection_state.go)

### Summary
The Fractional `Buyout.cash` bug is a stale-state accounting flaw: `buyoutShare` is computed from the immutable `buyoutInfo[_vault].ethBalance` on every call instead of a value that reflects prior withdrawals, so state read at cash-out time doesn't account for a concurrently-completed related operation, letting a second caller extract more than they should. The reachable analog in Nebula is the anti-replay `Bits` window used in `ConnectionState.Decrypt` / `ConnectionState.VerifyRelay`: the "check" and the "commit" (`Update`) of the replay bitmap are two separate, individually-locked operations with the decryption work sandwiched in between, so the authoritative state (`window` bitmap) used to reject replays is not updated atomically with the check that grants admission — exactly the same "read stale shared state, act on it, then reconcile later" pattern.

### Finding Description
`ConnectionState.Decrypt` first takes `decryptLock`, calls `cs.window.Check(l, messageCounter)`, and releases the lock [1](#0-0) . Only after the (comparatively expensive) AEAD decrypt completes does it re-take the lock and call `cs.window.Update(l, messageCounter)` to actually mark the counter as seen [2](#0-1) . `VerifyRelay` follows the identical two-phase check-then-update pattern for relay frames [3](#0-2) .

Because the bitmap mutation that actually prevents replay (`window.Update`, which calls `b.set(i)` / flips the bit for `i`) only happens after decryption, any two packets carrying the same `messageCounter` that are processed concurrently (e.g., the original packet and an attacker-replayed copy captured off the wire and re-injected before the first copy finishes decrypting) will both pass `Check` — because neither goroutine's `Update` has run yet to record the counter as consumed. `Bits.Check` only consults `b.get(i)`/`b.current`, and `Bits.Update` is the only call that mutates the bitmap [4](#0-3) [5](#0-4) . The lock is only held during each of `Check` and `Update` individually, never across the whole check-decrypt-update sequence, so the "ethBalance not decremented before the next reader observes it" class of bug reappears here as "the replay bitmap is not marked before the next reader with the same counter observes it."

### Impact Explanation
A successful race lets an attacker who can capture and replay a single UDP packet toward the victim (no valid certificate needed — this is purely a transport/anti-replay defect, exploitable by any network-adjacent attacker who can duplicate a packet before the legitimate copy is processed) cause the same encrypted application/relay message to be accepted and delivered to the TUN device (or forwarded through the relay path) twice. This defeats the core guarantee of the "Anti-Replay" subsystem documented for Nebula, whose entire purpose is to guarantee each message counter is consumed exactly once. Depending on the payload this can duplicate injected traffic, application-level replay effects, or double-processing of relay-forwarded control data.

### Likelihood Explanation
Exploitability requires only network-level packet duplication/injection timed to land while the first copy is mid-decrypt — no cryptographic material, no valid certificate, and no privileged position beyond the ability to observe and duplicate a ciphertext on the wire (which the Nebula threat model already assumes attackers can do, since it's precisely what anti-replay protection is meant to stop). The race window is bounded by one AEAD decrypt operation, so likelihood is moderate: it requires precise timing but is deterministically reproducible by an attacker who controls delivery timing of the duplicate packet (e.g., via a local relay/attacker-controlled network path), and there's no other window-based defense once concurrent decrypts of the same counter are both admitted at the `Check` stage.

### Recommendation
Make the "reserve this counter" step atomic with the check: hold `decryptLock` across the check-and-reserve step (e.g., call `Check` then immediately `set`/reserve the bit for `i` under the same lock acquisition, before releasing it to perform the AEAD decrypt), and only roll the reservation back if decryption subsequently fails. Concretely, merge `Check`+the bit-set into a single locked "TestAndSet" operation performed before decryption, rather than performing `Check`, unlocking, decrypting, relocking, and calling `Update` afterward.

### Proof of Concept
Conceptual PoC (cannot be executed without the code repository, but derivable directly from the control flow at [6](#0-5) ):
1. Attacker captures one legitimate UDP data packet with `messageCounter = N` addressed to a Nebula node.
2. Attacker immediately re-injects a duplicate of that packet toward the same node, timed so it arrives while the first copy is still inside `cs.dKey.DecryptDanger(...)` (between the `Check` and `Update` calls) on `line 64` and `line 76` of `connection_state.go`.
3. Both goroutines processing the two packets call `cs.window.Check(l, N)` before either has called `cs.window.Update(l, N)`; since `Check` only reads `b.get(i)`/`b.current` and the bit for `N` is not yet set, both return `true`.
4. Both packets proceed to `DecryptDanger` and succeed (same key, same counter, same ciphertext ⇒ same plaintext), and both subsequently call `Update`, with only the second `Update` call returning `false` (rejected as a dupe) — but by that time the plaintext from both decrypt calls has already been returned to the caller and handed off for processing (e.g., written to TUN), so the replay has already taken effect once for each copy.

### Citations

**File:** connection_state.go (L61-81)
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
