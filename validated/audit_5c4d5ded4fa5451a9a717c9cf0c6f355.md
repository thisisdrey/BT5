### Title
Check-then-Act race in the anti-replay window lets a duplicated ciphertext be decrypted twice before rejection - ([File: connection_state.go])

### Summary
The reported bug class is a boundary/TOCTOU flaw: two checks that are supposed to be mutually exclusive (and evaluated atomically as a pair) are instead evaluated as separate operations with a gap in between, letting an attacker land in that gap and cause the protocol to process the same event through two different, conflicting paths. In Nebula, the same pattern occurs in `ConnectionState.Decrypt`, where the anti-replay window's "is this counter new?" check (`window.Check`) and "mark this counter as seen" mutation (`window.Update`) are two separately-locked operations with unprotected AEAD decryption sandwiched between them, instead of being one atomic check-and-set.

### Finding Description
`ConnectionState.Decrypt` is the function that authenticates and decrypts every data packet on an established tunnel using the packet's `messageCounter` as the AEAD nonce and anti-replay index: [1](#0-0) 

The sequence is:
1. `decryptLock.Lock(); window.Check(messageCounter); decryptLock.Unlock()` — checks if the counter has *not* been seen yet.
2. `dKey.DecryptDanger(...)` — performs the actual AEAD decryption, **outside any lock**.
3. `decryptLock.Lock(); window.Update(messageCounter); decryptLock.Unlock()` — marks the counter seen and rejects if it turns out to already be marked.

Because the lock is dropped between steps 1 and 3, `Check` and `Update` are not atomic with respect to each other. If two goroutines invoke `Decrypt` concurrently with the *same* `messageCounter` (an attacker can trivially cause this by capturing one legitimate ciphertext and re-injecting a duplicate of it at the transport layer, e.g. via UDP amplification/duplication, before the first copy is fully processed), both goroutines can pass `window.Check` before either calls `window.Update` — the classic "check the boundary, then act, but the state can change out from under you before you act" pattern that produced the M-7 bug (two mutually-exclusive states, `onlyActiveAllocation` and `onlyAfterAllocation`, both evaluating true in the same instant because the check-then-act wasn't atomic).

This directly mirrors the reported vulnerability class: a window/boundary predicate (`Check`) is trusted to still hold when a later, supposedly-paired predicate (`Update`) runs, but nothing enforces that invariant across the gap, and the gap is externally triggerable (network timing) rather than something the code controls.

### Impact Explanation
The `Bits.Update` call after the race does still correctly reject the second thread's replayed counter (returns `false`, causing `Decrypt` to return `ErrAlreadySeen`), so the "second" duplicate's decrypted plaintext is discarded rather than delivered to the TUN device. However, the vulnerability is that the AEAD decryption itself is executed for a message that should have been rejected before ever being decrypted — the intended sequencing (reject before spending the decryption budget, and reject atomically with respect to the replay window) is violated. This weakens the intended replay-window invariant that `Check` and `Update` operate as a single atomic test-and-set, and opens the code up to a CPU-amplification/DoS surface: an attacker who replays the same captured ciphertext concurrently from multiple sources can force the tunnel's decrypt path to spend the full AEAD verification cost on both copies instead of being short-circuited by the anti-replay check, unlike the correct design where `Check` should make the second copy a cheap no-op.

### Likelihood Explanation
Triggering the race requires only the ability to duplicate a single captured ciphertext and deliver both copies to the victim's UDP listener close together in time — well within reach of any on-path or off-path attacker capable of packet capture/replay, with no valid Nebula certificate or CA trust required. Whether it manifests depends on Nebula's internal packet-processing concurrency model (multiple reader routines can call into the same tunnel's `Decrypt` when `routines` > 1), and could not be conclusively confirmed from the available parts of `outside.go`/`interface.go` due to indexing limits on those files; a Devin session with full repository access would be needed to trace exactly how many goroutines can invoke `ConnectionState.Decrypt` for the same tunnel concurrently and to reproduce the race under `-race`.

### Recommendation
Hold `decryptLock` across the entire `Check` → `Decrypt` → `Update` sequence (or fold `Check`+`Update` into a single atomic "test-and-set" operation performed once, before or after decryption but without releasing the lock in between), so no other goroutine can observe an intermediate state of the replay window for the same counter.

### Proof of Concept
Conceptual reproduction (would need to be validated in a live session):
1. Establish a tunnel between two Nebula instances with `routines` > 1 so multiple goroutines can service incoming UDP packets for the same peer.
2. Capture one legitimate data packet in flight.
3. Send two copies of that captured packet to the receiving instance in rapid succession (racing to arrive on different reader routines).
4. Under `go test -race`, observe that both goroutines can pass `window.Check` for the same `messageCounter` before either calls `window.Update`, meaning `DecryptDanger` is invoked twice for what should have been recognized as a single already-accepted/replayed counter. [2](#0-1)

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
