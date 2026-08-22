This confirms multiple UDP reader routines (`listen.routines` on Linux with `SO_REUSEPORT`) can concurrently call `readOutsidePackets` for the same tunnel, each ultimately invoking `ConnectionState.Decrypt`. That establishes the concurrency precondition needed for the finding below.

### Title
Non-atomic replay-window check-then-update in `ConnectionState.Decrypt`/`VerifyRelay` allows concurrent replay bypass - (File: `connection_state.go`)

### Summary
`ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` split the anti-replay check into two separate critical sections: `window.Check` is taken under `decryptLock`, released, the packet is authenticated/decrypted, and only afterward is `decryptLock` re-acquired to call `window.Update`, which actually marks the counter as seen. Because the lock is dropped between the check and the update, two concurrent invocations carrying the same message counter (e.g., the same captured wire packet delivered twice) can both pass `window.Check` before either has called `window.Update`, letting both proceed through authentication/decryption instead of exactly one being rejected as a replay.

### Finding Description
The replay window (`Bits`) is meant to guarantee each message counter is accepted at most once per `ConnectionState`. `Check` only reads the current window state; `Update` is the operation that actually records the counter as consumed. In `Decrypt`: [1](#0-0) 
the sequence is: lock → `Check` → unlock → `DecryptDanger` (no lock held) → lock → `Update` → unlock. The same pattern exists in `VerifyRelay`: [2](#0-1) 

This mirrors the reported bug class of "referencing state without first performing the operation that keeps it authoritative": the `Check` call is a stale read of the replay window that has not yet been updated to reflect any interleaved caller-in-progress, so the "is this counter unused" decision is made against outdated state during the window where a duplicate is also mid-flight.

Because Nebula supports multiple concurrent UDP reader routines per tunnel (`listen.routines` with `SO_REUSEPORT`, exercised via `f.routines` reader goroutines each calling `listenOut`/`readOutsidePackets`): [3](#0-2) [4](#0-3) 
an attacker who captures one legitimate wire packet from an already-established tunnel and re-injects it twice at (near-)the same time toward the receiver can cause both copies to reach `Decrypt`/`VerifyRelay` roughly simultaneously on different reader goroutines. Both can pass `window.Check` while the window has not yet been advanced by either, both then complete decryption/authentication, and only the second `window.Update` call fails — by which point the first (and, depending on timing, potentially both, since the ordering of the two `Update` calls is independent of which `Check` ran "first") replayed packet has already been decrypted and its plaintext delivered to the overlay/tun device or, for `VerifyRelay`, forwarded onward by a relay node. The changelog itself documents a related, already-fixed instance of this general class ("Advance the replay window on relayed packets so a relay drops replayed frames instead of re-forwarding them" and "Lock replay window updates so concurrent readers can't corrupt it"), confirming replay-window race conditions are a recognized, real risk area in this codebase: [5](#0-4) 
However, that fix addressed corruption/no-advance bugs in `Bits` itself and relay forwarding, not the check/update lock-release gap in `ConnectionState.Decrypt`/`VerifyRelay`.

### Impact Explanation
Successful exploitation lets an external attacker (who only needs to observe/capture on-path traffic between two already-handshaked peers, no CA-signed certificate required) cause a captured data-plane or relay-verification frame to be accepted and processed more than once despite Nebula's replay-window protection. For `VerifyRelay`, this directly reproduces the double-forwarding behavior the project previously fixed for a different code path — a relay could re-forward a replayed frame to the ultimate target. For `Decrypt`, a replayed application packet could be delivered twice to the tun device, undermining the integrity/freshness guarantee the replay window is supposed to provide (duplicate delivery of the same encrypted message, defeating "process at most once" semantics that higher layers may rely on).

### Likelihood Explanation
Exploitation requires (1) the ability to capture and replay a wire packet — feasible for any on-path or off-path attacker who can inject UDP packets to the target's listen port, and (2) the receiver having more than one reader routine (`listen.routines`/`SO_REUSEPORT`, supported on Linux) processing the same `ConnectionState` concurrently, or any other concurrent-call path into `Decrypt`/`VerifyRelay` for the same tunnel. With multiple routines enabled, delivering the duplicate packet at nearly the same time (which an attacker fully controls) makes the race straightforward to trigger; the window between the `Check` unlock and decrypt completing is attacker-widenable simply by sending both copies back-to-back.

### Recommendation
Hold `decryptLock` across the entire check-decrypt-update sequence in both `Decrypt` and `VerifyRelay` (or fold the "is this counter unused" test into the same atomic operation that marks it used, e.g. an atomic check-and-set on `Bits`), so no other caller can observe or act on the window state between the check and the corresponding update.

### Proof of Concept
1. Establish a tunnel between two Nebula nodes with `listen.routines` > 1 (Linux, `SO_REUSEPORT`) so `f.routines` ≥ 2 reader goroutines call `readOutsidePackets` → `Decrypt` concurrently for the same `ConnectionState`.
2. Capture one legitimate data-plane packet with message counter `N` sent to the receiver.
3. Re-inject that exact captured UDP packet twice, nearly simultaneously, at the receiver's listen port (as the relay replay test does for the `VerifyRelay` path): [6](#0-5) 
4. With sufficiently tight timing, both copies enter `Decrypt` on different reader goroutines, both call `window.Check(l, N)` before either calls `window.Update(l, N)`, both pass the check, and both complete `DecryptDanger` — resulting in the plaintext being delivered twice (or, on the relay path, the frame being forwarded twice) despite the replay window.

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

**File:** CHANGELOG.md (L79-82)
```markdown
- Advance the replay window on relayed packets so a relay drops replayed frames instead of re-forwarding them. (#1751)
- Fix a race in relay state handling. (#1753)
- Lock replay window updates so concurrent readers can't corrupt it. (#1802)
- Reject malformed handshakes more reliably, including invalid ed25519 key lengths. (#1601, #1756)
```

**File:** e2e/tunnels_test.go (L453-466)
```go
	// First delivery of the captured frame: the relay should forward it once.
	t.Log("Deliver the captured frame once; relay forwards it to them")
	relayControl.InjectUDPPacket(relayFrame)
	require.Equal(t, 1, drainForwards(200*time.Millisecond), "relay should forward the first, legitimate copy")

	// Replay the exact same frame several times. A correct replay window rejects
	// these duplicates so the relay forwards none of them.
	t.Log("Replay the captured frame; relay must drop the duplicates")
	const replays = 3
	for i := 0; i < replays; i++ {
		relayControl.InjectUDPPacket(relayFrame)
	}
	forwarded := drainForwards(200 * time.Millisecond)
	assert.Equal(t, 0, forwarded, "relay re-forwarded %d/%d replayed relay frames; replay protection is ineffective on relay tunnels", forwarded, replays)
```
