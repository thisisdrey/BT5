### Title
Replay-window check-then-act race in `ConnectionState.Decrypt`/`VerifyRelay` permits duplicate-packet processing - (File: `connection_state.go`)

### Summary
The external report's bug class is "a flawed calculation/decision routine that silently produces an incorrect security-relevant result, letting an attacker get a result the routine was designed to prevent." The closest reachable analog in this codebase is not a math-offset bug but a logically equivalent flaw in the anti-replay decision path: `Check()` (the routine that is supposed to prevent replay) and `Update()` (the routine that actually records the counter as seen) are invoked as two *separate*, individually-locked operations instead of one atomic check-and-set, so the "is this a replay" decision can be stale by the time the packet is actually accepted.

### Finding Description
`ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` both implement the same pattern: [1](#0-0) 

```
cs.decryptLock.Lock()
result := cs.window.Check(l, messageCounter)
cs.decryptLock.Unlock()
if !result { return nil, ErrAlreadySeen }

out, err = cs.dKey.DecryptDanger(...)   // no lock held here
...
cs.decryptLock.Lock()
result = cs.window.Update(l, messageCounter)
cs.decryptLock.Unlock()
if !result { return nil, ErrAlreadySeen }
```

`Bits.Check` only reads the sliding-window bitmap; `Bits.Update` is the function that actually marks a counter as consumed: [2](#0-1) [3](#0-2) 

Because the lock is released between the `Check` call and the `Update` call, two concurrent invocations of `Decrypt` (or `VerifyRelay`) for the *same* `messageCounter` on the *same* `ConnectionState` can both pass `Check` (neither has recorded the counter yet), both successfully run `DecryptDanger`, and both proceed to hand the payload up to packet processing/TUN write before either one calls `Update`. Only the first `Update` call actually records the counter; the second `Update` call correctly returns `false`, but by then the plaintext has already been decrypted and dispatched once for each racing call. This is analogous to the ELO report's core defect: the code *believes* it enforced a monotonic/one-time invariant (like the ELO formula believed its offset trick preserved the mathematical equivalence), but the decomposition into two non-atomic steps breaks the guarantee the single check was supposed to provide.

`Interface` reads inbound UDP packets across per-CPU/queue routines (`q int` is threaded through `readOutsidePackets` and used to select `f.readers[q]`), which is exactly the kind of multi-routine ingestion model that would let two copies of a replayed ciphertext (an attacker simply retransmits a previously observed valid ciphertext datagram) reach `ConnectionState.Decrypt` concurrently. [4](#0-3) [5](#0-4) [6](#0-5) 

### Impact Explanation
An unauthenticated network attacker who can capture a single valid encrypted data packet (no CA-signed certificate needed — they only need to observe wire traffic) can retransmit it multiple times in quick succession. If the retransmissions race through `Decrypt` before the first `Update` completes, the same plaintext packet can be decrypted and delivered to the TUN interface / firewall pipeline more than once, defeating the anti-replay guarantee the `Bits` window and `ReplayWindow` constant are meant to provide. This is a concrete traffic-replay impact within the allowed category ("nonce/replay handling").

### Likelihood Explanation
Exploitability depends on actually winning the race window between the `Check` unlock and the `Update` lock (which includes a full AEAD decrypt), which narrows the timing but is not infeasible for an attacker who can send bursts of duplicate UDP datagrams timed to land on different processing queues/goroutines. This is a real logic gap (non-atomic check-then-act) rather than a purely theoretical one, but requires precise timing/concurrency, so likelihood is moderate rather than trivial.

### Recommendation
Make the replay check-and-record atomic: hold `decryptLock` across the entire `Check` → `DecryptDanger` → `Update` sequence (or fold `Check`+`Update` into a single locked operation performed immediately before/after decryption, with rollback of the window state only on decrypt failure). Alternatively, reserve the counter slot in `Update` first (marking it consumed) and only decrypt afterward, rejecting if the AEAD open fails, so no two goroutines can ever pass the "not yet seen" gate for the same counter.

### Proof of Concept
Conceptually:
1. Attacker captures one valid ciphertext packet `P` (with `messageCounter = N`) sent between two legitimate Nebula peers.
2. Attacker sends two copies of `P` to the receiver in immediate succession, timed to land on two different reader routines/queues.
3. Goroutine A calls `window.Check(N)` → true, releases lock.
4. Before goroutine A calls `window.Update(N)`, goroutine B calls `window.Check(N)` → still true (not yet marked), releases lock.
5. Both A and B successfully `DecryptDanger` the same ciphertext and proceed to deliver the plaintext (e.g., write to TUN or process as a lighthouse/control message) before either calls `Update`.
6. Only one `Update(N)` call actually records the counter; the packet has nonetheless been processed twice, demonstrating a successful replay despite the anti-replay window being "checked."

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

**File:** outside.go (L25-26)
```go
func (f *Interface) readOutsidePackets(via ViaSender, out []byte, packet []byte, h *header.H, fwPacket *firewall.Packet, lhf *LightHouseHandler, nb []byte, q int, localCache firewall.ConntrackCache) {
	err := h.Parse(packet)
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

**File:** outside.go (L492-520)
```go
func (f *Interface) handleOutsideMessagePacket(hostinfo *HostInfo, out []byte, packet []byte, fwPacket *firewall.Packet, nb []byte, q int, localCache firewall.ConntrackCache) {
	err := newPacket(out, true, fwPacket)
	if err != nil {
		hostinfo.logger(f.l).Warn("Error while validating inbound packet",
			"error", err,
			"packet", out,
		)
		return
	}

	dropReason := f.firewall.Drop(*fwPacket, true, hostinfo, f.pki.GetCAPool(), localCache)
	if dropReason != nil {
		// NOTE: We give `packet` as the `out` here since we already decrypted from it and we don't need it anymore
		// This gives us a buffer to build the reject packet in
		f.rejectOutside(out, hostinfo.ConnectionState, hostinfo, nb, packet, q)
		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			hostinfo.logger(f.l).Debug("dropping inbound packet",
				"fwPacket", fwPacket,
				"reason", dropReason,
			)
		}
		return
	}

	_, err = f.readers[q].Write(out)
	if err != nil {
		f.l.Error("Failed to write to tun", "error", err)
	}
}
```
