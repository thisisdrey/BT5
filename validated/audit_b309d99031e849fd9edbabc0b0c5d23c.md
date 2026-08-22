Confirmed: `f.run()` launches `f.routines` concurrent goroutines each calling `listenOut(i)` → `readOutsidePackets` → `ConnectionState.Decrypt` for the *same* `HostInfo`/`ConnectionState` concurrently. This matches the "two different states processed non-atomically in the same execution window" bug class from the Pyth report.

### Title
Non-atomic check-then-decrypt-then-update replay window allows concurrent decrypt of duplicate/racing ciphertexts before dedup is enforced - (`connection_state.go`)

### Summary
`ConnectionState.Decrypt` (and `VerifyRelay`) split the anti-replay window operation into three separate, individually-locked steps: `window.Check()`, the actual AEAD `DecryptDanger()`, and `window.Update()`. The lock is released between `Check` and the decrypt call, and re-acquired only for `Update`. Because Nebula runs multiple UDP reader goroutines (`f.routines`, see `Interface.run()`/`listenOut`) that can deliver packets destined for the same `HostInfo` concurrently, this is a classic check-then-act (TOCTOU) window, structurally the same root cause as the Pyth oracle bug: state that should be validated and consumed atomically is instead read, acted upon, and re-validated in separate steps, letting multiple values race through the middle "act" stage before the final write settles which one is accepted.

### Finding Description
`Decrypt` is:
```go
cs.decryptLock.Lock()
result := cs.window.Check(l, messageCounter)
cs.decryptLock.Unlock()
...
out, err = cs.dKey.DecryptDanger(...)   // no lock held here
...
cs.decryptLock.Lock()
result = cs.window.Update(l, messageCounter)
cs.decryptLock.Unlock()
``` [1](#0-0) 

`readOutsidePackets` is invoked from `f.listenOut(i)` for `i` in `[0, f.routines)`, each running as its own goroutine reading from its own UDP writer/socket queue, all feeding the same `hostinfo.ConnectionState` when packets for that tunnel arrive on different queues: [2](#0-1) [3](#0-2) 

Because the lock is dropped between `Check` and `Update`, two packets carrying the same (or overlapping) `messageCounter` — e.g. a genuine packet and an attacker-replayed/duplicated copy of its ciphertext captured off the wire — can both pass `Check` (neither has marked the bit yet) and both proceed to run the expensive AEAD decrypt operation concurrently. Only at `Update` time is the duplicate finally rejected. This mirrors the oracle bug's structure: the "read" of validity state and the "commit" of that state are not atomic, so multiple submissions can slip through the gap and be acted upon before the authoritative state is finalized.

### Impact Explanation
Unlike the Pyth arbitrage (which yields direct monetary profit because both racing values are ultimately usable), here the terminal `Update()` step does correctly reject the loser, so no packet payload is delivered twice to the tun device from a single race outcome. The realistic impact is instead a remote resource-exhaustion/amplification vector: an attacker who captures a single valid ciphertext for a live tunnel can flood duplicate copies of it across the `f.routines` UDP queues to force repeated, wasted AEAD decrypt operations (CPU cost) for every duplicate that wins the `Check` race, before being dropped at `Update`. This is a lesser impact than a full authentication/decryption bypass — the finding is not a full analog of "profit from committing two different accepted values," so its severity should be considered informational/low rather than a certificate/handshake/firewall bypass.

### Likelihood Explanation
Reachable by any attacker on the underlay network capable of sending UDP traffic to a node's listen port for an established tunnel index, with no CA-signed certificate required, since decrypt is attempted before the AEAD tag is validated (the check race happens before authentication succeeds). Triggering the race requires precise timing across the `f.routines` reader goroutines and captured ciphertext, which lowers likelihood in practice but is not prevented by any current code path.

### Recommendation
Hold `decryptLock` across the entire check → decrypt → update sequence in `Decrypt` and `VerifyRelay`, or restructure the window bookkeeping into a single atomic check-and-reserve operation (reserve the slot at `Check` time, and roll it back only if decryption subsequently fails), so no other goroutine can observe or act on an un-committed slot state.

### Proof of Concept
Not independently verified against a running binary; the race is inferable directly from the code structure (lock released between `window.Check` and `window.Update` in `connection_state.go` lines 61-82) combined with the multi-goroutine reader fan-out in `interface.go` lines 273-337, which is sufficient to demonstrate the same class of non-atomic check-then-act flaw as the reported analog, but exploitability/impact under real scheduling has not been empirically measured here.

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
