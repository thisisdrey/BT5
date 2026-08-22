### Title
Non-atomic Check/Update in `ConnectionState.Decrypt` lets a spoofed replay race the legitimate packet and poison `HostInfo.remote` - ([File: connection_state.go])

### Summary
`ConnectionState.Decrypt` performs the anti-replay `Check`, the AEAD decrypt, and the anti-replay `Update` as three separate critical sections instead of one atomic operation, with the (relatively expensive) AEAD decrypt happening while the replay-window lock is released. Because Nebula runs multiple UDP reader goroutines/sockets per interface, an attacker who has observed one valid ciphertext for a tunnel and can spoof the UDP source address can inject that captured packet on a different reader queue so that its `Update` call commits the message-counter bit before the genuine packet's `Update` runs, causing `handleHostRoaming`/`HostInfo.SetRemote` to bind the tunnel's remote address to the attacker's spoofed `via.UdpAddr` while the real packet is discarded as a duplicate.

### Finding Description
`readOutsidePackets` (outside.go:126-136) calls `hostinfo.ConnectionState.Decrypt` and, only if it returns no error, immediately calls `f.handleHostRoaming(hostinfo, via)`, which can call `hostinfo.SetRemote(via.UdpAddr)` [1](#0-0) [2](#0-1) .

`Decrypt` itself is not atomic:
```go
cs.decryptLock.Lock()
result := cs.window.Check(l, messageCounter)
cs.decryptLock.Unlock()
...
out, err = cs.dKey.DecryptDanger(...)   // runs WITHOUT the lock held
...
cs.decryptLock.Lock()
result = cs.window.Update(l, messageCounter)
cs.decryptLock.Unlock()
``` [3](#0-2) 

`Check` only reads window state, it does not reserve the counter [4](#0-3) . Two concurrent calls for the exact same `messageCounter` can both pass `Check` (since neither has yet set the bit), both successfully run `DecryptDanger` (AEAD authentication does not bind the UDP source address — it only authenticates `packet[:header.Len]` and the ciphertext, neither of which include the sender's IP/port), and then race on `Update`. Only the call whose `Update` executes first wins (`Update`'s fast path sets the bit unconditionally and returns `true`; the loser's later call is detected as a duplicate and returns `false`, yielding `ErrAlreadySeen`) [5](#0-4) [6](#0-5) .

Nebula runs one reader goroutine per queue/socket (`f.routines`), each independently invoking `readOutsidePackets` (via `listenOut`/`ListenOut`) [7](#0-6) . Kernel flow-hashing (e.g., `SO_REUSEPORT`) routes packets with different source IP/port 4-tuples to different queues, so a spoofed replay with a different (attacker-chosen) source address is very likely to land on a different goroutine than the genuine sender's packet, giving real concurrency for the race described above, not just a single-threaded ordering.

If the attacker's replayed copy wins the `Update` race, `handleHostRoaming` treats `via.UdpAddr` (the attacker's spoofed address) as the new authenticated peer address (subject only to the lighthouse `remote_allow_list`, which is permissive by default, and the 2-second roam-suppression window that only blocks roaming *back* to a previously-suppressed address) and calls `hostinfo.SetRemote(via.UdpAddr)` → `RemoteList.LearnRemote` [8](#0-7) [9](#0-8) . The genuine packet, having lost the `Update` race, is silently dropped as a replay.

This breaks the intended invariant that peer addressing changes only on cryptographically verified traffic *from that specific source*: successful decryption is treated as proof that the current UDP source address is the legitimate peer, but decryption success alone (on a captured, replayed ciphertext) proves nothing about who currently controls that address — it only proves the ciphertext was validly produced at some point in the past.

### Impact Explanation
A successful race lets an unprivileged network-adjacent/on-path attacker redirect a live Nebula tunnel's learned remote address to an IP:port they control, diverting subsequent outbound traffic for that tunnel to the attacker (traffic redirection) and simultaneously dropping the genuine packet (tunnel disruption/takedown potential if repeated). This matches the "roaming/address poisoning" scoped impact called out in the question.

### Likelihood Explanation
Exploitation requires: (1) the attacker to have previously observed one valid ciphertext for the target tunnel (network sniffing / on-path position — a capability the question grants as a precondition, not full MITM or key compromise), (2) the ability to spoof the UDP source address, and (3) winning a tight race against the genuine packet's processing (the same message counter must not have already completed `Update` on the defender side). This is a narrow but real timing window, made more feasible because per-queue socket reader goroutines run truly concurrently and OS flow-hashing tends to route a differently-addressed spoofed packet to a different reader than the genuine flow, removing false serialization that a single-threaded model might otherwise provide. It is not reliably exploitable on every attempt, but it is repeatable given sufficiently precise timing/injection, e.g. in a scripted PoC that controls both packet deliveries.

### Recommendation
Make the replay-window reservation and the AEAD decrypt atomic with respect to a given message counter: hold `decryptLock` (or a per-counter reservation) across the full `Check` → `DecryptDanger` → `Update` sequence for a given `ConnectionState`, or restructure so a counter is provisionally marked "in flight" under `Check` and only released/confirmed after decrypt succeeds, ensuring at most one decrypt attempt per counter can ever complete regardless of goroutine interleaving. Additionally, consider not treating decrypt success alone as sufficient to roam remote addressing; e.g., require a small number of consecutive/consistent packets from the new address, or bind the roaming decision to a monotonic in-order counter check rather than any counter that merely "wins" the window race.

### Proof of Concept
Integration test plan:
1. Establish a real tunnel between `A` and `B`; capture one legitimate ciphertext packet `A→B` (via `GetFromUDP`/packet capture in the e2e harness, similar to `TestRelayReplayProtection`).
2. Set up two `Control`s so `A`'s reader queue is different from an injected spoofed sender queue (or directly call `Interface.readOutsidePackets`/`ConnectionState.Decrypt` from two goroutines) to simulate concurrency: goroutine 1 processes the genuine packet with source `A`'s real `UdpAddr`; goroutine 2 concurrently injects the identical captured ciphertext with a spoofed `via.UdpAddr` set to an attacker-controlled `netip.AddrPort`.
3. Synchronize the two goroutines so both call `ConnectionState.Decrypt` with the same `messageCounter`, with the spoofed goroutine's `Update` call artificially given priority (or run many trials to observe the race), and assert:
   - At most one of the two calls returns `nil` from `Decrypt` (replay protection holds for `Update`).
   - When the spoofed goroutine wins, assert `hostInfo.GetRemote()` becomes the attacker's spoofed address (demonstrating `SetRemote` was invoked on unauthenticated-source traffic) instead of remaining `A`'s real address, and that the genuine packet was dropped with `ErrAlreadySeen`.
4. Expected (fixed) behavior: `SetRemote`/roaming must never be triggered by a packet whose message counter did not win reservation *before* the AEAD decrypt runs, i.e., the winning `via.UdpAddr` must correspond to a `Check`+reservation that happened atomically prior to decrypting, preventing a same-counter race outcome from being source-dependent.

### Citations

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

**File:** outside.go (L264-294)
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

}
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

**File:** bits.go (L168-186)
```go
func (b *Bits) Update(l *slog.Logger, i uint64) bool {
	// Fast path: i is the next expected counter. Split out so the function
	// stays small and avoids paying for the slow paths' slog argument-build
	// stack frame on every call. The bit read/test/write is inlined to
	// touch the backing word once.
	if i == b.current+1 {
		pos := i & b.lengthMask
		word := pos >> 6
		mask := uint64(1) << (pos & 63)
		w := b.bits[word]
		if i > b.length && w&mask == 0 {
			b.lostCounter.Inc(1)
		}
		b.bits[word] = w | mask
		b.current = i
		return true
	}
	return b.updateSlow(l, i)
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
