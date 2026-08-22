Confirmed: `readOutsidePackets` is invoked concurrently from multiple `listenOut` goroutines (one per `routines` reader) via `li.ListenOut(...)`, and each calls `hostinfo.ConnectionState.Decrypt(...)` for whatever `HostInfo` the packet's `RemoteIndex` maps to [1](#0-0) . Since a single peer's `HostInfo`/`ConnectionState` can receive packets on any reader thread, two UDP datagrams carrying the same `MessageCounter` (i.e., a captured-and-replayed packet racing against the original) can be processed by two different goroutines concurrently and both reach `Decrypt`.

### Title
Anti-replay window check/update race allows duplicate decryption of a replayed packet - (File: connection_state.go)

### Summary
`ConnectionState.Decrypt` checks the replay window, releases the lock, performs AEAD decryption, then re-acquires the lock to mark the counter as seen. The check and the "commit" of state are not atomic, so a duplicated (replayed) packet with the same message counter can pass the `Check` before the original packet's `Update` completes, letting an off-path attacker who captures and replays a single valid ciphertext get it decrypted and delivered to the tun device twice — despite the code's dedicated anti-replay bitmap.

### Finding Description
`Decrypt` is structured as: lock → `window.Check` → unlock → `dKey.DecryptDanger` (no lock) → lock → `window.Update` → unlock [2](#0-1) . The `Bits.Check`/`Bits.Update` pair is the sole state that prevents replay, and `Update` is what actually marks a counter as consumed (`b.set(i)` / bit tests) [3](#0-2) [4](#0-3) . Because the decrypt step (the expensive part) happens *outside* the lock and *before* the window is updated, this mirrors the StRSR bug class exactly: an action that should be authorized only once state is durably updated is instead permitted to proceed based on a stale check, and the "commit" of that state happens too late to prevent a second, concurrent actor from exploiting the same window.

Nebula runs multiple reader goroutines per socket when `listen.routines`/`tun.routines` > 1 (`f.routines`), each independently calling `readOutsidePackets` → `ConnectionState.Decrypt` for whatever `HostInfo` a given packet's `RemoteIndex` resolves to [1](#0-0) [5](#0-4) . There is no per-message-counter serialization; the `decryptLock` mutex only protects the individual `Check`/`Update` calls, not the overall check-decrypt-commit sequence [2](#0-1) .

An on-path attacker (who can observe/capture nebula UDP traffic — no valid certificate is required, since the check happens on an already-established connection identified only by `RemoteIndex`) can duplicate a single genuine ciphertext datagram back at the target host in rapid succession across the receive queues. If both duplicates land on different reader goroutines close enough in time, both can pass `Check` (both see `i` as not-yet-marked), both independently decrypt successfully (AEAD decryption with a valid, previously-unused nonce/counter always succeeds deterministically regardless of how many times it's replayed), and both get delivered to `handleOutsideMessagePacket`/tun before either `Update` call commits the bit. Only after both decrypts do the two `Update` calls race for the same bit — but the packet has already been decrypted and forwarded twice by that point.

### Impact Explanation
This breaks Nebula's guaranteed replay protection for the data-plane traffic (`Bits`, `ReplayWindow`), which is central to preventing traffic-replay attacks in the mesh — a core security property listed among the accepted analog impacts ("traffic decryption/forgery/replay"). Concretely, replaying a captured legitimate packet (e.g., a command, a DNS query, a state-changing UDP payload) can cause it to be delivered to the tun device / application twice, defeating the intended one-time-delivery guarantee, similar in spirit to the StRSR issue where an action was allowed to proceed based on state that had not yet been committed. The severity depends on how sensitive the tunneled traffic is to duplicate delivery (e.g., duplicate application-layer side effects), and on the attacker's ability to win the race between two reader goroutines, which requires `routines > 1` and requires the attacker to time two copies of a captured packet closely.

### Likelihood Explanation
Likelihood is Low-to-Medium: it requires (1) `listen.routines`/multiqueue enabled so more than one goroutine can call `Decrypt` concurrently for the same `HostInfo`, (2) the attacker being able to capture and immediately duplicate a valid ciphertext to the target's listener (feasible for any on-path/off-path attacker who can inject UDP packets to the victim, since no cert/auth gate applies at this layer — only the `RemoteIndex` and AEAD tag are checked), and (3) winning a narrow race window between `Check` and `Update` across two goroutines. This is analogous to the original finding's caveat that impact depends on "liveliness" — here it depends on packet timing/scheduling rather than reward-period timing.

### Recommendation
Hold the `decryptLock` for the entire check-decrypt-commit sequence (or otherwise make `Check`+`Decrypt`+`Update` atomic per `ConnectionState`), so that a second copy of the same counter cannot pass `Check` until the first copy's `Update` has committed. This mirrors the analog recommendation of ensuring state is synchronized (`poke()`/reward payout) *before* allowing the dependent action to complete.

### Proof of Concept
Conceptual PoC (not run, since this requires precise goroutine scheduling to reproduce reliably):
1. Configure two Nebula nodes with `routines: 2` (or greater) so multiple reader goroutines can service the same UDP socket concurrently.
2. Establish a tunnel between the nodes and capture one legitimate encrypted `header.Message` packet (know its `RemoteIndex`/`MessageCounter`).
3. From an attacker position capable of injecting UDP to the victim's listener, send two copies of the exact same captured ciphertext back-to-back so they are likely to be picked up by two different `recvmmsg`/`ListenOut` reader routines nearly simultaneously.
4. Instrument (or add a test using `connection_state_test.go`-style unit test) two goroutines both calling `Decrypt` with the same `messageCounter`/ciphertext concurrently, observing that both can return `err == nil` (i.e., both succeed) rather than the second returning `ErrAlreadySeen`, confirming state is not synchronized as an atomic gate on the decrypt operation [2](#0-1) .

### Citations

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

**File:** outside.go (L89-132)
```go
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
	}

	out, err = hostinfo.ConnectionState.Decrypt(f.l, h.MessageCounter, out, packet, nb)
	if err != nil {
		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			hostinfo.logger(f.l).Debug("Failed to decrypt packet", "error", err, "from", via, "header", h)
		}
		return
	}
```
