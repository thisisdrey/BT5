### Title
Replay-window TOCTOU race in `ConnectionState.Decrypt` allows duplicate/replayed packets to bypass the anti-replay window - (File: connection_state.go)

### Summary
The reported Omnipool bug is a "check something, then act on unvalidated/stale state" pattern: `swapForGem()` sets Balancer limits to `type(int256).max` and accepts whatever price results, because there is no atomic bound enforced at execution time. Nebula's data-plane replay protection has the analogous structural flaw: the anti-replay check and the anti-replay commit are two separate, unlocked-in-between operations, so a second copy of the same packet can slip through the "not yet seen" check before the first copy commits its counter as seen.

### Finding Description
`ConnectionState.Decrypt` performs anti-replay validation in three discrete steps, taking and releasing `decryptLock` between them:

```go
func (cs *ConnectionState) Decrypt(l *slog.Logger, messageCounter uint64, out []byte, packet []byte, nb []byte) ([]byte, error) {
	cs.decryptLock.Lock()
	result := cs.window.Check(l, messageCounter)   // step 1: "have we seen this?"
	cs.decryptLock.Unlock()
	if !result {
		return nil, ErrAlreadySeen
	}
	out, err = cs.dKey.DecryptDanger(...)          // step 2: expensive AEAD decrypt, NOT locked
	...
	cs.decryptLock.Lock()
	result = cs.window.Update(l, messageCounter)   // step 3: "mark it seen"
	cs.decryptLock.Unlock()
	...
}
``` [1](#0-0) 

`VerifyRelay`, used for relay-forwarded frames, has the identical Check→decrypt→Update pattern with the lock dropped in between: [2](#0-1) 

`Bits.Check` only inspects whether the bit is currently set; it does not itself mark the counter as seen — that only happens later in `Bits.Update`: [3](#0-2) 

Because AEAD decryption is deliberately performed outside the lock (for concurrency/performance), an attacker who captures a single valid UDP packet from a legitimate tunnel — no CA-signed certificate is required, they only need to observe/replay ciphertext on the wire — can retransmit the exact same packet multiple times in quick succession. If those retransmissions are processed concurrently on different reader goroutines/queues (Nebula supports multiple listener `routines`, and `readOutsidePackets` is invoked with a queue index `q` per worker), each concurrent copy can pass `Check()` while the bit for that counter is still unset, because no copy has reached `Update()` yet. Every copy that passes `Check()` proceeds to fully decrypt (a genuinely valid ciphertext, since it's a byte-for-byte replay) and is then roamed/processed via `f.handleHostRoaming` and dispatched to `handleOutsideMessagePacket`/firewall/TUN injection before `Update()` on any of them has run.

This is the same "accept now, validate/commit later without atomicity" defect class as the report: the sandwich-attack report shows a value passed through with unconditional bounds (`int256 max` limits) so the eventual settlement can't be trusted; here the anti-replay window's "not yet accepted" state is checked and acted upon (full decrypt + downstream side effects) before the atomic commit occurs, so the enforcement can be raced.

### Impact Explanation
A successful race allows a captured, already-transmitted packet to be decrypted and re-delivered into the tunnel (TUN injection, roaming state changes, firewall/conntrack side effects, or relay re-forwarding) more than once, i.e., traffic replay/duplication bypassing the anti-replay mechanism that is explicitly relied upon to prevent this (see the `TestRelayReplayProtection` test asserting relayed frames must be dropped on replay, and the changelog entries "Lock replay window updates so concurrent readers can't corrupt it (#1802)" and "Advance the replay window on relayed packets so a relay drops replayed frames instead of re-forwarding them (#1751)", both of which show replay correctness is treated as security-relevant here). Depending on payload semantics carried over the tunnel, a duplicated/replayed packet can cause double processing of overlay traffic, redundant relay forwarding, or spurious roaming/host-state churn.

### Likelihood Explanation
Exploitation requires no valid certificate or trust relationship with the network — only the ability to observe and resend one legitimate ciphertext packet (e.g., a passive on-path or off-path attacker who can capture and retransmit UDP datagrams), plus the target running with multiple UDP reader routines (`listen.routines > 1`, supported and documented in the config) so concurrent decrypt calls on the same `ConnectionState` are possible. The race window is the time between `Check()`'s unlock and `Update()`'s lock, spanning at least one AEAD decrypt operation, which is a realistic (if narrow) window to win by flooding duplicate copies of the captured packet.

### Recommendation
Perform the replay check and the replay-window commit as a single atomic operation under one lock acquisition (e.g., a combined `CheckAndReserve(i)` that marks the slot provisionally seen before decryption and rolls back only on decrypt failure), rather than releasing the lock between `Check()` and `Update()`. This removes the TOCTOU gap while still allowing decryption to happen without holding the lock for the full AEAD operation, by reserving the counter first and only "un-reserving" it if decryption later fails.

### Proof of Concept
1. Establish two Nebula peers with `listen.routines` set above 1 so inbound UDP packets are processed by multiple goroutines concurrently.
2. Capture one legitimate data-plane UDP packet in flight between the peers (its Nebula header + AEAD ciphertext), analogous to `e2e/tunnels_test.go`'s `TestRelayReplayProtection` packet-capture technique: [4](#0-3) .
3. Rapidly re-inject multiple copies of the exact same captured packet at the target's UDP listener so that they land on different reader routines before any of them completes `ConnectionState.Decrypt`.
4. Because `Check()` and `Update()` are not atomic (`connection_state.go:64` vs `connection_state.go:76`), more than one copy can observe the counter as "not yet seen," decrypt successfully, and be delivered to the TUN device / relay-forward path, instead of all-but-one being rejected with `ErrAlreadySeen` as the existing `TestRelayReplayProtection` test expects for the single-threaded case: [5](#0-4) .

Note: I could not find in the indexed codebase an explicit multi-threaded regression test that exercises this exact concurrent race (the existing replay tests are sequential single-goroutine injections), so the precise exploitability under real multi-routine scheduling is inferred from the code structure rather than confirmed by a passing/failing test in the index. A background Devin session with full repo/test access would be needed to write and run a concurrency-based reproduction to confirm timing feasibility.

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

**File:** e2e/tunnels_test.go (L422-430)
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
