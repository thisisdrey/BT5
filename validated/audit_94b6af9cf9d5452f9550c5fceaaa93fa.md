### Title
Replay window Check-Decrypt-Update race in `ConnectionState.Decrypt`/`VerifyRelay` allows duplicate ciphertext acceptance - ([File: connection_state.go])

### Summary
`ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` in `connection_state.go` implement the replay-window nonce check as three separate critical sections instead of one atomic check-and-mark operation: `window.Check` (locked), `DecryptDanger` (unlocked, the "interaction" with attacker-supplied ciphertext), then `window.Update` (locked). This mirrors the reported CEI bug class: the external interaction (decryption/processing) happens before the state effect that actually consumes the anti-replay slot, and the lock is released in between, leaving a window where the same message counter can be "checked" as fresh by two concurrent callers before either one marks it seen.

### Finding Description
`Decrypt` performs:
1. `decryptLock.Lock(); result := cs.window.Check(l, messageCounter); decryptLock.Unlock()` [1](#0-0) 
2. `cs.dKey.DecryptDanger(...)` outside of any lock [2](#0-1) 
3. `decryptLock.Lock(); result = cs.window.Update(l, messageCounter); decryptLock.Unlock()` [3](#0-2) 

`VerifyRelay` follows the identical Check→Decrypt→Update pattern for relay-forwarded frames [4](#0-3) .

This is reachable purely by capturing and duplicating (retransmitting) a single legitimate ciphertext UDP datagram on the wire — the attacker performing the replay needs no CA-signed certificate of their own; they only need to observe/duplicate traffic between two already-handshaked peers. `readOutsidePackets` calls `hostinfo.ConnectionState.Decrypt` directly against the shared per-tunnel `ConnectionState` for every inbound packet [5](#0-4) , and this call path is invoked concurrently from multiple reader goroutines: `Interface.run` spawns one `listenOut` goroutine per configured `f.routines`, each independently reading from the UDP socket(s) and calling `readOutsidePackets` [6](#0-5) , and each `listenOut` invokes `readOutsidePackets` from its own `ListenOut` callback loop [7](#0-6) .

Because `Check` and `Update` are not combined into a single atomic "test-and-set" while holding the lock across the decrypt, two duplicate copies of the same wire packet delivered on different reader queues (or reordered/duplicated by the underlying UDP path) can both pass `Check` before either has called `Update`, both successfully `DecryptDanger`, and both be handed on to `handleOutsideMessagePacket`/tun write, i.e. the same authenticated message is accepted and processed twice. This directly parallels the audited bug's "Checks-Effects-Interactions" violation: the effect that should gate against reuse (marking the counter consumed) is deferred past the interaction (decrypting/using the data), and an attacker abuses the gap by feeding the same "resource" (ciphertext) again before the effect lands.

### Impact Explanation
A successful double-acceptance results in a valid encrypted packet from a legitimate peer being delivered twice to the local TUN device (data-plane replay), or, for `VerifyRelay`, a relay-forwarded control/data frame being accepted and forwarded twice. This is a concrete traffic-replay weakness in the AEAD nonce/sequence-window enforcement that Nebula relies on to prevent duplicate/replayed packet injection, even though authentication itself (AEAD tag, handshake) is not broken. Depending on payload content this could cause duplicate application-level side effects on the receiving overlay node.

### Likelihood Explanation
The race window is narrow (the time to run one AEAD decrypt) but is deterministically triggerable by an attacker with network visibility who duplicates one captured UDP datagram and sends both copies to arrive close together while the target build is configured with `routines > 1` (multi-queue enabled), which is a supported and documented configuration [8](#0-7) . On a single-routine build, ordinary duplicate UDP delivery from the network itself (routers, NAT re-transmission) can also produce near-simultaneous callback invocations, though the likelihood of winning the race is lower with a single goroutine serializing calls at the socket-read level. No malicious peer or valid certificate is required — only capture/replay capability on the underlay network.

### Recommendation
Combine the check and update into a single atomic operation performed under one lock acquisition that spans the entire decision to accept the counter, e.g., a `CheckAndUpdate` on `Bits` that holds `decryptLock` for the check, and only releases it after `DecryptDanger` succeeds and the bit has been committed — or, structure it so the window slot is provisionally reserved before decrypt and rolled back on decrypt failure, rather than leaving an unlocked gap where two callers can both observe "not yet seen."

### Proof of Concept
1. Establish a Nebula tunnel between two nodes with `f.routines` > 1 (multi-queue UDP enabled) or simply exploit natural network-level UDP duplication.
2. Capture one legitimate encrypted `header.Message` UDP packet sent from peer A to peer B.
3. Immediately re-inject two copies of that exact packet toward B, timed to land on two different reader queues (or via two near-simultaneous UDP sends) so both are dispatched to `readOutsidePackets` before either completes.
4. Both invocations call `hostinfo.ConnectionState.Decrypt`; both `window.Check` calls occur before either `window.Update` call commits the counter, because the lock is released between them and `DecryptDanger` runs unlocked in between [9](#0-8) .
5. Both decrypts succeed and both are forwarded to `handleOutsideMessagePacket`, writing the same plaintext to the TUN device twice, confirming the duplicate/replay acceptance.

Note: I was not able to run this PoC in an actual environment (no execution access in this mode); the finding is based on static analysis of the check/decrypt/update lock structure in `connection_state.go` and the concurrent dispatch structure in `interface.go`/`outside.go`. Confirming the exact race timing/likelihood under production conditions would require dynamic testing.

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

**File:** interface.go (L243-248)
```go
	if f.routines > 1 {
		if !f.inside.SupportsMultiqueue() || !f.outside.SupportsMultipleReaders() {
			f.routines = 1
			f.l.Warn("routines is not supported on this platform, falling back to a single routine")
		}
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
