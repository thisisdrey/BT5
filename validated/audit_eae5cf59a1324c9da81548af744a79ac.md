### Title
Check-then-act race between replay-window `Check` and `Update` allows a duplicate/replayed data packet to be decrypted and delivered twice - (File: `connection_state.go`)

### Summary
The reported Mochi bug is a classic check-then-act flaw: `claimRewardAsMochi` read a reward balance, paid it out, but never zeroed the balance before returning, so the same state could be "claimed" repeatedly. The reachable analog in this codebase is `ConnectionState.Decrypt` in `connection_state.go`, which also splits a "check the counter hasn't been consumed" step from the "consume/mark the counter" step, with an unlocked window in between during which the same counter can pass the check twice.

### Finding Description
`ConnectionState.Decrypt` performs the anti-replay check and the anti-replay commit as two separate, individually-locked operations, with the actual AEAD decryption/packet acceptance happening in between while the lock is released: [1](#0-0) 

```
cs.decryptLock.Lock()
result := cs.window.Check(l, messageCounter)   // "read balance"
cs.decryptLock.Unlock()
...
out, err = cs.dKey.DecryptDanger(...)          // work done using unmarked state
...
cs.decryptLock.Lock()
result = cs.window.Update(l, messageCounter)   // "zero balance" - happens too late
cs.decryptLock.Unlock()
```

`Bits.Check` only tests whether the counter has already been marked seen; it does not itself mark anything. `Bits.Update` is the only call that actually records the counter as consumed: [2](#0-1) [3](#0-2) 

Because `Check` and `Update` are not atomic with respect to each other in `Decrypt`, two calls to `Decrypt` for the *same* `messageCounter` — e.g. an attacker capturing and replaying a valid ciphertext packet, or the same packet arriving to the interface twice (duplicate UDP delivery, multiple reader routines/`listen.batch`/`tun.routines`>1 fan-in via `readOutsidePackets` in `outside.go`) — can both pass `Check` before either has called `Update`. Both calls will then independently succeed in `DecryptDanger` (the AEAD nonce/counter is supplied externally per call and does not itself prevent reuse at this layer) and both will be delivered to the caller as valid decrypted plaintext.

This mirrors the reported vulnerability's root cause precisely: a resource-consuming action was gated on a "have you already used this?" check whose corresponding "mark as used" write was deferred/decoupled, permitting the same state to be exploited more than once before the write took effect.

### Impact Explanation
An attacker who can capture a single legitimate encrypted data-plane (or relay) packet between two already-established Nebula peers can replay it back onto the wire. If it races with the original packet's processing (or is replayed against a build/configuration where the interface has multiple concurrent reader paths feeding `readOutsidePackets`), the replayed packet can be decrypted and delivered to the tun device a second time despite the anti-replay window (`ReplayWindow`/`Bits`) being specifically designed to prevent exactly this. This defeats the anti-replay guarantee (`ErrAlreadySeen`) that the protocol design depends on for traffic-integrity assurances, and — depending on payload semantics on the overlay network — can result in duplicate delivery/replay of application traffic. Note this affects only already-established, authenticated tunnels (post-handshake, valid peer with a valid certificate); it does not bypass certificate/CA verification itself, but it does defeat one of the concrete listed impact categories (traffic replay).

### Likelihood Explanation
The race window requires the attacker to get two decrypt attempts for the same counter processed concurrently (or a duplicated inbound delivery) before the first `Update` call executes. This is plausible on multi-routine listener configurations or simply via duplicate delivery at the UDP/network layer (a passive network attacker can duplicate a captured UDP datagram trivially), but the window between `Check` and `Update` is narrow (bounded by one `DecryptDanger` call), making exploitation timing-sensitive rather than trivially deterministic. I was not able to fully confirm from the available index whether `readOutsidePackets` is invoked concurrently from multiple goroutines for the same socket/hostinfo in the current build (the reader-routine setup in `main.go`/`interface.go` was only partially visible), so likelihood should be treated as moderate rather than confirmed-high pending that check.

### Recommendation
Make the replay-window check-and-mark atomic under a single critical section spanning both `Check` and `Update` (i.e., merge them into one locked "check-and-set" operation, or hold `decryptLock` across the entire `Check` → `Decrypt` → `Update` sequence, or perform `Update` first speculatively and roll back only on decrypt failure) so that a given `messageCounter` can never be evaluated as "not yet seen" by two concurrent callers.

### Proof of Concept
Conceptual PoC (not executed): 
1. Establish a Nebula tunnel between two peers.
2. Capture one legitimate outside-facing UDP packet carrying a `header.Message` payload.
3. Immediately re-inject (duplicate) that exact packet at the network layer so it is processed by `readOutsidePackets` concurrently with, or very shortly after, the original.
4. Because `ConnectionState.Decrypt` releases `decryptLock` between `window.Check` and `window.Update`, both invocations can observe `Check == true` before either commits via `Update`, allowing the duplicate to be decrypted and delivered to the tun device a second time instead of being rejected with `ErrAlreadySeen`. [1](#0-0)

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
