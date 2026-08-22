### Title
Nil `CipherState` silently "succeeds" a failed decryption instead of erroring - ([File: noiseutil/aesgcm.go], [File: noiseutil/chachapoly.go])

### Summary
`CipherStateAESGCM.DecryptDanger` and `CipherStateChaChaPoly.DecryptDanger` both special-case a nil receiver by returning `([]byte{}, nil)` — i.e. success with empty plaintext and no error — instead of returning an error. This mirrors the reported `EncryptedERC` bug class: code assumes a security-critical operation (`transferFrom` / AEAD decryption+authentication) either succeeds or reverts/errors, but here a failure-equivalent state (no cipher available) is coerced into a "no error" success return, letting the caller's state-mutating logic proceed as if the packet were validly authenticated.

### Finding Description
`ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` (`connection_state.go`) call `cs.dKey.DecryptDanger(...)` and treat any `err == nil` return as proof the AEAD tag was verified: [1](#0-0) [2](#0-1) 

But the underlying implementations do not actually enforce authentication when the `CipherState` value itself is nil: [3](#0-2) [4](#0-3) 

Both return `nil` error with an empty (unauthenticated) plaintext when `s == nil`, rather than surfacing an error. Every downstream caller in the packet-processing path (`outside.go`'s `readOutsidePackets`, which drives `hostinfo.ConnectionState.Decrypt` and `VerifyRelay` on attacker-reachable, non-cert-authenticated UDP input) only branches on `err != nil` to drop a packet — it never separately checks whether real cryptographic verification occurred: [5](#0-4) 

This is structurally identical to the ETRP-1 report's root cause: a security-critical call (`transferFrom` / AEAD `Open`) is assumed to fail loudly, but the implementation instead lets an unauthenticated/failed condition masquerade as a successful, empty result, and the caller's subsequent logic (deposit crediting / roaming, connection-manager liveness update, message dispatch) proceeds on that false-success signal.

### Impact Explanation
If any code path can construct a `ConnectionState` (or otherwise reach `Decrypt`/`VerifyRelay`) with a nil-valued `dKey` `CipherState` interface holding a nil concrete `*CipherStateAESGCM`/`*CipherStateChaChaPoly` pointer, an attacker sending a packet with a correctly formed Nebula header but arbitrary/garbage AEAD payload would have that payload accepted as "decrypted successfully" with empty content, and the caller would still run `handleHostRoaming`, `connectionManager.In(hostinfo)`, and further packet dispatch as if a genuine authenticated packet was received — i.e. remote state poisoning / a form of decryption-and-authentication bypass without needing a valid CA-signed certificate for that traffic.

### Likelihood Explanation
This requires a `ConnectionState` where `dKey` ends up as a nil-pointer-in-interface rather than truly unset (`nil` interface) — I was not able to confirm within the available context whether any code path in `pki.go`, `connection_state.go`, or the handshake completion logic actually constructs a `ConnectionState`/`CipherState` in that specific nil-pointer-wrapped-in-non-nil-interface state during normal operation (this is a classic Go footgun: an interface holding a nil concrete pointer is not `== nil` at the interface level, so `cs.dKey == nil` checks elsewhere would not catch it). Confirming exploitability requires tracing all constructors of `noiseutil.CipherState` values assigned to `ConnectionState.dKey`/`eKey` to see if a nil `*noise.CipherState` or nil concrete cipher struct can ever reach this field on a path attacker-reachable pre-authentication. I could not fully verify this within the given tool budget.

### Recommendation
Remove the nil-receiver short-circuit in `DecryptDanger` for both `CipherStateAESGCM` and `CipherStateChaChaPoly` (or have it explicitly return a descriptive error rather than `(empty, nil)`), so that any missing/invalid cipher state is treated as a hard decryption failure, consistent with the analogous fix recommended in the report (fail loudly rather than silently treating an unsafe/incomplete operation as success). Additionally, audit every constructor path that can populate `ConnectionState.dKey`/`eKey` to guarantee a nil concrete cipher can never be wrapped in a non-nil interface value reachable from `Decrypt`/`VerifyRelay`.

### Proof of Concept
Not able to construct a concrete, end-to-end PoC from the available context — doing so requires confirming a real code path that leaves `ConnectionState.dKey` holding a nil `*CipherStateAESGCM`/`*CipherStateChaChaPoly` while still being reachable by `readOutsidePackets` for an unauthenticated peer. This would need further investigation (e.g., a Devin session with full repository access) to trace all `NewCipherState`/`newConnectionStateFromResult` call sites and handshake failure/partial-completion branches.

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

**File:** noiseutil/aesgcm.go (L36-46)
```go
func (s *CipherStateAESGCM) DecryptDanger(out, ad, ciphertext []byte, n uint64, nb []byte) ([]byte, error) {
	if s == nil {
		return []byte{}, nil
	}
	nb[0] = 0
	nb[1] = 0
	nb[2] = 0
	nb[3] = 0
	binary.BigEndian.PutUint64(nb[4:], n)
	return s.c.Open(out, nb, ciphertext, ad)
}
```

**File:** noiseutil/chachapoly.go (L35-45)
```go
func (s *CipherStateChaChaPoly) DecryptDanger(out, ad, ciphertext []byte, n uint64, nb []byte) ([]byte, error) {
	if s == nil {
		return []byte{}, nil
	}
	nb[0] = 0
	nb[1] = 0
	nb[2] = 0
	nb[3] = 0
	binary.LittleEndian.PutUint64(nb[4:], n)
	return s.c.Open(out, nb, ciphertext, ad)
}
```

**File:** outside.go (L113-132)
```go
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
