### Title
Anti-replay window bypass via TOCTOU race between `Bits.Check` and `Bits.Update` in `ConnectionState.Decrypt` - (File: connection_state.go)

### Summary
`ConnectionState.Decrypt` releases `decryptLock` between the anti-replay `Check` and the anti-replay `Update`, decrypting the packet in between while unlocked. This creates a window where two goroutines processing the same message counter concurrently can both pass `Check` before either calls `Update`, allowing a captured/replayed ciphertext to be accepted and delivered twice instead of being rejected as a replay.

### Finding Description
`ConnectionState.Decrypt` is structured as: lock → `window.Check(messageCounter)` → unlock → decrypt (no lock) → lock → `window.Update(messageCounter)` → unlock. [1](#0-0) 

`Bits.Check` is a pure read that does not mark the counter as seen; only `Bits.Update` marks it. [2](#0-1) [3](#0-2) 

Because the mark-as-seen step (`Update`) is deferred until after decryption and is not covered by the same critical section as `Check`, if two packets carrying the *same* message counter (i.e., a replayed/duplicated ciphertext for a counter already in flight) are processed concurrently, both can observe `result := cs.window.Check(...)` as `true` before either has called `Update`. Both will then proceed to `DecryptDanger` and successfully decrypt (AEAD decryption for a given counter/nonce is deterministic and doesn't itself detect duplication), and only the second `Update` call will report `false`/`ErrAlreadySeen` — but by then the first (and possibly the second) decrypted payload has already been produced and can be returned/delivered to the caller before the "already seen" outcome is known. This is a classic check-then-act race that undermines the purpose of the counter/nonce window, which exists specifically to reject replayed traffic (`ReplayWindow`, `Bits`). [4](#0-3) 

This is analogous to the reported `AmpleEarn` bug class: a value is snapshotted/read ("checked") at one point, but the authoritative state-changing update happens later, and intervening concurrent activity is not reflected, letting stale/duplicate state slip through and produce an outcome the design intended to prevent.

### Impact Explanation
If packet processing for a single tunnel can occur on more than one goroutine concurrently (e.g., multiple sockets/threads reading from the underlay and dispatching to the same `ConnectionState`), an attacker who can capture and duplicate/replay a previously observed valid ciphertext (no valid certificate needed — replay only requires network visibility) could get a replayed packet processed as legitimate traffic instead of being dropped, defeating the anti-replay protection Nebula's `Bits` window is designed to enforce. Depending on the payload this can result in duplicate delivery of application traffic (state poisoning at the tunnel/application layer) via a genuine replay-protection bypass.

### Likelihood Explanation
The window between `Check` and `Update` is narrow (bounded by one `DecryptDanger` call), so exploitation is timing-sensitive and requires the attacker to race two copies of the same ciphertext into the decrypt path at (near) the same time. It is plausible on a general-purpose VPN daemon where multiple reader goroutines may dispatch inbound UDP packets for the same peer concurrently, but I was not able to fully confirm within this session whether Nebula's packet-reading architecture (`outside.go`/`interface.go`) actually dispatches inbound packets for a single `HostInfo`/`ConnectionState` across multiple concurrent goroutines, or serializes them per-tunnel (which would make this race unreachable in practice). This is the key remaining uncertainty.

### Recommendation
Hold `decryptLock` across the entire check-decrypt-update sequence (or otherwise make "check + mark" atomic, e.g., an optimistic "reserve" step that immediately marks the counter as seen before decrypting and rolls back the mark only if decryption fails) so that no two callers can observe an un-marked counter simultaneously.

### Proof of Concept
Conceptual sequence (assuming concurrent dispatch of the same counter is reachable):
1. Attacker captures a valid ciphertext for counter N sent from peer A to peer B.
2. Attacker sends two copies of this exact ciphertext to B at (nearly) the same time, arriving on two different reader goroutines.
3. Goroutine 1: `cs.window.Check(N)` → true (unlocked) → begins `DecryptDanger`.
4. Goroutine 2 (racing before goroutine 1 calls `Update`): `cs.window.Check(N)` → also true → begins `DecryptDanger`.
5. Both goroutines successfully decrypt the same payload; only one `Update(N)` call afterward returns true, but the duplicate payload from the other goroutine has already been produced and may be delivered up the stack before its "already seen" status is enforced.

I could not directly instrument/run this PoC in this session; confirming exploitability requires verifying nebula's inbound packet dispatch concurrency model, which I was unable to fully trace before the tool budget ran out.

### Citations

**File:** connection_state.go (L15-27)
```go
const ReplayWindow = 1024

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
