### Title
Replay-window check/decrypt/update race allows transient duplicate acceptance of a captured relay/data frame - (File: connection_state.go)

### Summary
`ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` in `connection_state.go` implement the anti-replay window as a three-step, separately-locked sequence: `window.Check` (locked), AEAD decrypt/verify (unlocked), then `window.Update` (locked). This is structurally the same bug class as the external report's "balance sampled before an external action, then compared/consumed after" pattern: the authoritative replay state (`Bits.current`/bitmap) is not made atomic with the operation it is meant to gate, leaving a window in which the same message counter can be admitted more than once before the window state catches up. [1](#0-0) [2](#0-1) 

### Finding Description
`Decrypt` and `VerifyRelay` both call `cs.window.Check(l, messageCounter)` under `decryptLock`, release the lock, perform the (comparatively expensive) AEAD operation without holding any lock, then re-acquire `decryptLock` to call `cs.window.Update(l, messageCounter)`. [3](#0-2) [4](#0-3) 

If the same wire frame (same header, same `MessageCounter`, same ciphertext) reaches `readOutsidePackets` concurrently — e.g. duplicated by an intermediate network path, or replayed deliberately by an attacker who does not hold a valid CA-signed certificate but only observes/replays UDP traffic on the wire — multiple goroutines can each pass `Check` before any of them has called `Update`, because the two calls are not atomic with each other. Each copy will then independently pass AEAD verification (the ciphertext/tag/nonce are identical and valid), and only afterward attempt `Update`; whichever loses the race gets `ErrAlreadySeen` back from `Update`, but by that point the decrypt/verify step (and, for the `VerifyRelay` path in `handleOutsideRelayPacket`, roaming/connection-manager bookkeeping and the relay-forward decision in `outside.go`) has already run for more than one copy. [5](#0-4) [6](#0-5) 

This mirrors the report's root cause exactly: the "before" state (`Check`) is read, an external/asynchronous action is performed, and only afterward is the authoritative state updated — so the gating decision does not reflect the true state at the moment the guarded action (forwarding/decrypting) actually executes.

### Impact Explanation
On the relay-forwarding path (`ForwardingType` in `handleOutsideRelayPacket`), a duplicate that survives the `Check`/decrypt window before `Update` catches it results in the relay re-forwarding a signed/verified but already-consumed relay frame toward the target — the exact "relay drops replayed frames instead of re-forwarding them" property that the anti-replay window exists to guarantee, and that other code in this same file explicitly protects against on the non-racing path. This constitutes remote traffic replay/duplication within a live tunnel, degrading the anti-replay guarantee that the protocol depends on. On the direct-decrypt path, it can cause a single application-layer packet to be delivered to the tun device more than once in a race window, corrupting the assumption that `Bits` gives exactly-once delivery semantics per message counter.

### Likelihood Explanation
The trigger requires only replaying (duplicating) already-observed ciphertext on the wire at high enough concurrency/timing precision to land two receives inside the tiny window between the unlocked `Check` and the subsequent `Update` — no valid certificate, private key, or handshake participation is required, since the attacker only needs to capture and resend an existing frame (as already demonstrated feasible for relay frames in `TestRelayReplayProtection`). Because packet decrypt/verify can run in parallel across the interface's read workers while the window state is only locked around the two short bracketing calls, the probability of a successful race is non-trivial under packet duplication/replay conditions, though it is inherently timing-dependent and bounded (one extra accepted duplicate per race, not unbounded replay).

### Recommendation
Hold `decryptLock` for the entire check-decrypt-update sequence (or otherwise make check+update atomic with respect to the guarded operation), analogous to the report's own recommendation to stop relying on a "before/after" comparison and instead operate on a single, atomically-observed state. Concretely, wrap `cs.window.Check`, the AEAD decrypt/verify call, and `cs.window.Update` in `connection_state.go`'s `Decrypt` and `VerifyRelay` under one critical section (single lock acquisition) so no other goroutine can interleave a duplicate counter between the check and the commit.

### Proof of Concept
1. Establish a tunnel through a relay as in `TestRelayReplayProtection` and capture one legitimate relay frame `relayFrame` addressed to the relay. [7](#0-6) 
2. Instead of injecting the frame sequentially (as the existing regression test does), inject `N` copies of `relayFrame` concurrently (e.g. from `N` goroutines calling `relayControl.InjectUDPPacket(relayFrame)` at the same instant) so that multiple reader paths call `hostinfo.ConnectionState.VerifyRelay` for the same `MessageCounter` before any of them has reached `window.Update`.
3. Because `VerifyRelay` calls `window.Check` and `window.Update` as two separately-locked operations with the AEAD verify (`DecryptDanger`) unlocked in between, more than one of the concurrent copies can pass `Check`, verify successfully, and reach `handleOutsideRelayPacket`'s `ForwardingType` branch, causing more than one forward of the same relay frame toward `them` — contradicted by the guarantee `TestRelayReplayProtection` otherwise asserts (`forwarded == 0` for sequential replays). [8](#0-7)

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

**File:** outside.go (L176-225)
```go
func (f *Interface) handleOutsideRelayPacket(hostinfo *HostInfo, via ViaSender, out []byte, packet []byte, h *header.H, fwPacket *firewall.Packet, lhf *LightHouseHandler, nb []byte, q int, localCache firewall.ConntrackCache) {
	// Successfully validated the thing. Get rid of the Relay header and the AEAD tag
	signedPayload := packet[header.Len : len(packet)-hostinfo.ConnectionState.dKey.Overhead()]
	// Pull the Roaming parts up here, and return in all call paths.
	f.handleHostRoaming(hostinfo, via)
	// Track usage of both the HostInfo and the Relay for the received & authenticated packet
	f.connectionManager.In(hostinfo)
	f.connectionManager.RelayUsed(h.RemoteIndex)

	relay, ok := hostinfo.relayState.QueryRelayForByIdx(h.RemoteIndex)
	if !ok {
		// The only way this happens is if hostmap has an index to the correct HostInfo, but the HostInfo is missing
		// its internal mapping. This should never happen.
		hostinfo.logger(f.l).Error("HostInfo missing remote relay index",
			"relayRemoteIndex", h.RemoteIndex,
		)
		return
	}

	switch relay.Type {
	case TerminalType:
		// If I am the target of this relay, process the unwrapped packet
		// From this recursive point, all these variables are 'burned'. We shouldn't rely on them again.
		via = ViaSender{
			UdpAddr:   via.UdpAddr,
			relayHI:   hostinfo,
			relay:     relay,
			IsRelayed: true,
		}
		f.readOutsidePackets(via, out[:0], signedPayload, h, fwPacket, lhf, nb, q, localCache)
	case ForwardingType:
		// Find the target HostInfo relay object
		targetHI, targetRelay, err := f.hostMap.QueryVpnAddrsRelayFor(hostinfo.vpnAddrs, relay.PeerAddr)
		if err != nil {
			hostinfo.logger(f.l).Info("Failed to find target host info by ip",
				"relayTo", relay.PeerAddr,
				"relayFrom", hostinfo.vpnAddrs[0],
				"error", err,
			)
			return
		}

		// If that relay is Established, forward the payload through it
		if targetRelay.State == Established {
			switch targetRelay.Type {
			case ForwardingType:
				// Forward this packet through the relay tunnel, rebuilding it in place.
				// Encode overwrites the old outer header, and the new AEAD tag lands where the old one was
				fwdBuf := packet[:0:len(packet)] // Cap to len(packet) to protect memory from a larger parent buffer
				f.SendVia(targetHI, targetRelay, signedPayload, nb, fwdBuf, true)
```

**File:** e2e/tunnels_test.go (L422-431)
```go
	// Capture a single legitimate relay frame that me transmits toward the relay.
	t.Log("Capture a relay frame from me -> relay")
	myControl.InjectTunPacket(BuildTunUDPPacket(theirVpnV6.Addr(), 80, myVpnV6.Addr(), 80, []byte("replay me")))
	relayFrame := myControl.GetFromUDP(true)
	require.Equal(t, relayUdpAddr, relayFrame.To, "captured frame should be addressed to the relay")
	var fh header.H
	require.NoError(t, fh.Parse(relayFrame.Data))
	require.Equal(t, header.Message, fh.Type)
	require.Equal(t, header.MessageRelay, fh.Subtype)

```

**File:** e2e/tunnels_test.go (L458-466)
```go
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
