### Title
Time-of-Check/Time-of-Use Gap in Replay-Window Locking Allows Concurrent Duplicate-Packet Decryption (Replay Bypass) - (File: connection_state.go)

### Summary
`ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` check the anti-replay window (`window.Check`), release the lock, perform the (comparatively expensive, attacker-triggerable) AEAD decrypt, and only *afterwards* re-acquire the lock to commit the result via `window.Update`. This mirrors the reentrancy bug class in the external report: a security-critical state commit (`_totalSupply++`/`saleSupplyMinted++` in the Solidity report; `window.Update` marking a message counter "seen" here) happens *after* an attacker-influenced operation, with no guard preventing a second, concurrent invocation from passing the same check before the first invocation's state update lands.

### Finding Description
`Decrypt` in [1](#0-0)  performs:
1. Lock `decryptLock`, call `cs.window.Check(l, messageCounter)`, unlock.
2. Perform `cs.dKey.DecryptDanger(...)` **outside the lock**.
3. Lock `decryptLock` again, call `cs.window.Update(l, messageCounter)`, unlock.

The same pattern exists in `VerifyRelay` at [2](#0-1) .

Nebula's inbound UDP path calls `Decrypt`/`VerifyRelay` per incoming packet from `readOutsidePackets` in [3](#0-2) , and `handleOutsideRelayPacket` for relay frames similarly invokes `VerifyRelay` before forwarding. If two packets carrying the **same `messageCounter`** for the same `ConnectionState` are processed concurrently (e.g., a duplicated/captured packet re-injected by an on-path attacker or via UDP duplication/multipath delivery, exercised by more than one goroutine), both calls to `Check` can observe the counter as "not yet seen" before either call to `Update` commits it, because the lock is dropped in between. Both decrypts can then succeed, and both `Update` calls will mark the slot afterward (the second one will be flagged as a "duplicate" only after the fact, once the bit is already set by the first — but the decrypt for the duplicate copy has already completed and its plaintext already handed off to firewall/tun processing).

This is structurally identical to the reported Solidity issue: a check occurs, an "external"/attacker-triggerable operation happens, and only afterward does the code finalize the state that the check depends on — allowing repeated bypass of a check meant to enforce "process only once."

### Impact Explanation
A successful race lets an attacker's captured/duplicated ciphertext be decrypted and processed twice (or more) by the receiver despite Nebula's replay window being designed to reject exactly this. For relay frames (`VerifyRelay`), this was previously flagged in the codebase itself as a serious issue — the CHANGELOG and `TestRelayReplayProtection` test at [4](#0-3)  shows that a relay failing to properly gate on the replay window let every replayed frame be re-forwarded. The lock-drop between `Check` and `Update` re-opens a narrow but real window for the same class of bug: race-induced double-acceptance of a replayed/duplicated packet, i.e., a remote-state/traffic-processing poisoning via replay, reachable by any peer with no valid certificate needed beyond a normal handshake (or even relay frames touching a relay node that isn't the final recipient).

### Likelihood Explanation
Exploitation requires winning a race between two decrypt calls processing the same counter concurrently, which requires the underlying transport/dispatch to actually deliver duplicate packets to concurrent goroutines (e.g., via multiple UDP read routines or relay forwarding paths) within the narrow window between `Check` and `Update`. This is a race condition, not a deterministic bypass, so likelihood is moderate: it depends on the degree of parallelism in Nebula's packet-reading pipeline and network conditions that produce near-simultaneous duplicate delivery (packet duplication is common on lossy or multi-path networks and easy for an active attacker to force by injecting a duplicate UDP datagram at the right instant).

### Recommendation
Perform the replay-window check-and-mark as a single atomic operation under one lock acquisition spanning both the check and the eventual commit for a given counter — i.e., reserve the slot (mark tentatively) before decrypting, and roll back only if decryption fails, rather than checking, unlocking, decrypting, and updating separately. Alternatively, hold `decryptLock` for the entire `Check`→`Decrypt`→`Update` sequence (accepting the serialization cost) or introduce a per-counter "in-flight" marker so a second concurrent call for the same counter is rejected immediately rather than allowed to also pass `Check`.

### Proof of Concept
Conceptual PoC (race, not deterministic):
1. Attacker captures one valid encrypted data packet destined to a Nebula node (as in `TestRelayReplayProtection`).
2. Attacker injects two copies of that exact packet back-to-back at the UDP layer such that they are picked up by two different processing goroutines/queues before either finishes.
3. Goroutine A: `Decrypt` calls `window.Check(counter)` → true (not seen), unlocks, begins `DecryptDanger`.
4. Goroutine B: before A calls `window.Update`, B calls `window.Check(counter)` → still true (A hasn't updated yet), unlocks, begins `DecryptDanger`.
5. Both A and B successfully decrypt and hand the same plaintext message to `handleOutsideMessagePacket`/relay-forward logic — the replayed packet is processed twice, defeating the anti-replay window's "exactly once" guarantee. Confirming this requires stress-testing `Decrypt`/`VerifyRelay` with concurrent goroutines feeding the identical `messageCounter`, akin to extending `TestRelayReplayProtection` at [5](#0-4)  with concurrent (rather than sequential) delivery of the duplicated frame.

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

**File:** outside.go (L105-132)
```go
	if len(packet) < header.Len+hostinfo.ConnectionState.dKey.Overhead() {
		f.messageMetrics.RxInvalid(1)
		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			f.l.Debug("packet too small", "from", via, "length", len(packet))
		}
		return
	}

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

**File:** e2e/tunnels_test.go (L377-467)
```go
// TestRelayReplayProtection asserts that a relay (forwarding-type) node rejects
// replayed relay frames. A captured relay frame, re-injected with the same
// message counter, must be dropped by the replay window rather than re-forwarded
// to the relay target. Before the fix, handleOutsideRelayPacket authenticated the
// frame but never advanced the replay window, so every replay was re-forwarded.
func TestRelayReplayProtection(t *testing.T) {
	t.Parallel()
	ca, _, caKey, _ := cert_test.NewTestCaCert(cert.Version2, cert.Curve_CURVE25519, time.Now(), time.Now().Add(10*time.Minute), nil, nil, []string{})
	myControl, myVpnIpNet, _, _ := newSimpleServer(cert.Version2, ca, caKey, "me     ", "10.128.0.1/24,fc00::1/64", m{"relay": m{"use_relays": true}})
	relayControl, relayVpnIpNet, relayUdpAddr, _ := newSimpleServer(cert.Version2, ca, caKey, "relay  ", "10.128.0.128/24,fc00::128/64", m{"relay": m{"am_relay": true}})
	theirUdp := netip.MustParseAddrPort("10.0.0.2:4242")
	theirControl, theirVpnIpNet, theirUdpAddr, _ := newSimpleServerWithUdp(cert.Version2, ca, caKey, "them   ", "fc00::2/64", theirUdp, m{"relay": m{"use_relays": true}})

	myVpnV6 := myVpnIpNet[1]
	relayVpnV4 := relayVpnIpNet[0]
	relayVpnV6 := relayVpnIpNet[1]
	theirVpnV6 := theirVpnIpNet[0]

	// Teach me how to reach the relay and that them is reachable via the relay
	myControl.InjectLightHouseAddr(relayVpnV4.Addr(), relayUdpAddr)
	myControl.InjectLightHouseAddr(relayVpnV6.Addr(), relayUdpAddr)
	myControl.InjectRelays(theirVpnV6.Addr(), []netip.Addr{relayVpnV6.Addr()})
	relayControl.InjectLightHouseAddr(theirVpnV6.Addr(), theirUdpAddr)

	r := router.NewR(t, myControl, relayControl, theirControl)
	defer r.RenderFlow()

	myControl.Start()
	relayControl.Start()
	theirControl.Start()

	// Establish the relayed tunnel in both directions so all handshakes complete.
	t.Log("Establish the relayed tunnel")
	myControl.InjectTunPacket(BuildTunUDPPacket(theirVpnV6.Addr(), 80, myVpnV6.Addr(), 80, []byte("Hi from me")))
	p := r.RouteForAllUntilTxTun(theirControl)
	assertUdpPacket(t, []byte("Hi from me"), p, myVpnV6.Addr(), theirVpnV6.Addr(), 80, 80)
	theirControl.InjectTunPacket(BuildTunUDPPacket(myVpnV6.Addr(), 80, theirVpnV6.Addr(), 80, []byte("Hi from them")))
	p = r.RouteForAllUntilTxTun(myControl)
	assertUdpPacket(t, []byte("Hi from them"), p, theirVpnV6.Addr(), myVpnV6.Addr(), 80, 80)

	// Drain anything still queued on me's UDP tx so the next packet we pull is the
	// relay frame we are about to generate.
	for myControl.GetFromUDP(false) != nil {
	}

	// Capture a single legitimate relay frame that me transmits toward the relay.
	t.Log("Capture a relay frame from me -> relay")
	myControl.InjectTunPacket(BuildTunUDPPacket(theirVpnV6.Addr(), 80, myVpnV6.Addr(), 80, []byte("replay me")))
	relayFrame := myControl.GetFromUDP(true)
	require.Equal(t, relayUdpAddr, relayFrame.To, "captured frame should be addressed to the relay")
	var fh header.H
	require.NoError(t, fh.Parse(relayFrame.Data))
	require.Equal(t, header.Message, fh.Type)
	require.Equal(t, header.MessageRelay, fh.Subtype)

	// drainForwards counts relay frames the relay forwards toward them within the
	// settle window. We match on destination + (Message, MessageRelay) so the
	// relay's own direct traffic to them can't be miscounted.
	drainForwards := func(settle time.Duration) int {
		ch := relayControl.GetUDPTxChan()
		count := 0
		for {
			select {
			case pkt := <-ch:
				var ph header.H
				if pkt.To == theirUdpAddr && ph.Parse(pkt.Data) == nil &&
					ph.Type == header.Message && ph.Subtype == header.MessageRelay {
					count++
				}
				pkt.Release()
			case <-time.After(settle):
				return count
			}
		}
	}

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
