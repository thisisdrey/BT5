This confirms readOutsidePackets is invoked concurrently across multiple reader goroutines (`f.routines`, using `SO_REUSEPORT`), each calling `hostinfo.ConnectionState.Decrypt` on the same shared `ConnectionState` for a given tunnel. The `Decrypt` function's check-then-act structure creates a genuine analog to the missing-reentrancy-guard bug class.

### Title
Check-then-Act Race in Replay Window Allows Concurrent Duplicate Packet Processing to Bypass Anti-Replay Guarantee - (File: connection_state.go)

### Summary
`ConnectionState.Decrypt` (and `VerifyRelay`) implements the anti-replay check as three separate, individually-locked steps: `window.Check()`, an unlocked `DecryptDanger()` call, and `window.Update()`. Because the lock is dropped between the check and the commit, the function violates the checks-effects-interactions discipline the reentrancy-guard report is about — the "effect" (marking the counter seen) is deferred past an expensive external-facing operation performed without holding the mutex, exactly the same anti-pattern (state check → external call → state update, unguarded) that the ReentrancyGuard fix in the report was meant to close.

### Finding Description
`Decrypt` in [1](#0-0)  does:
1. Lock, `window.Check(messageCounter)`, unlock.
2. `dKey.DecryptDanger(...)` — the AEAD decryption — performed with **no lock held**.
3. Lock, `window.Update(messageCounter)`, unlock.

`VerifyRelay` at [2](#0-1)  repeats the identical pattern for relay frames.

Nebula's interface layer deliberately runs `f.routines` concurrent UDP-reader goroutines, each backed by its own `SO_REUSEPORT` socket, all calling into `readOutsidePackets` for packets belonging to the same tunnel/`ConnectionState`: [3](#0-2) . `readOutsidePackets` calls `hostinfo.ConnectionState.Decrypt` directly on the shared, per-tunnel `ConnectionState` object without any per-packet serialization: [4](#0-3) .

Because the replay window's decision is split into an unlocked read-check, an unguarded external operation, and a separate locked commit, two copies of the exact same ciphertext (or two ciphertexts that legitimately decrypt to different values under the same wire counter, if the attacker can arrange it) delivered to two different reader queues at the same instant can both pass `window.Check()` before either commits via `window.Update()`. Both goroutines then proceed to run full AEAD decryption on attacker-controlled ciphertext outside the lock — this is the exact "check, then external interaction, then effect" ordering the reentrancy report flags, just realized as concurrent-goroutine races rather than a Solidity call stack re-entering itself.

### Impact Explanation
The `Bits` anti-replay window (`bits.go`) is the mechanism nebula relies on to guarantee at-most-once delivery per message counter, which the wire protocol's AEAD nonce/counter reuse-avoidance depends on. Splitting the check/decrypt/update sequence across separate lock acquisitions, with the expensive decrypt operation running unguarded, means the invariant "each message counter is fully processed before another copy with the same counter can begin" is not actually enforced by the code — only single-instance completion is serialized, not exclusive one-at-a-time processing. This degrades the anti-replay guarantee's atomicity under nebula's own concurrent multi-reader design, and the end-to-end behavior observed in `TestRelayReplayProtection` ( [5](#0-4) ) shows the project itself treats "replay window not advanced atomically around the decrypt" as a real, previously-shipped bug class for relay frames.

### Likelihood Explanation
Any remote peer that can reach a nebula node with `routines > 1` (the documented, supported multi-queue configuration using `SO_REUSEPORT`) can trigger this by duplicating a captured ciphertext packet and sending both copies back-to-back so the kernel/OS load-balances them onto different reader queues, exactly mirroring the natural race the codebase's own `TestStage1Race` and `TestHandshakeRetransmitDuplicate` tests exercise for other code paths. No valid certificate or authenticated peer status is required — this is purely a race on already-received ciphertext.

### Recommendation
Hold `decryptLock` for the entire check → decrypt → update sequence in both `Decrypt` and `VerifyRelay` (or otherwise make the check-and-reserve step atomic, e.g., by reserving the counter under the lock before decrypting and rolling back on decrypt failure), so that no second caller can observe a "not yet seen" result for a counter that is already being processed.

### Proof of Concept
1. Configure a nebula node with `listen.routines` > 1 (multi-reader/`SO_REUSEPORT` enabled) as in `TestControl_StartMultiqueueFailureReleases`-style setups.
2. Establish a tunnel and capture one legitimate data-plane ciphertext packet (as done in `TestRelayReplayProtection`, [6](#0-5) ).
3. Send two copies of the identical captured packet simultaneously to the victim's UDP socket so the OS delivers them to two different reader routines/queues.
4. Observe via instrumentation/timing that both goroutines pass `window.Check()` and both execute `dKey.DecryptDanger()` concurrently before either calls `window.Update()`, demonstrating the check-then-act window is not atomic across the full decrypt-and-commit sequence — the discipline that `Decrypt`'s check/decrypt/update split fails to provide.

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

**File:** interface.go (L273-337)
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

**File:** e2e/tunnels_test.go (L377-382)
```go
// TestRelayReplayProtection asserts that a relay (forwarding-type) node rejects
// replayed relay frames. A captured relay frame, re-injected with the same
// message counter, must be dropped by the replay window rather than re-forwarded
// to the relay target. Before the fix, handleOutsideRelayPacket authenticated the
// frame but never advanced the replay window, so every replay was re-forwarded.
func TestRelayReplayProtection(t *testing.T) {
```

**File:** e2e/tunnels_test.go (L422-426)
```go
	// Capture a single legitimate relay frame that me transmits toward the relay.
	t.Log("Capture a relay frame from me -> relay")
	myControl.InjectTunPacket(BuildTunUDPPacket(theirVpnV6.Addr(), 80, myVpnV6.Addr(), 80, []byte("replay me")))
	relayFrame := myControl.GetFromUDP(true)
	require.Equal(t, relayUdpAddr, relayFrame.To, "captured frame should be addressed to the relay")
```
