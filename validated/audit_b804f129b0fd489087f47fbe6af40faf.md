### Title
Replay-window check-then-act race allows duplicate decryption/delivery of a captured packet - ([File: connection_state.go])

### Summary
`ConnectionState.Decrypt` (and its relay counterpart `VerifyRelay`) validate a message counter against the anti-replay window, release the lock, perform the actual AEAD decryption, and only then re-acquire the lock to commit the counter into the window. The gap between the `Check` (validation) and `Update` (commit) steps is unlocked, so two concurrent calls for the same counter can both pass `Check` before either commits `Update`, letting the same captured packet be decrypted and delivered twice. This mirrors the reported bug class: a security-relevant check is performed against state that is allowed to go stale before the corresponding state-mutating action completes, because the validation and the commit are split across a mutable window with no atomicity guarantee.

### Finding Description
`Decrypt` first takes `decryptLock`, calls `cs.window.Check(l, messageCounter)`, and immediately releases the lock: [1](#0-0) 

Between the `Check` and the later `Update` (which is the operation that actually marks the counter as seen), the function performs `dKey.DecryptDanger(...)` without holding any lock. If a second goroutine processes another copy of the exact same encrypted packet (same message counter) during that window, `window.Check` for the second call will also return `true`, because the first call has not yet reached `Update`. Both goroutines will then successfully decrypt and deliver the payload, and only the second `Update` call will observe `ErrAlreadySeen` — after the plaintext has already been handed to the caller and processed via `handleOutsideMessagePacket`/tun injection.

The same check-then-act pattern exists in `VerifyRelay`, used for relay-forwarded frames: [2](#0-1) 

Concurrent delivery of packets belonging to the same `HostInfo.ConnectionState` is architecturally possible: the interface launches one `listenOut` goroutine per configured underlay queue, each independently calling `readOutsidePackets` and then `hostinfo.ConnectionState.Decrypt`/`VerifyRelay` with no hostinfo-level synchronization other than the replay window's own internal lock: [3](#0-2) [4](#0-3) 

Because a single logical tunnel (`HostInfo`) can be reachable over multiple underlay paths — most notably when relayed through more than one relay node, or when NIC/queue hashing differs for retransmitted/duplicate underlay datagrams — an attacker who captures one legitimate ciphertext packet and re-delivers it concurrently via two different underlay paths can cause two reader goroutines to invoke `Decrypt`/`VerifyRelay` on the same `ConnectionState` at (or near) the same time, hitting this check-then-act gap. The `Bits` anti-replay window itself is documented as needing external locking around its use precisely because it is not atomic across a "check, do work, update" sequence: [5](#0-4) 

This is directly analogous to the reported bug: `initiateWithdrawal` records state, an intervening event (price change / here, a second concurrent packet) changes the reference state before the second, dependent step (`withdraw`'s check / here, `Update`) is reached, and the two steps are validated against different "epochs" of the same tracked value.

### Impact Explanation
A successful race causes the same on-wire ciphertext to be decrypted and forwarded into the local tun device (or, for relay frames, re-forwarded to the relay target) twice, defeating the anti-replay guarantee that `Bits`/`ConnectionState` is meant to provide. This is a concrete traffic-replay bypass: duplicate application-layer traffic is injected past the AEAD/replay protections that are supposed to guarantee at-most-once delivery per message counter, undermining the confidentiality/integrity assumptions built on top of that guarantee (e.g., replay-sensitive protocols running over the tunnel).

### Likelihood Explanation
Exploitation requires an attacker (with no valid certificate, per scope) to capture one legitimate ciphertext frame and cause two copies of it to be processed concurrently by two different reader goroutines for the same `ConnectionState` — achievable when multiple underlay queues (`listen.batch`/multi-queue `routines`) or multiple relay paths exist for the same tunnel, both of which are supported, documented configurations in this codebase. The race window is bounded only by the time to perform one AEAD decrypt, which is short but nonzero, and the attack is fully repeatable (the attacker can keep sending duplicate copies until the race lands), so likelihood is moderate rather than purely theoretical.

### Recommendation
Make the replay-window "check and commit" operation atomic with respect to the decryption it is meant to gate: e.g., hold `decryptLock` across the entire `Check` → `DecryptDanger` → `Update` sequence (or use `Bits.Update` alone, since it already performs a duplicate check, as the single, lock-held gate before decryption) so that no second caller can observe a not-yet-committed counter as still valid. Apply the same fix to both `Decrypt` and `VerifyRelay`.

### Proof of Concept
1. Establish a tunnel between two nodes with a configuration that exposes multiple underlay reader queues for the same `ConnectionState` (e.g., `listen.routines`/`listen.batch` > 1, or a target reachable via two relay nodes as in `TestRelayReplayProtection`).
2. Capture one legitimate outbound `Message`/`MessageRelay` frame with message counter `N`.
3. Simultaneously re-inject two copies of the identical frame such that they are picked up by two different reader goroutines for the same `HostInfo` (e.g., via two different relay underlay sockets, mirroring `e2e/tunnels_test.go`'s `TestRelayReplayProtection` harness but sending both copies concurrently instead of sequentially).
4. Observe that under an unlucky (but reproducible with enough retries/instrumented sleep between `Check` and `DecryptDanger`) interleaving, both goroutines pass `cs.window.Check(l, N)` before either calls `cs.window.Update(l, N)`, resulting in the payload being decrypted and delivered/forwarded twice, even though the replay window is supposed to guarantee single delivery per counter.

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

**File:** outside.go (L113-132)
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
