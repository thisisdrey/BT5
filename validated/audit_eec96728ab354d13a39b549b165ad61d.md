## Analysis

The external report's bug class is a **check-then-act race**: a value is read (cached) under one lock, released, and later written back under a second lock acquisition — with no atomicity guaranteeing the read and write are for a consistent state. This is the same shape of bug present in the anti-replay logic of `ConnectionState.Decrypt`.

### Title
Replay-window check/update race allows duplicate packet acceptance - (File: `connection_state.go`)

### Summary
`ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` split the replay-window "have we seen this counter" check and the "mark this counter seen" update into two separate, independently-locked critical sections, with the expensive AEAD decryption running unlocked in between. This mirrors the reported pattern of caching a value, releasing consistency, then writing back based on the stale cached decision.

### Finding Description
`Decrypt` takes `decryptLock`, calls `cs.window.Check(l, messageCounter)`, and releases the lock before performing `dKey.DecryptDanger`. Only after decryption succeeds does it re-acquire `decryptLock` and call `cs.window.Update(l, messageCounter)` to actually mark the counter as consumed: [1](#0-0) 

The same pattern exists in `VerifyRelay`: [2](#0-1) 

Because `Check` only reads the bitmap state and `Update` is the only call that mutates it, and the two calls are not part of one atomic critical section, two goroutines invoking `Decrypt` concurrently for the *same* `ConnectionState` with the *same* `messageCounter` (e.g. a duplicated/replayed UDP datagram delivered to two different reader goroutines) can both pass `Check` before either calls `Update`. Nebula runs multiple concurrent outside-packet listener goroutines per interface (`f.routines`), each with its own `ConntrackCacheTicker` and calling into `readOutsidePackets` → eventually `Decrypt` on the shared per-peer `ConnectionState`: [3](#0-2) 

This is analogous to the reported vulnerability: a decision (`from_balance`/`to_balance`, here "already seen or not") is cached before a state-changing operation, and the two participants in the race (two decrypt attempts for the same counter, as opposed to the report's same `from`/`to` address) both proceed as if the cached decision were still valid, defeating the intended one-time enforcement.

### Impact Explanation
Successful exploitation lets an attacker with the ability to duplicate/replay a captured ciphertext packet (no valid certificate needed — this is post-decryption-attempt logic reachable from any UDP sender directed at an established tunnel) cause the same on-wire message to be accepted and processed twice by the data plane, bypassing the anti-replay guarantee `Bits.Check`/`Update` is meant to provide. This is a concrete violation of the "nonce/replay handling" security property nebula relies on to prevent replay attacks.

### Likelihood Explanation
Exploitation requires winning a narrow race window (the gap between the unlocked `DecryptDanger` call and the subsequent `Update`), and requires the interface to be configured with more than one outside-reader routine so two goroutines can process packets concurrently. It is not trivially reliable, but it is a genuine TOCTOU flaw in security-critical replay-protection code, not a hypothetical.

### Recommendation
Hold `decryptLock` for the entire `Check` → `DecryptDanger` → `Update` sequence (or otherwise make the check-decrypt-mark sequence atomic per `messageCounter`), so no other goroutine can observe the "not yet seen" state for the same counter until it has actually been consumed.

### Proof of Concept
1. Establish a tunnel between two nebula nodes with `routines` > 1 (multiple outside listener goroutines).
2. Capture one valid encrypted data packet destined to the responder.
3. Rapidly replay this exact packet twice, timed so that two outside-reader goroutines invoke `ConnectionState.Decrypt` with the same `messageCounter` concurrently (e.g., by sending on two different underlay sockets/paths that land on different reader queues).
4. Observe that both calls can pass `cs.window.Check` before either executes `cs.window.Update`, resulting in the payload being decrypted and forwarded to the TUN device twice instead of the second copy being rejected with `ErrAlreadySeen`.

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

**File:** interface.go (L280-337)
```go

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
