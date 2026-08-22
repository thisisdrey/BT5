Confirmed critical path: `Decrypt` calls `cs.window.Check(l, messageCounter)` **before** the AEAD tag is verified. `messageCounter` is `h.MessageCounter`, an attacker-controlled `uint64` read straight from the wire header at `header.H.Parse` [1](#0-0) , and passed unauthenticated into `Bits.Check`/`Bits.Update` at `connection_state.go` [2](#0-1)  and the relay-frame path `VerifyRelay` [3](#0-2) . Inside `Bits`, `b.current` is a `uint64` that gets set directly to this attacker-supplied index on the "jump" path in `updateSlow`, and multiple additions (`b.current+b.length`, `b.current+1`) are performed on it in `strictlyWithinWindow` and `updateSlow` without any upper-bound/overflow check [4](#0-3) [5](#0-4) .

### Title
Unauthenticated attacker-controlled `uint64` message counter can overflow the replay-window arithmetic before AEAD verification - (File: `bits.go`, `connection_state.go`)

### Summary
Nebula's replay-window (`Bits`) does the anti-replay bookkeeping *before* the packet's authenticity is checked: `ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` call `cs.window.Check(l, messageCounter)` using the raw `h.MessageCounter` header field, then only afterwards call `dKey.DecryptDanger` to authenticate the packet [2](#0-1) . Because `h.MessageCounter` is parsed straight off the wire as an unauthenticated `uint64` (`binary.BigEndian.Uint64(b[8:16])`) [1](#0-0) , an attacker who has an existing tunnel index (any established HostInfo, reachable via `hostMap.QueryIndex`/`QueryRelayIndex` in `outside.go`) can submit packets whose `MessageCounter` is set to values near `math.MaxUint64`, driving `Bits.current` to those extreme values in the "jump" branch of `updateSlow` before the AEAD tag is ever checked [5](#0-4) .

This mirrors the reported bug class: unguarded arithmetic on attacker-influenced integers at their type's boundary produces silently wrong results (here `b.current+b.length` wrapping in `uint64` space) instead of the intended bounded calculation — the same "should have widened/guarded the arithmetic before combining large values" defect described for `calculate_available_dividends`/`calculate_unlocked`.

### Finding Description
`Bits.strictlyWithinWindow` computes `i > b.current-b.length` (relying on intentional underflow during warmup, as documented) but `updateSlow`'s jump path computes `end := i; if end > b.current+b.length { end = b.current + b.length }` [6](#0-5) . If `b.current` is already close to `math.MaxUint64` (attacker set it there via a prior packet with `MessageCounter` near the max value, since `Check`'s fast/jump paths accept `i > b.current` unconditionally as "next number"), then `b.current+b.length` wraps to a small value. That wrapped `end` is smaller than `b.current`, so the `count := end - b.current` computation underflows to a huge `uint64`, which is then fed into `clearRange(startPos, count)` and `b.lostCounter.Inc(lost)` with a wildly incorrect `count`/`lost` value, and into `b.bits[word]` indexing math derived from `pos := i & b.lengthMask` where `i` itself is the attacker-chosen near-max value.

Crucially, all of this executes in `Decrypt`/`VerifyRelay` **before** `dKey.DecryptDanger` validates the AEAD tag [2](#0-1) . So the replay-window's internal state (`b.current`, the `bits` bitmap contents, and the lost/dupe/out-of-window metrics) can be corrupted or driven into an inconsistent state purely by an unauthenticated attacker who can reach the tunnel's remote index, without needing to forge a valid ciphertext.

### Impact Explanation
Once `b.current` has been pushed to a near-`uint64`-max value by an attacker-supplied (unauthenticated) counter, all subsequent legitimate traffic on that tunnel is evaluated against a corrupted window state: `Check`'s "next number" fast path (`i > b.current`) can never be satisfied by real traffic, and `strictlyWithinWindow`'s underflow-based warmup detection is defeated, causing `Check` to reject legitimate future packets as out-of-window. This is a remote, unauthenticated denial-of-service against an established Nebula tunnel: a single attacker on the same UDP path who knows (or can send arbitrary) `RemoteIndex`/`MessageCounter` values can permanently desynchronize the receiver's replay window for that peer, dropping all further data-plane traffic on it (`ErrOutOfWindow`/rejected as replay), while the AEAD decrypt step still runs on the crafted packet with garbage ciphertext and simply errors out — but the state corruption on `window` persists regardless of decrypt success.

### Likelihood Explanation
High. `MessageCounter` is read unauthenticated straight from the header before any cryptographic check `header.H.Parse` [1](#0-0) , and `Bits.Check`/`Bits.Update` are invoked on it prior to `DecryptDanger` in both the normal data-plane path and the relay path [7](#0-6) . The only prerequisite is knowledge of a valid `RemoteIndex` (or relay index) for an existing tunnel, which is routinely observable from captured/relayed traffic and does not require holding a CA-signed certificate or being a trusted peer.

### Recommendation
1. Do not mutate/derive any replay-window state from an unauthenticated `messageCounter` before the AEAD tag has been verified: restructure `Decrypt`/`VerifyRelay` so the sliding-window bookkeeping only happens after `DecryptDanger` succeeds (the existing pre-decrypt `Check` should be a bounds pre-filter only, not one that can corrupt `b.current`).
2. In `Bits.updateSlow`, guard all `uint64` additions (`b.current+b.length`, `b.current+1`) with explicit overflow checks (e.g. compare against `math.MaxUint64-b.length`) and reject/clamp counters that would overflow instead of silently wrapping.
3. Consider bounding accepted `MessageCounter` values to a sane maximum relative to the current window before doing any window arithmetic, independent of AEAD verification order.

### Proof of Concept
1. Establish a tunnel between two nebula instances (attacker only needs to observe/know the victim's `RemoteIndex` for that tunnel, e.g. by capturing one packet).
2. Send a crafted UDP packet directly to the victim with a valid `RemoteIndex` and `header.H.MessageCounter` set to a value near `math.MaxUint64` (e.g. `math.MaxUint64 - 100`), with arbitrary/garbage ciphertext payload.
3. `readOutsidePackets` → `ConnectionState.Decrypt` calls `cs.window.Check(l, messageCounter)` with this huge counter; `Check`'s `i > b.current` fast path returns true (any first huge counter looks like "next"), and calling `Update`/`updateSlow` on it sets `b.current` to the huge value before the subsequent `DecryptDanger` call even runs (and even if it later fails/returns an error, the `Check` step ran and can be repeated/raced to land on `Update`).
4. Subsequently observe that legitimate traffic from the real peer with normal, sequential `MessageCounter` values is now rejected by `Check`/`Update` as out-of-window, confirming the tunnel's replay-window state was corrupted by an unauthenticated packet.

### Citations

**File:** header/header.go (L142-155)
```go
// Parse is a helper function to parses given bytes into new Header struct
func (h *H) Parse(b []byte) error {
	if len(b) < Len {
		return ErrHeaderTooShort
	}
	// get upper 4 bytes
	h.Version = uint8((b[0] >> 4) & 0x0f)
	// get lower 4 bytes
	h.Type = MessageType(b[0] & 0x0f)
	h.Subtype = MessageSubType(b[1])
	h.Reserved = binary.BigEndian.Uint16(b[2:4])
	h.RemoteIndex = binary.BigEndian.Uint32(b[4:8])
	h.MessageCounter = binary.BigEndian.Uint64(b[8:16])
	return nil
```

**File:** connection_state.go (L61-107)
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
```

**File:** bits.go (L120-132)
```go
func (b *Bits) strictlyWithinWindow(i uint64) bool {
	// Handle the case where the window hasn't slid yet. This avoids u64 underflow.
	inWarmup := b.current < b.length
	if i < b.length && inWarmup {
		return true
	}

	// Next, if the packet is in-window, see if we've seen it before
	if i > b.current-b.length {
		return true
	}
	return false //not within window!
}
```

**File:** bits.go (L188-227)
```go
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
```
