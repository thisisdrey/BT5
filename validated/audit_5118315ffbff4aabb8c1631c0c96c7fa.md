### Title
Replay window `Check`-then-`Update` race in `ConnectionState.Decrypt` allows duplicate-packet processing (analog of ERC777 `depositCap` reentrancy bypass) - (File: `connection_state.go`)

### Summary
The Backd finding shows a check-then-effect vulnerability: `depositFor` checks `depositCap` against pre-reentrancy state, then a reentrant call slips through before the deposit is recorded, letting the guard be bypassed twice. Nebula's anti-replay guard has the same shape: `ConnectionState.Decrypt` checks the replay window, releases the lock to do the (comparatively slow) AEAD decrypt, and only afterward re-acquires the lock to mark the counter as consumed. Between the check and the mark, a duplicate/replayed packet with the same message counter can pass the same check, because the window has not yet been updated.

### Finding Description
`ConnectionState.Decrypt` performs the replay check and the window update as two separate, non-atomic critical sections: [1](#0-0) 

Between `cs.window.Check(l, messageCounter)` (which only reads the bitmap) and `cs.window.Update(l, messageCounter)` (which marks it seen), the lock is released for the duration of `cs.dKey.DecryptDanger(...)`. If a second copy of the same UDP datagram (same message counter) arrives and is processed concurrently — which is architecturally possible because Nebula spawns one reader goroutine per configured `routines`/queue, each independently calling into `readOutsidePackets` → `Decrypt` for packets it receives — that second call's `Check` also passes, since `Update` for the first copy has not run yet: [2](#0-1) [3](#0-2) [4](#0-3) 

This is directly analogous to the ERC777 issue: the guard (`depositCap` / replay window) is evaluated against state that has not yet reflected the in-flight operation, and the state-mutating step happens only after the slow/reentrant-prone operation completes, leaving a window where the same input can pass the guard more than once.

The same check-then-update pattern, with the same lock-release gap, also exists in `VerifyRelay`, used for relay-forwarded frames: [5](#0-4) 

The underlying `Bits.Check`/`Bits.Update` primitives themselves are correct in isolation — the bug is that the caller does not hold a single lock across the read-check and the write-mark, over an operation (AEAD decrypt) that has non-trivial duration relative to how quickly two duplicate UDP datagrams can be delivered to two different reader queues.

### Impact Explanation
An attacker who can duplicate an already-observed ciphertext UDP packet (a classic on-path/network replay, not requiring any valid CA-signed certificate or handshake participation) can cause the same message counter to pass the replay `Check` twice, so the payload is decrypted and handed to `handleOutsideMessagePacket`/`lhf.HandleRequest`/etc. twice. This defeats the intended nonce/replay protection that the `ConnectionState` and its `Bits` window are meant to enforce, an explicit protected category (nonce/replay handling) under this reachable-without-signed-cert scope. Depending on downstream handling this can lead to duplicate application of tunnel-level state changes (e.g., duplicated LightHouse updates, duplicated relay control messages) — i.e. remote state poisoning through replay.

### Likelihood Explanation
Exploitability depends on the race window being hit: the attacker needs `f.routines > 1` (multi-queue reading is enabled and supported by the platform/socket) so that two identical UDP datagrams can land on two different reader goroutines concurrently, or otherwise needs to win a tight race on a single routine. This is a narrower window than the ERC777 case (which is deterministically triggerable via a hook), so likelihood is moderate rather than certain, but it is a real, network-reachable race with no certificate/identity prerequisite — the attacker only needs to see and duplicate one ciphertext packet.

### Recommendation
Hold `decryptLock` for the entire check-decrypt-update sequence in both `Decrypt` and `VerifyRelay`, so the replay window's read-then-write is atomic with respect to the decrypt operation, e.g.:
```go
cs.decryptLock.Lock()
defer cs.decryptLock.Unlock()
if !cs.window.Check(l, messageCounter) {
    return nil, ErrAlreadySeen
}
out, err = cs.dKey.DecryptDanger(...)
if err != nil {
    return nil, err
}
if !cs.window.Update(l, messageCounter) {
    return nil, ErrAlreadySeen
}
return out, nil
```
This removes the check/update gap during which duplicate counters can both pass, matching the "add reentrancy guard" style fix recommended in the referenced report (serialize the guard check and the state mutation into one atomic unit).

### Proof of Concept
1. Establish a tunnel between two Nebula nodes with `listen.routines` (or platform default) `> 1` so multiple UDP reader queues are active, as constructed in `f.run()`/`f.listenOut`: [2](#0-1) .
2. Capture one legitimate encrypted `header.Message` UDP datagram sent to the victim node (any network position capable of observing/duplicating UDP traffic can do this; no valid certificate is required since the payload is just being replayed, not forged).
3. Send two copies of the exact same datagram to the victim in quick succession so kernel-level UDP fan-out/SO_REUSEPORT (or NIC/queue affinity used by `SupportsMultipleReaders`) delivers them to two different reader goroutines.
4. Because `ConnectionState.Decrypt` releases `decryptLock` between `Check` and `Update` [1](#0-0) , both goroutines can pass `Check` for the same `messageCounter` before either calls `Update`, causing the payload to be decrypted and processed twice instead of the second copy being rejected with `ErrAlreadySeen`.

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

**File:** interface.go (L309-337)
```go
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
