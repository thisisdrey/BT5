### Title
Check-then-act race in anti-replay window allows a duplicate/replayed packet to be decrypted twice under concurrent readers - (File: connection_state.go)

### Summary
`ConnectionState.Decrypt` (and `VerifyRelay`) validate a packet's counter against the anti-replay window with `window.Check`, release the lock, perform the (comparatively expensive) AEAD decryption, and only then re-acquire the lock to call `window.Update`, which actually marks the counter as consumed. Because `SupportsMultipleReaders()` on Linux returns `true` and `Interface.run()` launches `f.routines` concurrent `listenOut` goroutines that all funnel into `readOutsidePackets` → `ConnectionState.Decrypt` for the same `HostInfo`, two goroutines can both see the counter as "unseen" during their respective `Check` calls before either has executed `Update`. This is structurally the same bug class as the reported `addIncentive` issue: a permissionless, state-mutating operation whose authorization/validity check ("is this rate/counter allowed?") is decoupled in time from the state update that would prevent a second party from exploiting the same window, enabling a front-run/race between the check and the commit.

### Finding Description
`window.Check` is a read-only test ("have we already recorded counter i?"), and `window.Update` is the mutation that actually records it. In `Decrypt`: [1](#0-0) 

the lock is dropped between `Check` and `Update`, with `DecryptDanger` executing unlocked in between. `VerifyRelay` has the identical pattern: [2](#0-1) 

An attacker who can deliver two copies of the same on-wire ciphertext (a legitimate duplicate, a replay, or a race with itself) to a Nebula node running with `listen.routines > 1` can have both copies dispatched to different reader goroutines in `listenOut`/`readOutsidePackets`: [3](#0-2) [4](#0-3) 

Both goroutines call `Decrypt` for the same `hostinfo.ConnectionState` concurrently. Goroutine A takes the lock, `Check` reports "not seen," releases the lock. Before A calls `Update`, goroutine B takes the lock, also calls `Check` on the same counter, and also gets "not seen" because A has not yet recorded it. Both proceed to `DecryptDanger` and, on success, both call `Update`, which will accept the first `Update` and (per `Bits.updateSlow`) reject the second as a duplicate — but by then both copies have already been decrypted and both callers proceed to hand the packet up to `handleOutsideMessagePacket`/roaming/etc. The replay-window state (`Bits.current`/bitmap) is only a defense if `Check` and `Update` are atomic with respect to each other for a given counter; here they are not.

This directly parallels the report's root cause: `addIncentive`'s vulnerable branch reads `amountRemaining`/`incentiveRate`, and only conditionally commits `incentive.incentiveRate = incentiveRate;` afterward, letting a second permissionless caller's transaction land in between the read and the commit and corrupt the assumption the first caller relied on. Here, `Check` (read) and `Update` (commit) around a socket-level decrypt operation have the same gap.

`Bits.strictlyWithinWindow`/`Update`'s slow path do show a duplicate-detection final gate, but that gate only protects the *second* `Update` call from re-marking the bit — it does not prevent both packets from being decrypted and delivered to the application/data path, since delivery has already happened by the time `Update` runs: [5](#0-4) [6](#0-5) 

### Impact Explanation
This is an anti-replay bypass reachable without holding a valid CA-signed certificate for the attacker's own identity — the attacker only needs to be able to send (or capture-and-resend) a single valid ciphertext frame from a legitimate, already-established tunnel to the target's UDP listener while `listen.routines > 1` (Linux, `SupportsMultipleReaders()==true`). Successfully racing the window doesn't forge new plaintext, but it defeats the very mechanism (`Bits`/`ReplayWindow`) whose stated purpose is anti-replay protection, allowing a captured packet to be processed by the application/data path a second time (e.g., a duplicate tun-delivered application packet, or duplicate handling of control-plane message types such as `header.LightHouse`, `header.Test`, `header.Control`, `header.CloseTunnel` if the race is won against those subtypes). This is a concrete replay-handling correctness/security failure in the exact category the rules call out ("nonce/replay handling").

### Likelihood Explanation
Exploitability depends on winning a narrow race between two UDP reader goroutines processing the same duplicated ciphertext for the same `ConnectionState`, which requires: (1) `listen.routines` configured >1 (a supported, documented configuration on Linux, not test-only), (2) the attacker being able to deliver two copies of the identical wire packet to different kernel-delivered reader queues close together in time (achievable via raw duplicate UDP sends, or intercepting/replaying a captured packet in a LAN/on-path position). This is a genuine remote race condition, not a theoretical one — the lock is explicitly released between the check and the commit, so the window for the race is bounded only by the AEAD decrypt time, and an attacker fully controls the timing of the duplicate send.

### Recommendation
Make replay-window validation and consumption atomic with respect to a given counter: hold `decryptLock` for the full duration of `Check` → `DecryptDanger` → `Update`, or use a single combined "reserve-then-decrypt-then-confirm/rollback" primitive so no other goroutine can observe the counter as unconsumed while a decrypt for it is already in flight. At minimum, perform the window `Update` (or an equivalent "claim" operation) before releasing the lock the first time, treating a failed decrypt as a case to roll back the claim rather than leaving the counter unclaimed during the decrypt.

### Proof of Concept
1. Establish a Nebula tunnel between two hosts, with the receiving node configured with `listen.routines: 2` (or more) so `SupportsMultipleReaders()` is true and multiple `listenOut` goroutines run per `interface.go`'s `run()`.
2. Capture one legitimate encrypted `header.Message`/`MessageNone` packet on the wire (e.g., via a MITM/tap position, or by controlling a routing hop).
3. Re-inject (send) the exact same ciphertext bytes to the receiver's UDP port twice, back-to-back, so the kernel/socket layer is likely to dispatch them to two different reader goroutines (`recvmmsg`/multiple readers per `udp/udp_linux.go`).
4. Both goroutines invoke `f.readOutsidePackets` → `hostinfo.ConnectionState.Decrypt` concurrently for the same `HostInfo`. Because `Check` and `Update` are not atomic (`connection_state.go:61-82`), both may pass `Check` before either calls `Update`, causing the duplicated packet to be decrypted and delivered to `handleOutsideMessagePacket` (or other message handlers) twice, despite the anti-replay window's intended single-delivery guarantee.

Note: I was not able to fully verify from static reading alone whether downstream idempotency (e.g., TCP/IP stack semantics on the tun device, or connection-manager state) would mask the user-visible effect of a duplicate delivery for the `MessageNone` data path in all cases; the control-plane subtypes (`LightHouse`, `Test`, `Control`, `CloseTunnel`) are more directly observable as duplicated actions. A background Devin session with build/test tooling would be needed to write a concurrency reproduction test (e.g., extending `bits_test.go`/`connection_state_test.go`) to confirm the race deterministically.

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

**File:** interface.go (L273-288)
```go
func (f *Interface) run() {
	// Launch n queues to read packets from udp
	for i := 0; i < f.routines; i++ {
		f.wg.Go(func() {
			f.listenOut(i)
		})
	}

	// Launch n queues to read packets from tun dev
	for i := 0; i < f.routines; i++ {
		f.wg.Go(func() {
			f.listenIn(f.readers[i], i)
		})
	}

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

**File:** bits.go (L188-250)
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

	// If i is within the current window but below the current counter, check to see if it's a duplicate
	if b.strictlyWithinWindow(i) {
		pos := i & b.lengthMask
		word := pos >> 6
		mask := uint64(1) << (pos & 63)
		w := b.bits[word]
		if b.current == i || w&mask != 0 {
			if l.Enabled(context.Background(), slog.LevelDebug) {
				l.Debug("Receive window",
					"accepted", false,
					"currentCounter", b.current,
					"incomingCounter", i,
					"reason", "duplicate",
				)
			}
			b.dupeCounter.Inc(1)
			return false
		}

		b.bits[word] = w | mask
		return true
	}
```
