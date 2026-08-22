### Title
Anti-replay window check-then-act race allows duplicate packet acceptance - ([File: connection_state.go])

### Summary
`ConnectionState.Decrypt` (and `VerifyRelay`) implement the anti-replay window as a Check → Decrypt → Update sequence, but the `decryptLock` protecting the replay window `Bits` structure is **released between `Check` and `Update`**, with the expensive AEAD decryption happening outside the lock. This mirrors the external report's root cause: two supposedly 1:1-linked pieces of state ("has this counter been seen" and "should this packet be admitted") are read and written at different times against a value that can change in between, letting the second read/write diverge from the first.

### Finding Description
`Decrypt` in `connection_state.go` does:
1. Lock, call `cs.window.Check(l, messageCounter)`, unlock.
2. Call `cs.dKey.DecryptDanger(...)` (no lock held).
3. Lock, call `cs.window.Update(l, messageCounter)`, unlock. [1](#0-0) 

`f.readers` in `interface.go` and the `routines` configuration show that Nebula runs multiple concurrent reader goroutines (`f.routines`, `f.readers[]`), so `readOutsidePackets` — which calls `hostinfo.ConnectionState.Decrypt` for every inbound message — can execute concurrently for the same `HostInfo`/`ConnectionState` from different reader goroutines. [2](#0-1) [3](#0-2) 

Because `Check` only reads the current window state and `Update` is the only place that actually marks a counter as seen, a duplicate ciphertext (an attacker-replayed UDP packet, or a legitimate retransmission racing with itself, or two workers dequeuing copies) delivered on two goroutines nearly simultaneously can both pass `Check` (since neither has called `Update` yet), both successfully decrypt (the ciphertext is valid — it's a legitimate packet, just replayed), and only afterwards does one of the two `Update` calls lose the race and return `false`/duplicate. By that time the "losing" goroutine has already produced valid decrypted plaintext via `DecryptDanger` and returned it to the caller in `out`, which is then written to the firewall/handled as a fresh message before its `Update` call is even evaluated — the duplicate-detection state and the actual accept/drop decision are decoupled, exactly the same "two states computed from data that has since diverged" pattern as the `xezETH`/`ezETH` accounting split in the source report.

### Impact Explanation
This breaks the confidentiality/integrity guarantee that Noise/AEAD anti-replay protection is supposed to provide at the transport layer. A network-level attacker who can observe/capture a single valid encrypted Nebula packet (no CA-signed certificate or valid peer identity required — this is purely inbound UDP handling before any application-level authorization) can attempt to have the same packet accepted more than once by racing duplicate copies of it against the receiver's reader goroutines. Consequences range from duplicate application-layer message delivery (e.g., duplicate TUN packet delivery) to undermining the anti-replay invariant that downstream logic (roaming triggers, connection manager `In()`, LightHouse/Test/Control message handling) relies on being "exactly once per counter."

### Likelihood Explanation
Exploiting the window requires winning a fine-grained race between multiple reader goroutines processing the same replayed ciphertext, which is timing-sensitive and not guaranteed to succeed on every attempt, but it is remotely triggerable with no credentials, no valid certificate, and no prior trust relationship — only the ability to observe and duplicate a single UDP packet on the wire, similar to a classic replay attack. The multi-goroutine reader design (`f.routines`) makes the race window practically reachable rather than purely theoretical.

### Recommendation
Hold `decryptLock` across the entire Check → Decrypt → Update sequence in `ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` (or otherwise make the window check-and-mark atomic, e.g., have `Check` reserve the slot and `Update`/an explicit rollback release it on decryption failure) so no two goroutines can decrypt the same counter concurrently before the window is updated.

### Proof of Concept
1. Establish a tunnel between two Nebula nodes so a `ConnectionState` with a live `Bits` window exists.
2. Capture one valid encrypted `header.Message` UDP packet destined for a node running with `routines > 1` (multiple reader goroutines).
3. Send two copies of that exact packet to the receiver at (near-)simultaneous wall-clock time, e.g., via two sockets bound to slightly different source ports/threads, so they land on two different reader goroutines.
4. Because `cs.window.Check` (in `Decrypt`, `connection_state.go:64`) is evaluated for both copies before either copy's `cs.window.Update` (`connection_state.go:76`) executes, both copies can pass `Check`, both successfully `DecryptDanger`, and both get delivered to `handleOutsideMessagePacket`/TUN before the losing goroutine's `Update` call returns `ErrAlreadySeen` too late to prevent the double delivery. [1](#0-0) [4](#0-3)

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

**File:** interface.go (L73-94)
```go
	routines              int
	disconnectInvalid     atomic.Bool
	closed                atomic.Bool
	relayManager          *relayManager

	tryPromoteEvery atomic.Uint32
	reQueryEvery    atomic.Uint32
	reQueryWait     atomic.Int64

	sendRecvErrorConfig   recvErrorConfig
	acceptRecvErrorConfig recvErrorConfig

	// rebindCount is used to decide if an active tunnel should trigger a punch notification through a lighthouse
	rebindCount int8
	version     string

	conntrackCacheTimeout time.Duration

	ctx     context.Context
	writers []udp.Conn
	readers []io.ReadWriteCloser
	wg      sync.WaitGroup
```

**File:** outside.go (L126-146)
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

	switch h.Type {
	case header.Message:
		switch h.Subtype {
		case header.MessageNone:
			f.handleOutsideMessagePacket(hostinfo, out, packet, fwPacket, nb, q, localCache)
		default:
			hostinfo.logger(f.l).Error("IsValidSubType was true, but unexpected message subtype seen", "from", via, "header", h)
			return
		}
```
