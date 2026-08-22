## Title
Check-then-Decrypt-then-Update TOCTOU in `ConnectionState.Decrypt` permits replay-window bypass under concurrent packet delivery - (File: `connection_state.go`)

## Summary
`ConnectionState.Decrypt` follows the same anti-pattern the external report flags in `LBToken._transfer`: it reads the "current state" (the replay-window bit for a message counter) into an ephemeral result via `window.Check`, then performs the security-relevant side effect (AEAD decryption) *before* the state is durably committed via `window.Update`, releasing and re-acquiring `decryptLock` in between rather than holding a single critical section across the read-decide-write sequence.

## Finding Description
`Decrypt` locks, calls `cs.window.Check(l, messageCounter)`, unlocks, decrypts, then locks again and calls `cs.window.Update(l, messageCounter)`, unlocking again: [1](#0-0) 

`Bits.Check` only inspects whether `i` is inside the window and unset — it does not mark the counter as seen; only `Bits.Update` mutates the bitmap: [2](#0-1) [3](#0-2) 

Because `Check` and `Update` are two separate lock-protected operations with the (comparatively expensive) AEAD `DecryptDanger` call sandwiched in between and no lock held across the whole sequence, two packets carrying the *same* `messageCounter` that arrive concurrently (or are dispatched to different goroutines/readers) can both pass `Check` before either has called `Update`. This is structurally identical to the LBToken bug: a temporary/ephemeral read (`_fromBalance`/`Check` result) is used to authorize an action, and the corresponding write-back (`_balances[_id][_from] = ...`/`Update`) that should have prevented the duplicate is deferred and can be raced. `VerifyRelay` has the identical shape: [4](#0-3) 

The CHANGELOG documents that the team is actively aware replay-window updates are lock-sensitive and previously buggy on other paths ("Lock replay window updates so concurrent readers can't corrupt it" (#1802); "Advance the replay window on relayed packets so a relay drops replayed frames instead of re-forwarding them" (#1751)), confirming this exact class of bug has occurred before in this codebase and was only partially remediated: [5](#0-4) 

## Impact Explanation
If the race is winnable, an attacker who can deliver two copies of the same captured/replayed ciphertext packet to the victim in a tight enough window (e.g., via UDP duplication, a malicious relay, or simply flooding two copies back-to-back before the first `Update` call completes) can get both copies decrypted and handed to the application/tun path, i.e., the replay-protection guarantee is defeated for that one packet id. This falls squarely in the "traffic decryption/forgery/replay" impact bucket: the anti-replay window is the mechanism nebula relies on to prevent packet replay after decryption succeeds once; a bypass here reintroduces replay of tunnel payloads (duplicate delivery to the tun device) or, on the relay path (`VerifyRelay`), duplicate re-forwarding of relay frames — the very behavior #1751 was meant to fix.

## Likelihood Explanation
This requires no CA-signed certificate and no privileged network position beyond what any off-path attacker already has for a UDP-based protocol: the ability to capture/replay a valid ciphertext once a session exists, and to deliver two copies close together (classic UDP duplication/racing is easy for a network attacker; a malicious/relay-adjacent attacker has an even easier time). The race window is the time between `Check`'s unlock and `Update`'s lock re-acquisition, which spans a full AEAD decrypt call — non-trivial but plausible under packet flooding, especially against multi-core/multi-reader dispatch. This is a timing-dependent bug, so likelihood is moderate rather than certain, and I was not able to fully confirm within the available tool calls whether packets for a single `ConnectionState`/tunnel are always serialized onto one goroutine by the reader dispatch (which would reduce or eliminate the race) or can be processed by multiple UDP reader routines concurrently — this reachability detail should be verified against `outside.go`/`interface.go`'s packet dispatch loop.

## Recommendation
Hold a single lock (or use one atomic check-and-set operation, e.g., have `Bits` expose a combined "CheckAndReserve" that inspects-and-marks the bit atomically) across the entire Check → Decrypt → Update sequence, or equivalently reorder so the window bit is committed (tentatively reserved) before decryption begins and rolled back only on genuine decrypt failure, ensuring no two goroutines can simultaneously observe an "unset" bit for the same counter and both proceed to decrypt.

## Proof of Concept
Not independently reproducible from static analysis alone within this session — a concrete PoC would require instrumenting/racing two goroutines against a shared `ConnectionState.Decrypt` call with the same `messageCounter` (analogous to a `go test -race` style harness sending the same captured relay/data frame twice in parallel) and observing whether `DecryptDanger` succeeds and returns a usable plaintext for both, similar to how `TestRelayReplayProtection` in `e2e/tunnels_test.go` validates sequential replay is dropped but does not test concurrent/racing delivery: [6](#0-5)

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
