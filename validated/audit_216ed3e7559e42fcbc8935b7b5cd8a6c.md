### Title
Non-atomic Check-then-Decrypt-then-Update in anti-replay window allows a raced/spoofed replay to win `handleHostRoaming` and redirect `hostinfo.remote` - (File: connection_state.go, outside.go)

### Summary
`ConnectionState.Decrypt` checks the replay window, performs the AEAD decrypt, and only *then* marks the counter as consumed, using two separate lock acquisitions instead of one atomic reserve-then-commit operation. Because multiple `listenOut` goroutines can concurrently process packets for the same `HostInfo`/`ConnectionState`, an attacker who has eavesdropped a legitimate ciphertext and races a spoofed replay of it can win the second (`Update`) critical section, causing `readOutsidePackets` to treat the spoofed copy as the "winning" authenticated packet and pass its `via.UdpAddr` into `handleHostRoaming`, which then calls `SetRemote` on that attacker-controlled address.

### Finding Description
`Decrypt` in [1](#0-0)  performs three separate steps: `window.Check` (locked), the actual AEAD `DecryptDanger` call (unlocked), and `window.Update` (locked). Between the `Check` and `Update` calls there is a window during which a second, concurrently-processed copy of the same ciphertext/counter can also pass `Check` (since the bit for that counter has not yet been set), then also successfully complete `DecryptDanger` (deterministic AEAD verification of the same valid ciphertext succeeds for any caller), and race to be the one whose `Update` call marks the slot first. Only one of the two `Decrypt` calls returns success — but which one wins is determined by lock-acquisition order in `Update`, not by which packet's *source address* is legitimate.

`readOutsidePackets` is invoked from `f.listenOut(i)` for `i` in `[0, f.routines)`, each running as an independent goroutine reading from its own underlay socket/queue: [2](#0-1)  and [3](#0-2) . Packets destined for the same `HostInfo` (same `RemoteIndex`) but arriving via different queues/goroutines can therefore call `hostinfo.ConnectionState.Decrypt` concurrently: [4](#0-3) .

If the goroutine processing the attacker's spoofed replay wins the `Update` race, `Decrypt` returns success for that call with `err == nil`, and `readOutsidePackets` immediately calls `f.handleHostRoaming(hostinfo, via)` with `via.UdpAddr` set to the attacker's spoofed source address: [5](#0-4) . `handleHostRoaming` only checks `GetRemoteAllowList().AllowAll(hostinfo.vpnAddrs, via.UdpAddr.Addr())`, which by default allows all addresses and provides no authentication of the UDP source — it cannot, since UDP source addresses are inherently unauthenticated. It then calls `hostinfo.SetRemote(via.UdpAddr)` [6](#0-5) , redirecting the tunnel's remote endpoint to the attacker's address. The genuine sender's identical packet, losing the race, is rejected by the losing `Update` call as `ErrAlreadySeen` and is silently dropped, so the legitimate packet's own `via.UdpAddr` never reaches `handleHostRoaming` for that counter.

This is a genuine TOCTOU gap in the "replayed packets are dropped" guarantee: exactly one copy of a given counter is accepted (as intended), but *which* copy (and therefore which source address is trusted for roaming) is a race outcome rather than an authentication decision.

### Impact Explanation
A successful race lets an attacker with eavesdropping capability and precise timing redirect `hostinfo.remote` to an address they control, without possessing any key material or forging any cryptography. Because `SetRemote` changes where subsequent outbound traffic for that tunnel is sent, this is a remote state poisoning / traffic-redirection issue that can be used for denial-of-service against the legitimate peer (traffic no longer reaches them) and potential redirection of traffic to an address of the attacker's choosing, undermining "peer addressing is authenticated."

### Likelihood Explanation
Exploitation requires: (1) an on-path or eavesdropping capability to obtain a valid ciphertext + counter for the target tunnel (an off-path blind attacker without traffic visibility cannot construct a valid ciphertext, since AEAD prevents forgery); (2) sending a spoofed copy of that exact ciphertext timed to race the legitimate packet's processing before its `Update` call completes; and (3) both copies landing in concurrently-scheduled `listenOut` goroutines. This is a narrow, timing-dependent race rather than a deterministic bypass, and each attempt targets a single specific message counter value, so it is not trivially "always-win," but it is a real, repeatable race condition rooted in the non-atomic `Check`/`Decrypt`/`Update` sequence, not merely a config or best-practice issue.

### Recommendation
Make the anti-replay window check-and-mark atomic with the decrypt operation: hold `decryptLock` (or a per-counter reservation) across the full `Check` → `Decrypt` → `Update` sequence, or restructure `Bits` to offer a single locked "reserve" operation that marks the counter as claimed before decryption begins and rolls it back only if decryption fails. This removes the window in which two concurrent callers can both pass `Check` for the same counter, ensuring the outcome of "who gets treated as authoritative for roaming" cannot be decided by a race between a legitimate sender and a replay.

### Proof of Concept
Unit test plan targeting `connection_state_test.go` (or a new test file):
1. Build two `ConnectionState` objects sharing the same underlying keys/counter state as used by a real `HostInfo` (or directly manipulate the shared `Bits` window plus a stubbed `DecryptDanger`).
2. Capture one valid ciphertext/counter pair.
3. Spawn two goroutines that each call `Decrypt` with the same `messageCounter`/ciphertext, but instrument/delay so that both `Check` calls happen before either `Update` call (simulating two `listenOut` goroutines racing).
4. Assert that exactly one call returns `err == nil`, but show via a controlled interleave (forcing the "attacker" goroutine's `Update` to run first) that the *first* successful decrypt is not deterministically tied to which goroutine called `Check` first — i.e., the "legitimate" caller can lose despite calling `Check` first.
5. Extend to an integration test on `Interface.readOutsidePackets`: fire two goroutines calling `readOutsidePackets` with identical ciphertext/counter but distinct `via.UdpAddr` values (one "legitimate", one "attacker"), and assert that it is possible (over repeated trials with interleaving forced via a test hook/sleep between `Check` and `Update`) for `hostinfo.GetRemote()` to end up equal to the attacker's `via.UdpAddr` after the race, confirming `SetRemote` was poisoned by the losing/attacker copy.

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

**File:** interface.go (L273-279)
```go
func (f *Interface) run() {
	// Launch n queues to read packets from udp
	for i := 0; i < f.routines; i++ {
		f.wg.Go(func() {
			f.listenOut(i)
		})
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

**File:** outside.go (L126-136)
```go
	out, err = hostinfo.ConnectionState.Decrypt(f.l, h.MessageCounter, out, packet, nb)
	if err != nil {
		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			hostinfo.logger(f.l).Debug("Failed to decrypt packet", "error", err, "from", via, "header", h)
		}
		return
	}

	// Roam before we respond
	f.handleHostRoaming(hostinfo, via)
	f.connectionManager.In(hostinfo)
```

**File:** outside.go (L264-292)
```go
func (f *Interface) handleHostRoaming(hostinfo *HostInfo, via ViaSender) {
	curRemote := hostinfo.GetRemote()
	if !via.IsRelayed && curRemote != via.UdpAddr {
		if !f.lightHouse.GetRemoteAllowList().AllowAll(hostinfo.vpnAddrs, via.UdpAddr.Addr()) {
			if f.l.Enabled(context.Background(), slog.LevelDebug) {
				hostinfo.logger(f.l).Debug("lighthouse.remote_allow_list denied roaming", "newAddr", via.UdpAddr)
			}
			return
		}

		if !hostinfo.lastRoam.IsZero() && via.UdpAddr == hostinfo.lastRoamRemote && time.Since(hostinfo.lastRoam) < RoamingSuppressSeconds*time.Second {
			if f.l.Enabled(context.Background(), slog.LevelDebug) {
				hostinfo.logger(f.l).Debug("Suppressing roam back to previous remote",
					"suppressSeconds", RoamingSuppressSeconds,
					"udpAddr", curRemote,
					"newAddr", via.UdpAddr,
				)
			}
			return
		}

		hostinfo.logger(f.l).Info("Host roamed to new udp ip/port.",
			"udpAddr", curRemote,
			"newAddr", via.UdpAddr,
		)
		hostinfo.lastRoam = time.Now()
		hostinfo.lastRoamRemote = curRemote
		hostinfo.SetRemote(via.UdpAddr)
	}
```

**File:** hostmap.go (L777-783)
```go
func (i *HostInfo) SetRemote(remote netip.AddrPort) {
	// We copy here because we likely got this remote from a source that reuses the object
	if i.GetRemote() != remote {
		i.remote.Store(&remote)
		i.remotes.LearnRemote(i.vpnAddrs[0], remote)
	}
}
```
