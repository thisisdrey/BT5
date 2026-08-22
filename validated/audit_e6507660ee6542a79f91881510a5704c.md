### Title
Replay-window check/update race allows a captured packet to be processed twice before being marked seen - ([File: connection_state.go])

### Summary
`ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` in `connection_state.go` implement anti-replay protection with a check-then-act pattern split across two separately-locked critical sections, with the (unlocked) AEAD decrypt/authentication operation happening in between. This mirrors the governance bug class: a decision ("this packet counter has not been seen yet") is made and acted upon, but the record that finalizes/locks that decision (`window.Update`) is deferred to a second, separately-acquired lock, leaving a window where the same "vote" (packet) can be accepted twice.

### Finding Description
`Decrypt` is implemented as: [1](#0-0) 

```
cs.decryptLock.Lock()
result := cs.window.Check(l, messageCounter)
cs.decryptLock.Unlock()
if !result { return nil, ErrAlreadySeen }

out, err = cs.dKey.DecryptDanger(...)   // <-- lock released here
...

cs.decryptLock.Lock()
result = cs.window.Update(l, messageCounter)
cs.decryptLock.Unlock()
if !result { return nil, ErrAlreadySeen }
return out, nil
```

`VerifyRelay` has the identical structure: [2](#0-1) 

Nebula runs multiple concurrent UDP-reader goroutines (`f.routines`), each independently invoking `readOutsidePackets` → `hostinfo.ConnectionState.Decrypt` / `VerifyRelay` for the same `HostInfo`/`ConnectionState`: [3](#0-2) [4](#0-3) 

`Bits.Check` only inspects whether a counter slot is set without mutating state, and only `Bits.Update` marks it seen: [5](#0-4) 

Because `Check` and `Update` are each protected by their own lock/unlock pair rather than one lock held across the whole "check, decrypt, mark" sequence, two threads that receive the identical UDP packet (or an attacker-replayed copy of a captured packet, delivered to two different reader queues, e.g. via kernel UDP fan-out/multiqueue or simple duplicate delivery) can both pass `Check` before either calls `Update`. Both then independently perform a successful AEAD decrypt of the same ciphertext (AEAD decryption is deterministic and does not itself detect reuse) and both proceed to hand the resulting plaintext to `handleOutsideMessagePacket`/lighthouse/control handling as if it were two distinct, freshly-authenticated messages. Only after both decrypts finish does one of the two `Update` calls "win" the race; by then the duplicate has already been treated as a valid message.

The changelog entry "Lock replay window updates so concurrent readers can't corrupt it. (#1802)" shows the developers previously hardened `Bits` against data races on the underlying bitmap, but that fix only serializes access to the bitmap itself — it does not close the logical gap between "we determined this counter is unseen" and "we recorded it as seen," because the AEAD decrypt (the expensive, blocking operation) runs outside any lock between those two steps.

### Impact Explanation
This allows a captured Nebula application/data-plane message (or relay frame) to be re-delivered and processed a second time by the target's decrypt/dispatch pipeline even though nebula's design intends the sliding-window `Bits` structure to guarantee each message counter is accepted exactly once. Depending on the message type this manifests as: duplicate application traffic delivered to the tun device, duplicate LightHouse/Test/Control message processing, or (per the relay case) potential duplicate relay forwarding — i.e., a traffic-replay class issue in the encrypted data plane, which the project explicitly treats as security-relevant (see the `TestRelayReplayProtection` regression test and its associated fix).

### Likelihood Explanation
Exploitation requires an attacker capable of causing the same ciphertext to be delivered twice to two independent reader goroutines before the first `Update` call completes — achievable by a network-position/on-path attacker duplicating a captured legitimate UDP packet (a classic replay primitive that does not require possessing a valid CA-signed certificate), particularly when `listen.batch`/multi-routine reading (`routines > 1`) is enabled, which nebula supports and documents. The race window is bounded by the time to run one AEAD `Open`, but on a loaded multi-core host with `routines > 1` this window is realistically hittable, especially by an attacker who can send many duplicate copies in a short burst to increase the chance of straddling two reader routines.

### Recommendation
Hold `cs.decryptLock` for the entire check-decrypt-mark sequence in both `Decrypt` and `VerifyRelay` (or otherwise make counter reservation atomic with the check), e.g. call `window.Check`, and only release the lock after `window.Update` has been called following a successful decrypt, or reserve the slot atomically before decrypting and roll it back on decrypt failure, so no other goroutine can observe the counter as "unseen" while a decrypt for that same counter is in flight.

### Proof of Concept
1. Establish a tunnel between two nebula instances configured with `routines > 1` (or on a platform/socket that supports multiple UDP readers, `SupportsMultipleReaders()==true`).
2. Capture one legitimate encrypted data-plane (or relay) packet sent from A to B.
3. Rapidly re-inject two (or more) copies of that exact captured packet toward B at the same instant, timed so the OS/multiqueue socket delivers them to two different reader goroutines (`listenOut(i)` instances) concurrently.
4. Because `Check` for both goroutines can pass before either has called `Update` (the AEAD decrypt runs with the lock released), both goroutines successfully decrypt and dispatch the same message — resulting in the payload being delivered to the tun device (or relay-forwarded) more than once, despite the sliding-window replay protection.

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

**File:** interface.go (L273-337)
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

func (f *Interface) wait() error {
	f.wg.Wait()
	if e := f.fatalErr.Load(); e != nil {
		return *e
	}
	return nil
}

// onFatal stores the first fatal reader error, and calls triggerShutdown if it was the first one
func (f *Interface) onFatal(err error) {
	swapped := f.fatalErr.CompareAndSwap(nil, &err)
	if !swapped {
		return
	}
	if f.triggerShutdown != nil {
		f.triggerShutdown()
	}
}

func (f *Interface) listenOut(i int) {
	var li udp.Conn
	if i > 0 {
		li = f.writers[i]
	} else {
		li = f.outside
	}

	ctCache := firewall.NewConntrackCacheTicker(f.ctx, f.l, f.conntrackCacheTimeout)
	lhh := f.lightHouse.NewRequestHandler()
	plaintext := make([]byte, udp.MTU)
	h := &header.H{}
	fwPacket := &firewall.Packet{}
	nb := make([]byte, 12, 12)

	err := li.ListenOut(func(fromUdpAddr netip.AddrPort, payload []byte) {
		f.readOutsidePackets(ViaSender{UdpAddr: fromUdpAddr}, plaintext[:0], payload, h, fwPacket, lhh, nb, i, ctCache.Get())
	})

	// An error after teardown began is shutdown noise, the closed flag covers resources
	// Close releases itself and the cancelled ctx covers ones torn down by their owners
	// reacting to it, like the user device pipes
	if err != nil && !f.closed.Load() && f.ctx.Err() == nil {
		f.l.Error("Error while reading inbound packet, closing", "error", err)
		f.onFatal(err)
	}

	f.l.Debug("underlay reader is done", "reader", i)
}
```

**File:** outside.go (L105-132)
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
```

**File:** bits.go (L150-186)
```go
}

// Update has three branches:
//   - i == b.current+1: fast path; advance the cursor by one and lose-count
//     the slot we just stomped (only past warmup; see the i > b.length guard
//     below).
//   - i  >  b.current+1: jump path; clear all slots between current and i
//     (or up to a full window's worth, whichever is smaller) via clearRange,
//     then mark i. Two arms here: a warmup arm that handles the very first
//     window before the cursor has slid, and a steady-state arm that treats
//     every cleared empty slot as a lost packet.
//   - i  <= b.current: in-window check for duplicates; out-of-window otherwise.
//
// NewBits seeds bits[0]=1 so counter 0 looks "received" — Update never
// clears that marker during warmup (clearRange skips position 0 when
// startPos=1), and once b.current >= b.length the marker is no longer
// consulted. The marker prevents a fictitious "lost" hit on the first real
// counter.
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
