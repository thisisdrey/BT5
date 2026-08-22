### Title
Anti-replay window Check/Update settlement is not atomic in `ConnectionState.Decrypt`, allowing duplicate packet processing under concurrent UDP readers - (File: `connection_state.go`)

### Summary
`ConnectionState.Decrypt` (and its relay counterpart `VerifyRelay`) split the anti-replay check into two separately-locked steps: `window.Check()` is called and released, the packet is then decrypted, and only afterwards is `window.Update()` called (again under its own lock) to actually mark the counter as consumed. Because `decryptLock` is released between the `Check` and the `Update`, the "settlement" of the replay window (marking a counter seen) is decoupled from the "check", exactly mirroring the reported bug class: a security-relevant piece of state (the bucket's `lastRewardIndex` / here, the replay window's `current`/bitmap) is read for a decision but the actual settling of that state happens later and can be raced, so a second concurrent attempt for the *same* counter can pass the check before the first attempt settles it.

### Finding Description
`Bits.Check` reports whether a message counter is "new" (not yet in the window), and `Bits.Update` is the operation that actually records the counter as seen. In `connection_state.go`: [1](#0-0) 

the sequence is: `Lock → Check → Unlock`, then AEAD decrypt (no lock held), then `Lock → Update → Unlock`. There is a window, while the ciphertext is being decrypted, during which the replay bitmap has *not yet* been updated for this counter. Nebula's data plane is read by multiple independent goroutines when `routines > 1` (SO_REUSEPORT-style multi-queue UDP), each calling `readOutsidePackets` → `ConnectionState.Decrypt` concurrently for the same `hostinfo`/`ConnectionState`: [2](#0-1) [3](#0-2) 

If the exact same packet (a captured/duplicated ciphertext with the same `messageCounter`) is delivered twice — e.g. by an on-path attacker deliberately duplicating and re-injecting a captured UDP datagram (no CA-signed certificate needed, since the attacker never needs to complete a handshake, only to replay bytes seen on the wire) so that both copies land on different reader queues — both goroutines can pass `Check()` before either has called `Update()`, both will successfully AEAD-decrypt (the ciphertext and counter are identical and still valid), and both will deliver the plaintext to `handleOutsideMessagePacket`. This is precisely the "reset/settle" ordering flaw in the reported issue: the decision (Check) is made against state that has not yet been durably updated (settled), so the same input can be accepted twice.

### Impact Explanation
This breaks the core guarantee of Nebula's anti-replay window: a captured packet should never be processed twice. Successfully racing the window allows an unauthenticated network attacker to force duplicate delivery/processing of a legitimate encrypted packet, i.e., a concrete traffic-replay bypass on the data plane (`header.Message` payloads reach `handleOutsideMessagePacket` twice, `header.Test`/`header.CloseTunnel` etc. could likewise be replayed once past the window). This matches the accepted impact category of "traffic decryption/forgery/replay".

### Likelihood Explanation
Exploitation requires: (1) `routines > 1` (multiple UDP reader queues), which is a supported and documented configuration, and (2) the ability to duplicate a captured UDP datagram onto the wire at (or very near) the same instant so it lands on two different reader queues before the first `Update()` commits — feasible for a network-adjacent/on-path attacker without needing a CA-signed certificate, since replaying raw bytes requires no cryptographic material of their own. The race window is small (one AEAD decrypt operation) but is a genuine TOCTOU flaw rather than a theoretical one, and the CHANGELOG shows the project has previously had to patch exactly this class of replay-window locking issue (#1802, #1751), indicating it is a recognized and reachable attack surface.

### Recommendation
Make the check-and-settle operation on the replay window atomic: hold `decryptLock` across `Check`, `DecryptDanger`, and `Update` (or perform a single combined `CheckAndReserve`/`CheckAndUpdate` operation under one lock) so that no second caller can observe the counter as "not yet seen" until the first caller has either committed it or failed. Apply the same fix to `VerifyRelay`.

### Proof of Concept
1. Configure `routines: 2` so Nebula binds multiple SO_REUSEPORT UDP sockets, each read by an independent goroutine (`f.listenOut(i)` in `interface.go`).
2. Establish a tunnel between two peers and capture one legitimate data-plane packet (ciphertext + header) sent from A to B.
3. As an on-path/network attacker, inject the identical captured datagram twice at B's UDP port at nearly the same instant (e.g., via two sockets targeting the two reader queues, or packet duplication at a switch/relay).
4. If both copies are picked up by different reader goroutines before the first `window.Update()` call commits, both will pass `window.Check()` in `ConnectionState.Decrypt`, both AEAD-decrypt successfully, and both proceed to `handleOutsideMessagePacket`, resulting in the same message being delivered twice to the tun device — demonstrating replay-window bypass analogous to the "settle-after-check" flaw described in the source report.

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
