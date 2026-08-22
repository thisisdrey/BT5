### Title
Anti-replay window check-then-decrypt-then-update race allows duplicate packet delivery/replay bypass - (File: connection_state.go)

### Summary
`ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` in `connection_state.go` split the anti-replay window's "check" and "commit" operations across two separately-locked critical sections, with the actual AEAD decrypt/verify performed *outside* the lock in between. When multiple UDP reader routines (`f.routines > 1`, `listenOut`) process packets concurrently for the same tunnel, this creates a TOCTOU race that lets a duplicated wire packet be decrypted and delivered twice, defeating the purpose of the replay window.

### Finding Description
`Decrypt` takes `decryptLock`, calls `cs.window.Check(l, messageCounter)`, and releases the lock before doing the actual AEAD decryption: [1](#0-0) 

The same check-unlock-decrypt-lock-update pattern is repeated in `VerifyRelay`: [2](#0-1) 

`Bits.Check` (used for the pre-decrypt check) only reads window state; `Bits.Update` (used for the post-decrypt commit) is the operation that actually marks a counter as seen: [3](#0-2) [4](#0-3) 

Nebula runs multiple concurrent UDP reader goroutines when `listen.routines > 1` is configured, each independently invoking the outside-packet path (and therefore `ConnectionState.Decrypt`) for packets that land on their queue: [5](#0-4) 

Because `decryptLock` is released between `Check` and the actual `DecryptDanger` call, two copies of the same on-wire packet (message counter `i`) arriving on different reader routines at nearly the same time can both pass `Check(i)` (since neither has called `Update(i)` yet), both successfully decrypt (the AEAD tag is valid for a duplicate of a legitimately-sent packet), and only afterward race to call `Update(i)` — one succeeds, one gets `ErrAlreadySeen`, but by that point the decrypted plaintext of the duplicate has already been produced and returned to the caller for delivery (e.g., written to the tun device).

### Impact Explanation
This is a replay-handling failure: the anti-replay window is explicitly designed so a given message counter is processed (delivered) at most once, but the split lock windows around `Check`/decrypt/`Update` allow a duplicated packet to be decrypted and handed to the data path twice under concurrent reader routines. An attacker with only network-level access to duplicate/relay observed ciphertext frames (no valid certificate needed — they never need to decrypt or forge anything, only re-inject an already-captured packet) can exploit normal network jitter or intentionally duplicate a captured UDP frame to increase the odds of hitting this race window, causing duplicate delivery of encrypted application traffic. This falls squarely in the "traffic decryption/forgery/replay" and "nonce/replay handling" categories called out as in-scope. The severity is bounded because it does not break confidentiality/integrity of new content, but it does defeat the explicit anti-replay guarantee under the multi-routine configuration, and duplicate delivery of tunneled packets can have downstream effects on TCP semantics, health checks, or other systems that assume exactly-once processing of a given segment.

### Likelihood Explanation
Exploitability requires: (1) `listen.routines` configured `> 1` (a supported/documented setting, not a test-only path), and (2) an attacker able to duplicate an already-captured wire packet with tight timing so it lands on two different reader queues before the first `Update` commits. This is a real, network-observable-only precondition (no valid CA-signed cert or key material needed to trigger the race — only the ability to capture and re-send a UDP datagram), making it a genuine race condition rather than a purely theoretical one, though the timing window is narrow (the entire journey between `Check` and `Update`, including a full AEAD decrypt) which lowers likelihood somewhat compared to a trivially-won race.

### Recommendation
Hold `decryptLock` (or an equivalent per-connection critical section) across the entire check-decrypt-update sequence so that `Check`, `DecryptDanger`/verify, and `Update` for a given `ConnectionState` execute atomically with respect to other packets on the same tunnel. Alternatively, restructure `Bits` to expose a single atomic `CheckAndReserve`/`CheckAndUpdate` operation that is called once, before decryption, and reserves the slot pessimistically, decrementing/backing-out only on decrypt failure — ensuring no two decrypt operations for the same counter can ever both proceed.

### Proof of Concept
1. Configure a nebula node with `listen.routines: 2` (or more) so `f.routines > 1` and multiple `listenOut` goroutines run concurrently, each handling packets read from a `SO_REUSEPORT`-style socket set.
2. Establish a tunnel between two nodes and capture one legitimate encrypted data-plane UDP frame (with message counter `i`) sent from peer A to peer B.
3. From an attacker position capable of duplicating on-wire UDP traffic (e.g., a MITM or someone with access to the underlay network), inject two copies of the exact same captured frame nearly simultaneously so the OS/kernel dispatches them onto two different reader queues on B (achievable by sending in quick succession, relying on socket-level load balancing across the `routines` readers).
4. On B, both `listenOut` goroutines call `readOutsidePackets` → `ConnectionState.Decrypt` concurrently for counter `i`. Because `cs.window.Check` and `cs.window.Update` are not held under one lock spanning the decrypt call (`connection_state.go` lines 61-82), both goroutines can pass the initial `Check`, both perform `DecryptDanger` successfully, and both attempt delivery of the decrypted payload up the stack — only the second `Update` call returns `ErrAlreadySeen`, after the plaintext has already been produced once (in the race window, potentially delivered twice depending on where in the caller the `ErrAlreadySeen` check occurs relative to already-in-flight processing).
5. Observe (via added instrumentation/logging or by monitoring the tun device on B) that the same application-layer segment associated with counter `i` is processed/delivered more than once, demonstrating that the anti-replay window's atomicity guarantee is broken under the multi-routine configuration.

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

**File:** bits.go (L120-150)
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

**File:** bits.go (L186-227)
```go
}

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

**File:** interface.go (L243-337)
```go
	if f.routines > 1 {
		if !f.inside.SupportsMultiqueue() || !f.outside.SupportsMultipleReaders() {
			f.routines = 1
			f.l.Warn("routines is not supported on this platform, falling back to a single routine")
		}
	}

	metrics.GetOrRegisterGauge("routines", nil).Update(int64(f.routines))

	// Prepare n tun queues
	var reader io.ReadWriteCloser = f.inside
	for i := 0; i < f.routines; i++ {
		if i > 0 {
			reader, err = f.inside.NewMultiQueueReader()
			if err != nil {
				return err
			}
		}
		f.readers[i] = reader
	}

	// On error the caller owns the cleanup, Control.Start cancels the service context
	// before releasing our resources so a waiter never observes a live context
	if err = f.inside.Activate(); err != nil {
		return err
	}

	return nil
}

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
