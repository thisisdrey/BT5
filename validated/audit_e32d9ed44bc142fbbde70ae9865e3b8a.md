### Title
Anti-replay window check-then-update race allows duplicate packet re-processing - ([File: connection_state.go])

### Summary
`ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` split the replay-window verdict into a non-atomic "check" then "decrypt" then "commit" sequence, releasing the guarding mutex between the check and the commit. When multiple reader routines process the underlay socket concurrently (Nebula's multi-queue/`SupportsMultipleReaders` mode), two copies of the same duplicated ciphertext packet can both pass the replay check before either one commits, letting both be decrypted and delivered to the data plane as if they were distinct, legitimate packets — defeating the anti-replay window's purpose.

### Finding Description
The replay window (`Bits`) is guarded by `cs.decryptLock`, but the lock is held only around the individual `Check`/`Update` calls, not around the intervening AEAD decrypt: [1](#0-0) 

```
cs.decryptLock.Lock()
result := cs.window.Check(l, messageCounter)   // (1) verdict: "not yet seen"
cs.decryptLock.Unlock()
...
out, err = cs.dKey.DecryptDanger(...)          // (2) unlocked AEAD decrypt
...
cs.decryptLock.Lock()
result = cs.window.Update(l, messageCounter)   // (3) commit: mark as seen
cs.decryptLock.Unlock()
```

The same pattern exists in `VerifyRelay`: [2](#0-1) 

`Bits.Check` only inspects window state without mutating it, and `Bits.Update` is the sole mutator (`bits.go`): [3](#0-2) 

This is a genuine check-then-act (TOCTOU) gap of the same class as the report's "intermediate value used before it is committed/finalized" reentrancy bug: the code makes an accept/reject decision for a given nonce, performs expensive work in between (decryption) with the lock released, and only *afterward* commits the state that was supposed to prevent duplicate acceptance. If a second copy of the same packet (or a duplicate injected by an on-path/off-path attacker) is processed by another goroutine during that unlocked window, `Check` for the duplicate also succeeds (since `Update` hasn't run yet), both packets decrypt successfully, and only one call to `Update` will ultimately "win" — but by then both plaintexts have already been handed to `f.handleOutsideMessagePacket`/the firewall/tun device.

This is reachable by processing multiple UDP reader queues concurrently, which Nebula explicitly supports: [4](#0-3) [5](#0-4) 

No valid certificate is required for the attacker to trigger this: any party able to duplicate/replay a UDP datagram on the path between two already-established Nebula peers (or even one peer, per Nebula's threat model where the underlay network is untrusted) can attempt to feed the same ciphertext through the interface faster than the receiver's own commit path completes, e.g. by racing packet delivery across reader queues or via kernel-level duplication.

### Impact Explanation
A successful race allows a captured/duplicated encrypted packet to be delivered twice to the receiving node's inside device/firewall instead of being dropped as a replay. Depending on the payload, this can result in traffic replay (e.g., a duplicated inside-network IP packet processed twice), undermining the AEAD replay-window guarantee that Nebula's data-plane security model depends on. This maps to the "traffic decryption/forgery/replay" impact category.

### Likelihood Explanation
Exploitation requires winning a narrow race window between `Check` and `Update` across concurrent reader routines, which only exist when `routines > 1` (multi-queue socket configurations) is enabled and supported by the platform. This narrows practical likelihood versus the theoretical existence of the gap, but the design flaw itself is deterministic and reachable by anyone who can duplicate/replay UDP datagrams toward a multi-queue Nebula node.

### Recommendation
Hold `decryptLock` across the entire check-decrypt-update sequence in both `Decrypt` and `VerifyRelay` (or otherwise make the check-then-commit atomic with respect to a given `messageCounter`), so that only one caller can ever observe a "not yet seen" verdict for a given counter and successfully commit it before a concurrent duplicate is rejected.

### Proof of Concept
Conceptually: run two goroutines that both call `ConnectionState.Decrypt` with the same `hostinfo.ConnectionState`, the same captured ciphertext `packet`, and the same `messageCounter`, started so their `window.Check` calls both execute before either reaches `window.Update` (e.g. insert a small sleep in a test build between the Check and Decrypt steps, or drive two `f.listenOut` reader goroutines with the identical duplicated datagram delivered near-simultaneously to different queues). Both calls should decrypt successfully and return non-error plaintext, at which point the packet has been "accepted" twice by the data plane even though the anti-replay window is designed to allow only one acceptance per counter value.

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

**File:** interface.go (L309-326)
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
```
