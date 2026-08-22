### Title
Anti-replay window Check/Update race in `ConnectionState.Decrypt` allows message replay across concurrent UDP reader queues - (File: connection_state.go)

### Summary
The external report describes an accounting/consistency bug where concurrent state updates (reward claims vs. balance reads) can be observed in an inconsistent, exploitable state because the check and the update of accounted values are not atomic. The reachable analog in this repo is `ConnectionState.Decrypt`, where the anti-replay `Bits` window is checked and then updated as two separate, non-atomic critical sections, allowing an attacker to replay a captured data-plane packet through the concurrent multi-queue UDP reader and have it accepted (and decrypted) more than once.

### Finding Description
`Decrypt` protects the replay window with `decryptLock`, but it takes and releases the lock twice, with the AEAD decrypt operation running in between while the lock is not held: [1](#0-0) 

```
cs.decryptLock.Lock()
result := cs.window.Check(l, messageCounter)
cs.decryptLock.Unlock()
...
out, err = cs.dKey.DecryptDanger(...)
...
cs.decryptLock.Lock()
result = cs.window.Update(l, messageCounter)
cs.decryptLock.Unlock()
```

`Bits.Check` only inspects the sliding-window bitmap without marking the counter as seen; the counter is only actually recorded by `Bits.Update`, which runs later: [2](#0-1) 

Nebula spawns one `listenOut` goroutine per UDP reader queue, and each dispatches inbound packets independently to `readOutsidePackets`, which calls `hostinfo.ConnectionState.Decrypt` for the same `HostInfo`/`ConnectionState` without any per-packet-counter serialization beyond the `Bits.Check`/`Bits.Update` split: [3](#0-2) [4](#0-3) 

If two copies of the same encrypted packet (same `messageCounter`) arrive on different reader queues (or are duplicated on the wire and land in different UDP receive batches), both goroutines can call `Check` before either calls `Update` — both see the counter as "not yet seen," both proceed to `DecryptDanger` and succeed (the AEAD tag/nonce is valid because it's a legitimate previously-sent ciphertext), and only after that do they race on `Update`, where the second call is silently dropped as a duplicate. By that point the packet has already been decrypted and handed to the application/message-processing path twice.

### Impact Explanation
This is a concrete traffic-replay bypass: the same authenticated data-plane message (e.g., a `header.Message`, `header.LightHouse`, or `header.Test` payload) can be delivered to the tunnel consumer more than once despite the anti-replay `Bits` window existing specifically to prevent this. Depending on payload semantics this can cause duplicate application-level effects (e.g., duplicate delivery on the tun device), which is the same class of impact the external report flags (state that should be uniquely accounted for is instead double-counted due to a non-atomic check-then-update).

### Likelihood Explanation
Nebula listens on multiple UDP reader queues by design (`listenOut(i)` for `i` in `[0, numWorkers)`), so genuinely concurrent delivery paths for packets addressed to the same host already exist. An attacker does not need a CA-signed certificate — this is purely a race on decryption/replay bookkeeping triggered by delivering (or having the network deliver, e.g., via duplicate transmission at lower layers) two copies of a previously captured ciphertext to different queues within the small window between `Check` and `Update`. The race window is small but the report explicitly notes such issues "increase" in impact as load/traffic increases, which raises likelihood under load (multiple queues, high throughput).

### Recommendation
Merge the check-and-mark operation into a single atomic critical section under `decryptLock` (i.e., hold the lock across `Check`, `DecryptDanger`, and `Update`, or restructure `Bits` to expose a single `CheckAndReserve`/`TestAndSet`-style call that marks the counter as consumed before the expensive decrypt is attempted, then rolls back on decrypt failure). This removes the window during which two goroutines can both observe "not yet seen" for the same counter.

### Proof of Concept
1. Establish a tunnel between two nebula nodes with `listen.routines` (multi-queue) enabled so `listenOut` spawns more than one reader goroutine.
2. Capture a valid encrypted data-plane packet (`header.Message`) with counter `N`.
3. Simultaneously replay two copies of the exact same packet to the receiver such that they are handled by two different reader queues (e.g., using SO_REUSEPORT duplication or sending on two different threads with tight timing, or simply flooding the socket so the kernel delivers both to different queues in the same instant).
4. Both goroutines call `hostinfo.ConnectionState.Decrypt(f.l, N, ...)` concurrently; both call `cs.window.Check(l, N)` before either calls `cs.window.Update(l, N)`, so both see `true` and proceed to decrypt successfully, resulting in the same message being processed twice by `handleOutsideMessagePacket`/tun write path.

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
