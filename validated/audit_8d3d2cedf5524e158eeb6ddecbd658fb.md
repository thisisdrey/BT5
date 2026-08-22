### Title
Replay-window check-then-update race in `ConnectionState.Decrypt`/`VerifyRelay` allows duplicate packets to bypass anti-replay protection - (File: connection_state.go)

### Summary
The reported auction bug is a classic "check value, act on stale value" race: a security-relevant decision (the auction's starting price) is computed from mutable state, and an attacker can mutate that state after the check but before the effect is finalized, causing the check to be effectively bypassed for a worse outcome. The same TOCTOU (time-of-check/time-of-use) shape exists in Nebula's anti-replay counter handling in `ConnectionState.Decrypt` and `ConnectionState.VerifyRelay`: the replay-window `Check` and the replay-window `Update` are two separate, independently-locked operations with attacker-controlled work (AEAD decryption) happening in between, unlocked.

### Finding Description
`ConnectionState.Decrypt` performs the anti-replay check and the anti-replay commit as two separate critical sections: [1](#0-0) 

```
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
return out, nil
```

`Bits.Check` only *reads* whether counter `i` has been marked; it does not mark it. The bit is only set by `Bits.Update`, called separately, after decryption: [2](#0-1) [3](#0-2) 

Between the first `Lock`/`Check`/`Unlock` and the second `Lock`/`Update`/`Unlock`, `cs.decryptLock` is released, and the decryption of the packet body happens outside the lock. Nebula reads inbound UDP packets on multiple queue workers (`readOutsidePackets` is invoked per-queue index `q`), and `outside.go` dispatches directly into `hostinfo.ConnectionState.Decrypt`/`VerifyRelay` without any per-message-counter serialization beyond this window: [4](#0-3) [5](#0-4) 

If two copies of the same on-wire ciphertext packet (an on-path/network attacker duplicating a captured UDP datagram — no CA-signed certificate or session key required to do this) arrive close together and are processed by different queue goroutines, both can pass `Check` (since neither has called `Update` yet), both proceed to `DecryptDanger`, and both attempt `Update`. This mirrors the bug-class in the reported finding: a value gated by a "check" (the replay window state) can be raced/frontrun by a second use of the *same* input before the "commit" (Update) that is supposed to make the check authoritative actually lands, producing two successful decrypt paths for what should be a single-use counter.

`VerifyRelay` has the identical structure: [6](#0-5) 

### Impact Explanation
A successful race allows a duplicated/replayed ciphertext packet to be decrypted and delivered to the TUN device (or relay-forwarded) twice instead of being rejected as a duplicate, defeating the intended one-time-delivery guarantee of the anti-replay window. This is a remote-state-poisoning/duplication issue in a core security control (nonce/replay handling) that an on-path attacker can trigger purely by capturing and re-injecting an already-observed ciphertext packet — no valid certificate, handshake participation, or key material is required from the attacker.

### Likelihood Explanation
Triggering the race requires (a) an attacker able to duplicate/replay an observed UDP packet onto the wire so two copies race across Nebula's queue workers, and (b) the two copies landing on different reader goroutines within the small window between `Check` and `Update`. This is a genuine, narrow race window rather than a deterministic bypass, so likelihood is lower than the original financial-frontrunning report, but the mechanism is real and directly analogous: a security check and its corresponding state commit are non-atomic, and attacker-controlled network timing/duplication can be used to land actions inside that gap.

### Recommendation
Make the replay-window check-and-mark operation atomic: hold `decryptLock` (or an equivalent per-counter lock) across both the `Check` and the `Update` (or fold them into a single `CheckAndReserve`-style call) so no other goroutine can observe a "not yet seen" result for a counter that is concurrently being processed. At minimum, perform the `Update`/reservation before starting the expensive decrypt operation, and roll it back only on decrypt failure, so a duplicate cannot slip through the gap.

### Proof of Concept
1. Establish a Nebula tunnel between two hosts so a `ConnectionState` with an active replay window exists.
2. Capture one valid encrypted `header.Message` packet on the wire (e.g., via a network tap/MITM position — no cert or key needed).
3. Re-inject two copies of the exact same captured packet in rapid succession such that Nebula's multiple UDP-reader queues (`q` in `readOutsidePackets`) each pick up one copy and call `hostinfo.ConnectionState.Decrypt` concurrently.
4. If both goroutines execute `window.Check` before either executes `window.Update` (the unlocked gap around `DecryptDanger` in `connection_state.go:61-82`), both will decrypt successfully and both payloads will be forwarded to the TUN device / relay path, demonstrating a duplicate delivery that the anti-replay window was designed to prevent.

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

**File:** outside.go (L25-25)
```go
func (f *Interface) readOutsidePackets(via ViaSender, out []byte, packet []byte, h *header.H, fwPacket *firewall.Packet, lhf *LightHouseHandler, nb []byte, q int, localCache firewall.ConntrackCache) {
```

**File:** outside.go (L113-121)
```go
	// All remaining packets are encrypted
	if isMessageRelay {
		// Relay packets are special, this branch should always early-return
		if err = hostinfo.ConnectionState.VerifyRelay(f.l, h.MessageCounter, packet, nb); err != nil {
			if f.l.Enabled(context.Background(), slog.LevelDebug) {
				hostinfo.logger(f.l).Debug("Failed to verify relay packet", "error", err, "from", via, "header", h)
			}
			return
		}
```
