The strongest reachable analog is the check-then-act race in `ConnectionState.Decrypt` / `VerifyRelay` (`connection_state.go`), reached via `readOutsidePackets` in `outside.go`, which multiple concurrent underlay reader goroutines (`f.routines > 1`, see `interface.go:273-286`) can execute in parallel against the same `hostinfo.ConnectionState`.

### Title
Replay-window check/decrypt/update race allows duplicate AEAD decryption of a captured ciphertext before the replay window is updated - (File: connection_state.go)

### Summary
`ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` split the anti-replay logic into three separate lock acquisitions: check the replay window, perform the (comparatively expensive) AEAD decrypt/verify outside the lock, then re-acquire the lock to mark the counter as seen. This mirrors the reported Pod.sol pattern (state check → external/expensive operation → state mutation), where the state-advancing mutation happens only after an unlocked, attacker-triggerable operation.

### Finding Description
`Decrypt` performs:
1. `cs.decryptLock.Lock(); result := cs.window.Check(...); cs.decryptLock.Unlock()` [1](#0-0) 
2. `cs.dKey.DecryptDanger(...)` outside the lock [2](#0-1) 
3. `cs.decryptLock.Lock(); result = cs.window.Update(...); cs.decryptLock.Unlock()` [3](#0-2) 

`VerifyRelay` follows the identical Check → decrypt → Update pattern for relayed frames. [4](#0-3) 

Because `Check` (non-mutating) and `Update` (mutating) are two independent, separately-locked operations rather than one atomic "check-and-mark," two packets carrying the same `MessageCounter` can both pass `Check` before either has called `Update`. This is reachable by an outside attacker who simply captures one legitimate ciphertext packet on the wire and re-transmits it: no valid certificate or key material is required, since decryption is driven entirely by the receiver's own `hostinfo.ConnectionState`, and the packet path is dispatched from `readOutsidePackets`, which multiple concurrent underlay-reader goroutines invoke when `routines > 1` (`f.routines` is configurable and defaults >1 on supporting platforms). [5](#0-4) [6](#0-5)  Each `listenOut` goroutine runs its own independent read loop feeding `readOutsidePackets` for the same `hostinfo`. [7](#0-6) 

The design intent, per the changelog, was explicitly to make replay-window updates atomic against concurrent processing ("Lock replay window updates so concurrent readers can't corrupt it," and "Advance the replay window on relayed packets so a relay drops replayed frames instead of re-forwarding them"), confirming this exact code path is a known source of prior replay-related races. [8](#0-7) 

### Impact Explanation
A remote network attacker who can capture ciphertext (no cert, no keys) can replay a captured packet multiple times, racing the same `MessageCounter` across concurrent reader goroutines. Both racing calls pass `Check`, and both perform the costly AEAD open (`DecryptDanger`) concurrently before `Update` finally rejects the loser as a duplicate. This wastes CPU cycles proportional to the number of raced copies per legitimate packet, amplifying attacker-controlled replay traffic into duplicated decrypt work on the victim — a computational amplification / minor DoS effect. Because the final `Update` call still gates whether the caller in `outside.go` proceeds to `handleOutsideMessagePacket`/tun delivery or relay forwarding, this race does not, based on the code reviewed, allow the duplicate packet to be delivered twice to the tun device or the relay target; only one call ultimately returns success from `Decrypt`/`VerifyRelay`. I could not fully verify, within the available context, whether any downstream effect (e.g., `f.connectionManager.In(hostinfo)` or `handleHostRoaming`) is invoked prior to the final Update check in a way that could be exploited beyond CPU amplification — this would require examining the full call ordering under actual concurrent execution and is flagged as uncertain.

### Likelihood Explanation
Likelihood is high for triggering the race window itself (an attacker only needs to fire the same captured ciphertext rapidly toward the target, ideally via different attacker-side source ports to influence kernel-level UDP queue distribution across `routines`), but likelihood of a security-significant outcome (beyond amplification) is currently unconfirmed based on the code reviewed.

### Recommendation
Make the replay-window check-and-mark atomic with respect to decryption: either hold `decryptLock` for the entire Check→Decrypt→Update sequence (accepting the serialization cost), or use a compare-and-swap/two-phase reservation scheme where `Check` provisionally reserves the counter (rejecting concurrent duplicates immediately) and only rolls back the reservation if decryption subsequently fails, rather than allowing multiple concurrent decrypt attempts for the same counter before any state mutation occurs.

### Proof of Concept
1. Establish a tunnel with `routines` > 1 configured on the receiver (multi-queue enabled: `interface.go` `run()` launches one `listenOut` goroutine per routine). [9](#0-8) 
2. Capture one legitimate encrypted `Message` (or relay) packet destined for the receiver.
3. Rapidly re-inject (replay) many copies of the identical captured UDP payload toward the receiver, varying the attacker's local source port per copy to increase the chance the kernel/socket dispatch routes copies to distinct reader queues/goroutines.
4. Observe (e.g., via profiling/metrics on `dKey.Overhead()`/AEAD open calls or CPU usage) that the same `MessageCounter` is decrypted more than once concurrently before `cs.window.Update` rejects the duplicates, confirming the check-then-act race in `ConnectionState.Decrypt`/`VerifyRelay`.

### Citations

**File:** connection_state.go (L61-68)
```go
func (cs *ConnectionState) Decrypt(l *slog.Logger, messageCounter uint64, out []byte, packet []byte, nb []byte) ([]byte, error) {
	var err error
	cs.decryptLock.Lock()
	result := cs.window.Check(l, messageCounter)
	cs.decryptLock.Unlock()
	if !result {
		return nil, ErrAlreadySeen
	}
```

**File:** connection_state.go (L70-73)
```go
	out, err = cs.dKey.DecryptDanger(out, packet[:header.Len], packet[header.Len:], messageCounter, nb)
	if err != nil {
		return nil, err
	}
```

**File:** connection_state.go (L75-80)
```go
	cs.decryptLock.Lock()
	result = cs.window.Update(l, messageCounter)
	cs.decryptLock.Unlock()
	if !result {
		return nil, ErrAlreadySeen
	}
```

**File:** connection_state.go (L84-107)
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
```

**File:** interface.go (L273-286)
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

**File:** CHANGELOG.md (L79-81)
```markdown
- Advance the replay window on relayed packets so a relay drops replayed frames instead of re-forwarding them. (#1751)
- Fix a race in relay state handling. (#1753)
- Lock replay window updates so concurrent readers can't corrupt it. (#1802)
```
