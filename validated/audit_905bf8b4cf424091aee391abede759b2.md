### Title
Check-Then-Act Race in Replay-Window Enforcement Allows a Captured Packet to be Decrypted and Accepted Twice - ([File: connection_state.go])

### Summary
`ConnectionState.Decrypt` (and the analogous `VerifyRelay`) split anti-replay enforcement into two separately-locked steps — `window.Check()` before decryption and `window.Update()` after decryption — with the actual AEAD decryption performed *outside* the lock. This mirrors the reported bug class: a value/state check (the "quote"/price read) is separated from the state-mutating step (the "burn"), and an attacker who can trigger both paths concurrently can make the check pass twice before the mutation is applied, defeating the very protection the two-step design exists to provide.

### Finding Description
`ConnectionState.window` is a `Bits` anti-replay bitmap protected by `decryptLock`. `Decrypt` performs:

```go
cs.decryptLock.Lock()
result := cs.window.Check(l, messageCounter)
cs.decryptLock.Unlock()
if !result { return nil, ErrAlreadySeen }

out, err = cs.dKey.DecryptDanger(out, packet[:header.Len], packet[header.Len:], messageCounter, nb)
...
cs.decryptLock.Lock()
result = cs.window.Update(l, messageCounter)
cs.decryptLock.Unlock()
if !result { return nil, ErrAlreadySeen }
``` [1](#0-0) 

Because the lock is released between `Check` and `Update`, and the expensive AEAD decryption happens entirely unlocked, two concurrent invocations of `Decrypt` for the same `messageCounter` (i.e., a legitimate packet and an attacker-replayed copy of the exact same ciphertext arriving close together) can both observe `Check() == true` before either has called `Update()`. Both then independently perform `DecryptDanger` (a deterministic, stateless AEAD verification that succeeds for any valid ciphertext regardless of how many times it is called) and both proceed to call `Update()`.

`Bits.Update`'s fast path is even more permissive than the `Check`/`Update` split alone: when `i == b.current+1` it unconditionally sets the bit and returns `true` — it does not verify the bit is still unset:

```go
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
``` [2](#0-1) 

So in the race window where the counter is still `current+1` for both callers, **both** `Update()` calls return `true`, and both `Decrypt()` calls return the plaintext successfully — the replayed packet is fully authenticated and accepted a second time, even though the design intent of the `Bits` window is exactly to reject this ("Check returns true if i is within (or way out in front of) the window, and not a replay").

This is reachable purely by an on-path/off-path network attacker who captures a single legitimate ciphertext packet and re-injects it over UDP; no CA-signed certificate or valid handshake participation is required, since replay happens against an already-established tunnel's ciphertext stream. Nebula's own regression coverage confirms this exact bug class exists for the relay path (`handleOutsideRelayPacket` previously "authenticated the frame but never advanced the replay window, so every replay was re-forwarded") and was patched by locking the replay-window update [3](#0-2) , and a dedicated e2e test guards against re-introduction on the relay path [4](#0-3) . However, the guard added ("Lock replay window updates so concurrent readers can't corrupt it") only prevents corruption of the bitmap's internal state; it does not close the semantic TOCTOU gap between `Check` and `Update` in `Decrypt`/`VerifyRelay`, nor does it change `Update`'s unconditional-accept fast path.

### Impact Explanation
An attacker who can capture and duplicate a single ciphertext frame on the wire can cause the underlying decrypted payload to be delivered/processed twice by the receiving Nebula node, on any tunnel, without needing to complete a handshake or hold a valid certificate. This is a replay-protection bypass — the core security property the `ReplayWindow`/`Bits` mechanism exists to guarantee is defeated under concurrent delivery, which is realistic in multi-routine (`routines > 1`) deployments and even single-routine deployments where two UDP datagrams for the same tunnel can be processed back-to-back before the first `Update()` completes (e.g., relay forwarding path, or simple duplicate injection at line rate). Consequences include duplicate application-level state transitions on the inside network (e.g., replayed TCP/UDP segments reprocessed by the tun-side stack) and violation of the "no replay" guarantee documented for `Bits.Check`.

### Likelihood Explanation
Likelihood is moderate-to-high in specific but realistic conditions: it requires the attacker to capture and near-simultaneously re-inject a genuine ciphertext packet, and requires the receiver's processing of the original and replay to interleave within the `Check`→(decrypt)→`Update` window. This window is widened by the fact that decryption is deliberately performed without holding `decryptLock`, and by multi-routine UDP reading which increases the chance of true concurrent execution. The relay path historical bug (`#1751`) demonstrates this exact class of defect was previously present and exploitable in production before being partially addressed.

### Recommendation
Close the TOCTOU gap by holding a single lock (or using an atomic check-and-set primitive) across the entire "check-decrypt-mark" sequence for a given `messageCounter`, or by making `Bits.Update`'s fast path (and all paths) strictly reject an already-set bit rather than unconditionally setting it. Concretely: perform the replay check, decryption, and window update as one atomically-guarded operation per `ConnectionState`, and only release the decrypted plaintext to the caller if the window update establishes that this exact counter had not already been marked seen.

### Proof of Concept
1. Attacker sniffs the encrypted UDP stream between two established Nebula peers and captures one ciphertext data packet with message counter `N` (where `N == cs.window.current + 1` at the time of capture).
2. Attacker immediately re-transmits (duplicates) that exact ciphertext frame to the receiving peer's UDP port, timed to arrive while the original packet's `Decrypt` call is still between its `Check` and `Update` phases (e.g., under load, with `routines > 1`, or via the relay-forwarding path where an intermediate relay node processes attacker-controlled timing).
3. Both the genuine and the replayed packet call `cs.window.Check(l, N)` before either has called `Update`; both observe `true` (not yet in window as "seen").
4. Both independently call `cs.dKey.DecryptDanger(...)`, which succeeds deterministically for both copies since it is a pure cryptographic verification with no side effects tracking prior calls.
5. Both call `cs.window.Update(l, N)`. Because `N == cs.window.current+1` for both invocations at call time (the race window), the fast path in `bits.go` (lines 173-184) sets the bit and returns `true` for both, rather than the second call detecting the bit is already set.
6. Both `Decrypt` calls return the decrypted plaintext successfully to their respective callers, so the replayed packet's payload is processed a second time by the tun-side pipeline, confirming the replay-window bypass.

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

**File:** bits.go (L173-184)
```go
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
```

**File:** CHANGELOG.md (L79-81)
```markdown
- Advance the replay window on relayed packets so a relay drops replayed frames instead of re-forwarding them. (#1751)
- Fix a race in relay state handling. (#1753)
- Lock replay window updates so concurrent readers can't corrupt it. (#1802)
```

**File:** e2e/tunnels_test.go (L377-381)
```go
// TestRelayReplayProtection asserts that a relay (forwarding-type) node rejects
// replayed relay frames. A captured relay frame, re-injected with the same
// message counter, must be dropped by the replay window rather than re-forwarded
// to the relay target. Before the fix, handleOutsideRelayPacket authenticated the
// frame but never advanced the replay window, so every replay was re-forwarded.
```
