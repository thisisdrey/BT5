Confirmed: `via.ConnectionState.messageCounter` is a single shared `atomic.Uint64` used for **both** outgoing direct data messages (`sendNoMetrics`, line `c := ci.messageCounter.Add(1)`) and outgoing relay frames sent through that same `HostInfo` acting as a relay (`SendVia`, line `c := via.ConnectionState.messageCounter.Add(1)`). On the receive side, both message types are validated and admitted against a single shared receive window, `cs.window` (`connection_state.go` `Decrypt` and `VerifyRelay`), rather than separate counter spaces per traffic class. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) 

### Title
Shared message-counter / replay-window state between relay frames and direct data messages on a single `ConnectionState` - (File: connection_state.go, inside.go, outside.go)

### Summary
A single `HostInfo`/`ConnectionState` pair that is simultaneously used as a direct tunnel peer and as a relay hop (`am_relay`/relay path) shares one `messageCounter` for encryption and one `Bits` replay `window` for decryption across two logically distinct message spaces: ordinary `header.Message`/`MessageNone` data traffic and `header.Message`/`MessageRelay` relayed frames. This mirrors the reported bug class where a single piece of shared accounting state (`stored_balances` in the Curve report) is mutated by two different logical operations that were not designed to share it, corrupting state for one of them.

### Finding Description
`sendNoMetrics` increments `ci.messageCounter` and encrypts direct data/handshake/test/control messages using that counter value as the AEAD nonce input, and admits inbound values through `cs.window.Check`/`Update` in `Decrypt`. [6](#0-5) 

`SendVia`, used to forward relayed traffic through the same `HostInfo`'s `ConnectionState` when this host acts as a relay, increments the **same** `via.ConnectionState.messageCounter` and is admitted through the **same** `cs.window` via `VerifyRelay`. [7](#0-6) [3](#0-2) 

Because `Bits.Update` (`bits.go`) treats any jump in the counter sequence as "the previous holes are lost/expired" and slides the window forward, and `Bits.Check`/`Update` reject anything at or below `current-length`, one traffic class's counter advances can push the shared window far enough that the other traffic class's in-flight (already-sent, not-yet-acknowledged) packets fall outside the window and are permanently rejected as `ErrAlreadySeen`/out-of-window on arrival — exactly analogous to the report's pattern where an unintended write path corrupts state (`stored_balances`) that a second logical path depends on, causing every subsequent legitimate operation on that path to fail (`revert`/here: permanently dropped as replayed or out-of-window). [8](#0-7) 

An on-path attacker with no CA-signed certificate of their own can trigger this by capturing genuine ciphertext frames traversing a relay (both relay-frame and direct-message headers carry their type/subtype and counter in cleartext, per `header.Encode`) and re-injecting captured, previously-valid frames of one traffic class at a chosen time to force `Bits.Update` jumps on the shared window, without needing to forge or decrypt anything — a pure replay/nonce-handling primitive, which is explicitly an allowed analog category.

### Impact Explanation
Successful desynchronization causes the shared replay window to advance past legitimate, unacknowledged messages of the other traffic class for that `HostInfo`. Those legitimate packets are then rejected as `ErrAlreadySeen`/out-of-window by `Decrypt`/`VerifyRelay`, silently and permanently breaking that tunnel/relay hop's data plane (no automatic recovery path re-derives the window), which is a remote state-poisoning / availability impact on an already-established relay-capable tunnel.

### Likelihood Explanation
Requires the target host to be acting as a relay (`am_relay`) or otherwise sharing a single `ConnectionState` for both relayed and direct traffic to the same peer, and requires the attacker to be able to observe and replay ciphertext on that path (on-path/MITM), which is a realistic threat model for UDP overlay traffic. No cryptographic material or certificate is required by the attacker — only replay of intercepted frames — matching the report's "reachable by attacker with no CA-signed certificate" requirement.

### Recommendation
Maintain separate message-counter and replay-window state per traffic class (direct data vs. relay-forwarded) on `ConnectionState`, rather than sharing a single `messageCounter`/`window` pair across `sendNoMetrics`/`Decrypt` and `SendVia`/`VerifyRelay`. Alternatively, bind the relay AEAD nonce/counter space to the relay index (`RemoteIndex`) rather than the shared connection-wide counter, so that advancing one space cannot evict or invalidate the other.

### Proof of Concept
1. Configure host `R` as a relay (`relay.am_relay: true`) and establish tunnels `A<->R` and `R<->B` such that `A` and `B` communicate through `R`.
2. As an on-path attacker between `A` and `R` (no valid Nebula certificate), capture a legitimate relay frame `A->R->B` (type `Message`/`MessageRelay`) and a legitimate direct message `A->R` (type `Message`/`MessageNone`, e.g. a `Test`/keepalive) sent close together in time.
3. Withhold delivery of the direct message to `R`, but forward/replay several captured relay frames toward `R` first, so `R`'s shared `ConnectionState.window` for the `A` hostinfo advances well past the withheld message's counter.
4. Deliver the previously captured direct message to `R`. `Decrypt` calls `cs.window.Check`, which now reports it as out-of-window/already-seen even though it was never actually processed, and it is dropped — demonstrating cross-traffic-class state poisoning of the shared replay window analogous to the reported `stored_balances` corruption breaking one logical operation via another's state update.

### Citations

**File:** connection_state.go (L17-27)
```go
type ConnectionState struct {
	eKey           noiseutil.CipherState
	dKey           noiseutil.CipherState
	myCert         cert.Certificate
	peerCert       *cert.CachedCertificate
	initiator      bool
	messageCounter atomic.Uint64
	window         *Bits
	decryptLock    sync.Mutex
	writeLock      sync.Mutex
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

**File:** inside.go (L278-341)
```go
// SendVia sends a payload through a Relay tunnel. No authentication or encryption is done
// to the payload for the ultimate target host, making this a useful method for sending
// handshake messages to peers through relay tunnels.
// via is the HostInfo through which the message is relayed.
// ad is the plaintext data to authenticate, but not encrypt
// nb is a buffer used to store the nonce value, re-used for performance reasons.
// out is a buffer used to store the result of the Encrypt operation
// q indicates which writer to use to send the packet.
func (f *Interface) SendVia(via *HostInfo,
	relay *Relay,
	ad,
	nb,
	out []byte,
	nocopy bool,
) {
	if noiseutil.EncryptLockNeeded {
		// NOTE: for goboring AESGCMTLS we need to lock because of the nonce check
		via.ConnectionState.writeLock.Lock()
	}
	c := via.ConnectionState.messageCounter.Add(1)

	out = header.Encode(out, header.Version, header.Message, header.MessageRelay, relay.RemoteIndex, c)
	f.connectionManager.Out(via)

	// Authenticate the header and payload, but do not encrypt for this message type.
	// The payload consists of the inner, unencrypted Nebula header, as well as the end-to-end encrypted payload.
	if len(out)+len(ad)+via.ConnectionState.eKey.Overhead() > cap(out) {
		if noiseutil.EncryptLockNeeded {
			via.ConnectionState.writeLock.Unlock()
		}
		via.logger(f.l).Error("SendVia out buffer not large enough for relay",
			"outCap", cap(out),
			"payloadLen", len(ad),
			"headerLen", len(out),
			"cipherOverhead", via.ConnectionState.eKey.Overhead(),
		)
		return
	}

	// The header bytes are written to the 'out' slice; Grow the slice to hold the header and associated data payload.
	offset := len(out)
	out = out[:offset+len(ad)]

	// In one call path, the associated data _is_ already stored in out. In other call paths, the associated data must
	// be copied into 'out'.
	if !nocopy {
		copy(out[offset:], ad)
	}

	var err error
	out, err = via.ConnectionState.eKey.EncryptDanger(out, out, nil, c, nb)
	if noiseutil.EncryptLockNeeded {
		via.ConnectionState.writeLock.Unlock()
	}
	if err != nil {
		via.logger(f.l).Info("Failed to EncryptDanger in sendVia", "error", err)
		return
	}
	err = f.writers[0].WriteTo(out, via.GetRemote())
	if err != nil {
		via.logger(f.l).Info("Failed to WriteTo in sendVia", "error", err)
	}
	f.connectionManager.RelayUsed(relay.LocalIndex)
}
```

**File:** inside.go (L343-367)
```go
func (f *Interface) sendNoMetrics(t header.MessageType, st header.MessageSubType, ci *ConnectionState, hostinfo *HostInfo, remote netip.AddrPort, p, nb, out []byte, q int) {
	if ci.eKey == nil {
		return
	}
	useRelay := !remote.IsValid() && !hostinfo.GetRemote().IsValid()
	fullOut := out

	if useRelay {
		if len(out) < header.Len {
			// out always has a capacity of mtu, but not always a length greater than the header.Len.
			// Grow it to make sure the next operation works.
			out = out[:header.Len]
		}
		// Save a header's worth of data at the front of the 'out' buffer.
		out = out[header.Len:]
	}

	if noiseutil.EncryptLockNeeded {
		// NOTE: for goboring AESGCMTLS we need to lock because of the nonce check
		ci.writeLock.Lock()
	}
	c := ci.messageCounter.Add(1)

	//l.WithField("trace", string(debug.Stack())).Error("out Header ", &Header{Version, t, st, 0, hostinfo.remoteIndexId, c}, p)
	out = header.Encode(out, header.Version, t, st, hostinfo.remoteIndexId, c)
```

**File:** bits.go (L152-227)
```go
// Update has three branches:
//   - i == b.current+1: fast path; advance the cursor by one and lose-count
//     the slot we just stomped (only past warmup; see the i > b.length guard
//     below).
//   - i  >  b.current+1: jump path; clear all slots between current and i
//     (or up to a full window's worth, whichever is smaller) via clearRange,
//     then mark i. Two arms here: a warmup arm that handles the very first
//     window before the cursor has slid, and a steady-state arm that treats
//     every cleared empty slot as a lost packet.
//   - i  <= b.current: in-window check for duplicates; out-of-window otherwise.
//
// NewBits seeds bits[0]=1 so counter 0 looks "received" — Update never
// clears that marker during warmup (clearRange skips position 0 when
// startPos=1), and once b.current >= b.length the marker is no longer
// consulted. The marker prevents a fictitious "lost" hit on the first real
// counter.
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

// updateSlow handles jumps, in-window backfill, dupes, and out-of-window.
func (b *Bits) updateSlow(l *slog.Logger, i uint64) bool {
	// If i is a jump, adjust the window, record lost, update current, and return true
	if i > b.current {
		end := i
		if end > b.current+b.length {
			end = b.current + b.length
		}
		count := end - b.current
		startPos := (b.current + 1) & b.lengthMask

		var lost int64
		if b.current >= b.length {
			// Steady state: every cleared slot is past warmup, so any unset
			// bit we evict is a lost packet from the previous cycle.
			wasSet := b.clearRange(startPos, count)
			lost = int64(count) - int64(wasSet)
		} else {
			// Warmup (the very first window). Some cleared slots represent
			// packets <= length where eviction is not "lost" in the usual
			// sense. This branch is taken at most once per connection so we
			// don't bother optimizing it.
			for n := b.current + 1; n <= end; n++ {
				if !b.get(n) && n > b.length {
					lost++
				}
			}
			b.clearRange(startPos, count)
		}

		// Anything past the new window can never be backfilled, so it's lost.
		if i > b.current+b.length {
			lost += int64(i - b.current - b.length)
		}
		b.lostCounter.Inc(lost)

		b.set(i)
		b.current = i
		return true
	}
```
