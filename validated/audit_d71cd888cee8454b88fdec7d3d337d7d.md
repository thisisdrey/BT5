### Title
Anti-replay window check-before-decrypt TOCTOU allows replayed ciphertext to bypass the replay filter - ([File: connection_state.go])

### Summary
`ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` in `connection_state.go` check the anti-replay window (`window.Check`), release the lock, perform the AEAD decryption/verification *unlocked*, and only mark the counter as seen (`window.Update`) afterward. This mirrors the root cause of the referenced Sherlock finding: a security-critical check ("has this counter already been consumed?") is validated before the state-changing action instead of atomically with it, letting concurrent requests slip past the guard before the guard's own bookkeeping catches up.

### Finding Description
`Decrypt` does:
```go
cs.decryptLock.Lock()
result := cs.window.Check(l, messageCounter)   // "not yet seen"
cs.decryptLock.Unlock()
...
out, err = cs.dKey.DecryptDanger(...)          // unlocked AEAD work
...
cs.decryptLock.Lock()
result = cs.window.Update(l, messageCounter)   // marks seen, AFTER decrypt
cs.decryptLock.Unlock()
``` [1](#0-0) 

`VerifyRelay` has the identical structure for relay frames. [2](#0-1) 

Nebula runs one independent reader goroutine per configured `routines` queue, each with its own UDP socket bound via `SO_REUSEPORT`, and each goroutine calls into the shared per-peer `ConnectionState` through `readOutsidePackets` → `Decrypt`: [3](#0-2) [4](#0-3) 

Because the lock is released between `Check` and the actual decrypt, and re-acquired only for `Update`, two goroutines that receive the *same* previously-observed ciphertext (i.e., a replayed packet) concurrently can both pass `window.Check` — since neither has called `Update` yet — both successfully run `DecryptDanger`, and both return plaintext to the caller. The lookup of the destination `ConnectionState`/`HostInfo` is keyed by the wire `RemoteIndex` in the packet header, not by the receiving socket, so an attacker who has observed one ciphertext packet (RemoteIndex and counter are visible on the wire, not secret) can resend it through source ports that the kernel's per-flow hash routes to a different `SO_REUSEPORT` socket/reader goroutine than the original, forcing the race window to be hit deliberately rather than relying on luck. The exact same pattern that let the audited contract's CF check be evaluated before the state it was meant to gate had already changed is present here: the replay-window's protective check is evaluated before its own state update completes.

### Impact Explanation
This breaks the anti-replay invariant that the sliding-window `Bits` structure exists to enforce: a captured on-the-wire packet can be redelivered and accepted a second time, resulting in decrypted/forged traffic delivery (duplicate application-layer messages delivered to the TUN device) despite Nebula's documented replay protection. This falls squarely under "traffic decryption/forgery/replay" impact.

### Likelihood Explanation
Exploitability requires: (1) an attacker able to observe/capture a valid encrypted Nebula packet on the wire (no valid CA cert needed — packets are UDP and visible to any network observer/MITM), and (2) `routines`/multi-queue reader mode enabled (`listen.routines`/`routines` > 1, a supported and documented production configuration) so that duplicate deliveries can land on different reader goroutines concurrently. Given Nebula explicitly supports multi-queue UDP listening as a performance feature, this is a realistic deployment configuration, and the race window (unlocked decrypt) is wide enough (an AEAD operation) to be practically hittable by flooding duplicate packets.

### Recommendation
Hold `decryptLock` across the full check-decrypt-update sequence (or otherwise make the check-and-mark atomic), the same fix pattern recommended in the referenced report (perform the state mutation atomically with the guard, not the check-then-later-update split):
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
Apply the same change to `VerifyRelay`.

### Proof of Concept
1. Enable `routines: 2` (or more) so multiple `SO_REUSEPORT` sockets/goroutines each call `listenOut` → `readOutsidePackets` → `hostinfo.ConnectionState.Decrypt` for packets addressed to the same `RemoteIndex` [5](#0-4) .
2. Capture one legitimate encrypted data packet from the victim tunnel (e.g., via passive network capture, as demonstrated for the analogous relay-replay scenario in `TestRelayReplayProtection`) [6](#0-5) .
3. Resend two copies of the exact same captured packet nearly simultaneously via source ports/paths that the kernel's flow hash assigns to two different reader sockets.
4. Both `Decrypt` calls execute `window.Check` before either has executed `window.Update` (the gap between lines 64-65 and 75-76 of `connection_state.go`), so both succeed and both plaintexts are delivered to the TUN device — the replay is accepted twice instead of being rejected by the anti-replay window.

### Citations

**File:** connection_state.go (L61-81)
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

**File:** interface.go (L273-286)
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

**File:** outside.go (L124-132)
```go
	}

	out, err = hostinfo.ConnectionState.Decrypt(f.l, h.MessageCounter, out, packet, nb)
	if err != nil {
		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			hostinfo.logger(f.l).Debug("Failed to decrypt packet", "error", err, "from", via, "header", h)
		}
		return
	}
```

**File:** e2e/tunnels_test.go (L377-382)
```go
// TestRelayReplayProtection asserts that a relay (forwarding-type) node rejects
// replayed relay frames. A captured relay frame, re-injected with the same
// message counter, must be dropped by the replay window rather than re-forwarded
// to the relay target. Before the fix, handleOutsideRelayPacket authenticated the
// frame but never advanced the replay window, so every replay was re-forwarded.
func TestRelayReplayProtection(t *testing.T) {
```
