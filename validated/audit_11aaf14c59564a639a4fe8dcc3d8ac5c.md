Confirmed: `f.routines` reader goroutines each run `listenOut(i)` independently, and every reader can call `readOutsidePackets` concurrently for the *same* `hostinfo.ConnectionState`, since `HostInfo` lookup by `RemoteIndex` is shared across all queues. [1](#0-0) [2](#0-1) [3](#0-2) 

### Title
Replay-window check/decrypt/update race allows duplicate delivery of a replayed data-plane packet - (File: connection_state.go)

### Summary
`ConnectionState.Decrypt` (and `VerifyRelay`) validate the replay window, then decrypt, then mark the window — as three separate lock-protected steps rather than one atomic operation. Because the underlay reader runs on `f.routines` independent goroutines that all share the same `HostInfo`/`ConnectionState` for a peer, two workers can process the same message counter concurrently, both pass the `Check`, both successfully decrypt, and only afterwards does `Update` reject the second one — after its plaintext has already been produced and (in the caller) forwarded to the tun device or firewall. This mirrors the WildCredit root cause: performing a state-mutating "commit" (`Update`, analogous to `accrue`) too late relative to the side effect that state was supposed to gate (delivering a decrypted, "already seen" packet, analogous to minting shares before debt accrual).

### Finding Description
`Decrypt` takes `decryptLock`, calls `cs.window.Check(...)`, releases the lock, performs the AEAD decrypt outside the lock, then re-acquires the lock to call `cs.window.Update(...)`: [4](#0-3) 

`VerifyRelay` follows the identical pattern for relay frames: [5](#0-4) 

The replay window's actual anti-replay guarantee is only enforced by `Update`, which marks the bit and is the sole place duplicates are rejected on the "same-counter-not-yet-marked" path: [6](#0-5) 

The `Check` call by itself is read-only and does not reserve the slot, so it provides no exclusivity guarantee once the lock is dropped. Because `readOutsidePackets` (which calls `Decrypt`/`VerifyRelay`) executes on any of `f.routines` reader goroutines for the same `HostInfo`, an attacker who sends two copies of the same captured ciphertext at nearly the same time can cause both to pass `Check` before either calls `Update`: [7](#0-6) [3](#0-2) 

This is structurally the same class of bug as `uniClaimDeposit`: the code separates "read the authoritative state to decide legitimacy" from "commit the state that enforces legitimacy," with a window in between where the guarded side effect (minting shares / delivering decrypted plaintext) can occur twice for what should be a single-use token (a supply of tokens backed by `totalSupplyAmount` / a message counter in the replay window).

### Impact Explanation
A successful race lets an attacker's replayed packet be decrypted and delivered a second time even though the replay window is supposed to guarantee each message counter is processed at most once. This can cause duplicate application of an inbound VPN packet (e.g. duplicate tun delivery), a duplicate relay forward (undermining the intended one-shot forwarding of relay frames, see the `TestRelayReplayProtection` regression test that hardened this exact path), or duplicate firewall/conntrack state changes derived from the packet. It does not itself yield a certificate or handshake bypass, but breaks the "processed at most once" invariant that downstream logic (conntrack, relay forwarding, application semantics) depends on. [8](#0-7) 

### Likelihood Explanation
Exploitation requires only a passively captured ciphertext and the ability to send two UDP datagrams to the target in close succession — no valid CA-signed certificate, no privileged position, and no participation in the handshake is needed beyond having observed one packet on the wire (an on-path/relay-adjacent attacker). It is timing-dependent (the two decrypts must race inside the `Check`-to-`Update` gap), so likelihood is moderate, not certain, but it is a real, remotely triggerable race in the exact replay-defense code path that the project's own changelog and tests treat as security-critical (`Advance the replay window on relayed packets...`, `Lock replay window updates so concurrent readers can't corrupt it.`). [9](#0-8) 

### Recommendation
Combine the check-and-mark into a single atomic, lock-held operation (i.e., call `window.Check` and `window.Update` under one critical section, or fold them into a single `CheckAndReserve`-style method that atomically tests-and-sets the bit before decryption begins), analogous to the WildCredit fix of accruing debt before minting shares in the same call rather than as separate steps that a concurrent caller can interleave between.

### Proof of Concept
1. Establish a tunnel between two nodes and capture one legitimate data-plane ciphertext packet with message counter `N` addressed to a peer whose interface runs with `listen.routines > 1`.
2. Simultaneously inject two copies of the exact same captured UDP packet, timed so both land on different reader goroutines (`listenOut(i)`) before either has completed its `Decrypt` call for counter `N`.
3. Both goroutines call `cs.window.Check(l, N)` under the lock separately (each sees "not yet seen" since neither has called `Update` yet), release the lock, and independently perform `DecryptDanger` successfully.
4. Both proceed to the caller (e.g., `handleOutsideRelayPacket` / tun write) with valid decrypted plaintext before `Update` runs for either; only the second `Update` call returns `false`, but by then the first delivery has already occurred and the second's plaintext may already have been acted upon before its `Update` result is checked, depending on caller ordering.
5. Observe duplicate downstream processing (e.g., a second relay forward or a second tun delivery) for what should be a single, replay-protected message counter — analogous to the WildCredit attacker minting shares before `accrue` executes, exploiting the gap between the read that should gate an action and the write that commits it.

### Citations

**File:** interface.go (L273-326)
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
```

**File:** outside.go (L86-123)
```go
	// Relay packets are special
	isMessageRelay := (h.Type == header.Message && h.Subtype == header.MessageRelay)

	var hostinfo *HostInfo
	if isMessageRelay {
		hostinfo = f.hostMap.QueryRelayIndex(h.RemoteIndex)
	} else {
		hostinfo = f.hostMap.QueryIndex(h.RemoteIndex)
	}

	// At this point we should have a valid existing tunnel, verify and send
	// recvError if necessary
	if hostinfo == nil || hostinfo.ConnectionState == nil {
		if !via.IsRelayed {
			f.maybeSendRecvError(via.UdpAddr, h.RemoteIndex)
		}
		return
	}

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
```

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

**File:** bits.go (L229-250)
```go
	// If i is within the current window but below the current counter, check to see if it's a duplicate
	if b.strictlyWithinWindow(i) {
		pos := i & b.lengthMask
		word := pos >> 6
		mask := uint64(1) << (pos & 63)
		w := b.bits[word]
		if b.current == i || w&mask != 0 {
			if l.Enabled(context.Background(), slog.LevelDebug) {
				l.Debug("Receive window",
					"accepted", false,
					"currentCounter", b.current,
					"incomingCounter", i,
					"reason", "duplicate",
				)
			}
			b.dupeCounter.Inc(1)
			return false
		}

		b.bits[word] = w | mask
		return true
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

**File:** CHANGELOG.md (L79-81)
```markdown
- Advance the replay window on relayed packets so a relay drops replayed frames instead of re-forwarding them. (#1751)
- Fix a race in relay state handling. (#1753)
- Lock replay window updates so concurrent readers can't corrupt it. (#1802)
```
