## Analysis

Nebula supports multiple UDP read routines (`listen.routines`), and `f.readOutsidePackets` is invoked concurrently from these goroutines for packets landing on the same `*HostInfo`/`*ConnectionState`, since dispatch is keyed only by `h.RemoteIndex` looked up via `f.hostMap.QueryIndex` [1](#0-0) , with no per-hostinfo serialization before `Decrypt` is called [2](#0-1) .

`ConnectionState.Decrypt` implements its replay/anti-replay guard as three separate critical sections instead of one atomic check-and-set:

```go
func (cs *ConnectionState) Decrypt(l *slog.Logger, messageCounter uint64, out []byte, packet []byte, nb []byte) ([]byte, error) {
	cs.decryptLock.Lock()
	result := cs.window.Check(l, messageCounter)   // 1. CHECK (locked)
	cs.decryptLock.Unlock()
	...
	out, err = cs.dKey.DecryptDanger(...)          // 2. ACT (unlocked, expensive AEAD op)
	...
	cs.decryptLock.Lock()
	result = cs.window.Update(l, messageCounter)   // 3. COMMIT (locked)
	cs.decryptLock.Unlock()
	...
}
``` [3](#0-2) 

This is precisely the "approve()" bug class from the report translated to a security-state machine: a **check** against shared mutable state, a **read-your-write gap** (the AEAD decrypt work) where no lock is held, and a **commit** that finally mutates the state — with the mutation deferred until after the expensive operation. `VerifyRelay` has the identical pattern for relay frames [4](#0-3) .

Between step 1 and step 3, the replay window (`Bits`) has *not yet marked* `messageCounter` as seen. `Bits.Check` only inspects state; it never mutates `b.bits` [5](#0-4) . If two goroutines receive the same authenticated ciphertext (a duplicate on-wire packet — trivial to produce by re-sending a captured UDP datagram, no valid CA cert needed since the packet is already validly encrypted/authenticated by the legitimate peer) at nearly the same time, both can call `Check` and get `true` before either calls `Update`. Both then proceed to decrypt and deliver the same payload to the tun device / firewall path, i.e. the replay window is bypassed for a duplicate frame — the network analog of "N tokens spent twice" in the `approve()` frontrun: the authorization check (`Check`) is read and acted upon before the corresponding debit (`Update`) is committed, so two concurrent operations can each observe the pre-commit state and both succeed.

This is reachable by an unauthenticated on-path/off-path attacker: they need only capture and duplicate a single legitimate UDP datagram (standard replay, no cert forging or CA trust required) and race it against `listen.routines > 1`. The changelog itself documents that the team has fixed adjacent replay-window races before ("Lock replay window updates so concurrent readers can't corrupt it." #1802; "Advance the replay window on relayed packets..." #1751) [6](#0-5) , showing this is a recognized class of bug in this exact subsystem, but the current `Decrypt`/`VerifyRelay` split-lock check-then-commit pattern still leaves the described race open.

### Title
Time-of-check/time-of-use race in `ConnectionState.Decrypt`/`VerifyRelay` allows replay-window bypass under concurrent packet processing - (File: `connection_state.go`)

### Summary
`ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` split the anti-replay check (`window.Check`) and the anti-replay commit (`window.Update`) into two separate lock acquisitions, with the expensive AEAD decrypt/verify operation running unlocked in between. This mirrors the ERC20 `approve()` frontrunning bug class: a permission/state check is read, acted upon, and only later committed, leaving a window where two concurrent operations can both pass the check before either commits.

### Finding Description
`Decrypt` locks `decryptLock`, calls `cs.window.Check(l, messageCounter)`, unlocks, performs `DecryptDanger`, then re-locks and calls `cs.window.Update(l, messageCounter)` to actually mark the counter as seen [3](#0-2) . `Bits.Check` is a pure read that never mutates the bitmap [5](#0-4) ; only `Bits.Update` marks the slot as consumed [7](#0-6) . Because nebula can run multiple UDP read routines that all dispatch into `f.readOutsidePackets` and then `hostinfo.ConnectionState.Decrypt` for packets resolved to the same `HostInfo` via `QueryIndex` [8](#0-7) , two threads receiving a duplicated ciphertext (an attacker-replayed UDP datagram) can each execute `Check` and get a pass before either has executed `Update`, since the mutation is deferred past the unlocked decrypt step. `VerifyRelay`, used for relay-forwarded frames, has the same three-step structure [4](#0-3) .

### Impact Explanation
A successful race allows a replayed (previously-seen, encrypted) packet to be decrypted and delivered twice — bypassing the anti-replay protection that is supposed to guarantee at-most-once delivery per message counter. For relayed traffic this also means a relay could double-forward a frame it should have dropped, matching the historical bug fixed in #1751 for a different code path. This is a concrete traffic-replay/duplication impact within the accepted categories (traffic replay bypass), though it requires favorable multi-routine scheduling and does not on its own decrypt new content (the attacker still needs a captured legitimate ciphertext).

### Likelihood Explanation
Likelihood is limited by the narrow race window (an AEAD decrypt call) and by the requirement that `listen.routines` be configured greater than 1 so packets for the same tunnel can be processed by different goroutines concurrently, and that a captured duplicate datagram be redelivered within that same short window; this is a low-probability, opportunistic race rather than a reliably-reproducible bypass.

### Recommendation
Perform the replay-window check-and-mark as a single atomic operation under one lock acquisition (combine `Check`+`Update` into one locked call that both validates and marks the counter, aborting decrypt if the slot is already/being consumed), analogous to requiring an atomic "compare-and-swap" instead of check-then-later-commit, the same fix pattern recommended for the `approve()` bug (require the pre-state to match atomically with the state transition).

### Proof of Concept
1. Establish a tunnel with `listen.routines` set > 1.
2. Capture one legitimate encrypted UDP data packet destined for a host.
3. Fire two (or more) copies of that exact packet at the receiver in rapid succession so they land on different UDP read routines.
4. Observe that under unfavorable (attacker-favorable) scheduling, both copies can pass `cs.window.Check` before either calls `cs.window.Update`, resulting in the duplicate being decrypted and delivered to the tun device / relay-forward path instead of being rejected as `ErrAlreadySeen`.

### Citations

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

**File:** CHANGELOG.md (L79-81)
```markdown
- Advance the replay window on relayed packets so a relay drops replayed frames instead of re-forwarding them. (#1751)
- Fix a race in relay state handling. (#1753)
- Lock replay window updates so concurrent readers can't corrupt it. (#1802)
```
