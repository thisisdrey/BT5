### Title
Replay-window check/decrypt/update race allows duplicate packet acceptance in `ConnectionState.Decrypt`/`VerifyRelay` - (File: connection_state.go)

### Summary
The reported Gelato bug is a check-then-act pattern (check balance, perform a side-effecting operation, re-check balance) that a reentrant call can exploit because the lock/guard around the check and the effect is not held atomically. The Nebula analog is the anti-replay accounting in `ConnectionState.Decrypt` and `ConnectionState.VerifyRelay`: the replay-window `Check`, the AEAD decrypt, and the replay-window `Update` are three separate, non-atomic critical sections that release `decryptLock` in between.

### Finding Description
`ConnectionState.Decrypt` performs:
1. Lock, `cs.window.Check(l, messageCounter)`, unlock.
2. Perform `cs.dKey.DecryptDanger(...)` with no lock held.
3. Lock, `cs.window.Update(l, messageCounter)`, unlock. [1](#0-0) 

`VerifyRelay` has the identical structure for relay frames. [2](#0-1) 

Because the lock is dropped between the `Check` and the `Update`, two concurrent invocations for the *same* `messageCounter` (e.g. a duplicated/replayed UDP datagram delivered to two reader goroutines, or the same packet processed twice via the recursive relay path in `handleOutsideRelayPacket`) can both pass `Check` (since neither has yet called `Update` to mark the counter as seen), both successfully decrypt the same valid ciphertext, and only one will fail at the final `Update`. This mirrors the report's root cause exactly: a check is performed, a state-changing/side-effecting operation runs unguarded, and only afterward is the check state (the counter bitmap / balance) committed — allowing a concurrent duplicate to "reenter" the gap and be treated as freshly-authenticated traffic before the anti-replay bookkeeping catches up.

The `Bits.Check`/`Bits.Update` split itself documents that `Update` is what actually marks the bit and can return `false` for a duplicate, meaning `Check` alone is not sufficient proof of freshness once the lock is released. [3](#0-2) [4](#0-3) 

The caller `readOutsidePackets` treats a successful `Decrypt`/`VerifyRelay` as an authoritative "not a replay" signal and immediately proceeds to hand the plaintext to the firewall/tun or forward it via relay, with no additional replay defense downstream. [5](#0-4) [6](#0-5) 

### Impact Explanation
If the check/decrypt/update race is exploitable (requires two goroutines processing the same authenticated ciphertext/counter concurrently — e.g. via duplicated UDP delivery paths, relay recursion in `handleOutsideRelayPacket` calling back into `readOutsidePackets`, or an attacker-induced duplicate delivery), a single legitimately-captured/observed packet could be accepted and delivered to the tun device or forwarded twice, defeating the anti-replay guarantee that the sliding window is meant to enforce. This does not grant decryption/forgery capability (AEAD auth still required) but does allow replay/duplication of already-authenticated traffic, which is precisely the class of bug the reentrancy report targeted (bypassing an intended "exactly once" accounting guarantee through a non-atomic check-act-update sequence).

### Likelihood Explanation
Exploitability depends on actually triggering two concurrent calls to `Decrypt`/`VerifyRelay` with the same `messageCounter` on the same `ConnectionState`, which requires either duplicate UDP delivery at the OS/network layer or multi-worker read paths racing on the same packet. This is a narrower trigger than the original report's fully attacker-controlled reentrant contract call, so likelihood is lower and depends on Nebula's runtime concurrency model (number of reader queues `q`, and whether `readOutsidePackets` for a single physical packet can run on more than one goroutine). I could not fully verify from the available index whether the multi-queue reader (`q int` parameter) can dispatch true duplicates of the same wire packet to two goroutines simultaneously, so likelihood should be treated as uncertain pending confirmation of the reader dispatch model.

### Recommendation
Hold `decryptLock` across the entire check-decrypt-update sequence in both `Decrypt` and `VerifyRelay` (or otherwise make the check-and-reserve atomic, e.g. reserve the counter slot before decrypting and roll back on decrypt failure) so no other caller can observe `Check==true` for a counter that is mid-flight and not yet committed.

### Proof of Concept
Conceptual PoC (not fully verified against the reader dispatch code, which the index did not expose in full):
1. Establish a tunnel between two Nebula instances.
2. Capture one legitimate data-plane UDP packet with counter `N` in flight to the responder.
3. Arrange for it to be delivered twice to the responder's `readOutsidePackets` concurrently (e.g., duplicate at the UDP/OS level, or via the relay recursion path where the same signed payload is re-entered through `readOutsidePackets`).
4. Both goroutines call `hostinfo.ConnectionState.Decrypt` for counter `N`; both execute `window.Check` before either executes `window.Update`, so both see `true`.
5. Both successfully decrypt (same ciphertext, same counter — AEAD succeeds twice) and only one `Update` call marks the bit; the other's `Update` returns false, but decryption/processing already occurred for both, resulting in double delivery/acceptance of a duplicate/replayed data-plane message.

Because I was unable to confirm from the indexed code whether Nebula's actual UDP read pipeline can present the exact same wire packet twice to concurrent goroutines before either finishes processing, this PoC is presented as a code-level reachability path (the lock gap in `connection_state.go`) rather than a confirmed end-to-end exploit; a Devin session with full repo/runtime access would be needed to validate the multi-queue reader concurrency model and reproduce the race in practice.

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

**File:** connection_state.go (L85-108)
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

**File:** outside.go (L105-136)
```go
	if len(packet) < header.Len+hostinfo.ConnectionState.dKey.Overhead() {
		f.messageMetrics.RxInvalid(1)
		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			f.l.Debug("packet too small", "from", via, "length", len(packet))
		}
		return
	}

	// All remaining packets are encrypted
	if isMessageRelay {
		// Relay packets are special, this branch should always early-return
		if err = hostinfo.ConnectionState.VerifyRelay(f.l, h.MessageCounter, packet, nb); err != nil {
			if f.l.Enabled(context.Background(), slog.LevelDebug) {
				hostinfo.logger(f.l).Debug("Failed to verify relay packet", "error", err, "from", via, "header", h)
			}
			return
		}
		f.handleOutsideRelayPacket(hostinfo, via, out, packet, h, fwPacket, lhf, nb, q, localCache)
		return
	}

	out, err = hostinfo.ConnectionState.Decrypt(f.l, h.MessageCounter, out, packet, nb)
	if err != nil {
		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			hostinfo.logger(f.l).Debug("Failed to decrypt packet", "error", err, "from", via, "header", h)
		}
		return
	}

	// Roam before we respond
	f.handleHostRoaming(hostinfo, via)
	f.connectionManager.In(hostinfo)
```
