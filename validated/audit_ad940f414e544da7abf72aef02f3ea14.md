### Title
`CipherState.DecryptDanger` silently reports authentication success on a nil cipher instead of failing closed - ([File: noiseutil/aesgcm.go], [File: noiseutil/chachapoly.go])

### Summary
### Finding Description
The external report's root issue is that `_pullTokenInputAndPayProtocolFee()` assumes an operation (`safeTransferFrom`) had the expected effect without actually checking the resulting state, letting a no-op call be treated as success. The same code smell exists in nebula's AEAD wrapper: `CipherStateAESGCM.DecryptDanger` and `CipherStateChaChaPoly.DecryptDanger` both special-case a nil receiver by returning `[]byte{}, nil` instead of performing (or failing) the AEAD `Open` call: [1](#0-0) [2](#0-1) 

This is asymmetric with `EncryptDanger`, which correctly treats a nil cipher state as a hard error (`"no cipher state available to encrypt"`): [3](#0-2) 

The only caller of `DecryptDanger` on the data plane is `ConnectionState.Decrypt` / `ConnectionState.VerifyRelay`, both reachable directly from attacker-controlled UDP packets via `readOutsidePackets` in `outside.go` before any further processing occurs: [4](#0-3) [5](#0-4) [6](#0-5) 

If a `ConnectionState.dKey` were ever nil at this call site (e.g., an unexpected code path builds/reuses a `ConnectionState` before `dKey` is assigned, or a future refactor of `newConnectionStateFromResult` / hostmap reuse leaves it unset), `Decrypt`/`VerifyRelay` would return `err == nil` with an empty (or unauthenticated) buffer — i.e., "success" without ever verifying the AEAD tag — exactly mirroring the report's pattern of trusting an operation's return value without confirming it actually did what was expected.

### Impact Explanation
If reached with a nil `dKey`, this bypasses AEAD authentication entirely: an attacker-supplied UDP packet would be treated as successfully decrypted/authenticated (empty plaintext, no error) rather than rejected, which is a decryption/authentication-bypass class impact — it defeats the fail-closed guarantee that message decryption should provide.

### Likelihood Explanation
Uncertain / low-to-unproven. I could not find a concrete production code path in this snapshot where `ConnectionState.dKey` is nil when `Decrypt` or `VerifyRelay` is invoked — `newConnectionStateFromResult` always populates `dKey` via `noiseutil.NewCipherState(r.DKey, r.Cipher)` before a `ConnectionState` is attached to a `HostInfo`. So today the vulnerable branch in `DecryptDanger` appears to be dead code guarding against a nil receiver that shouldn't occur in the current call graph. The risk is that the API's silent-success-on-nil behavior is inconsistent with `EncryptDanger`'s fail-loud behavior, so any future code path that constructs or reuses a `ConnectionState`/`CipherState` before keys are derived (e.g. new relay/handshake shortcuts) would silently accept unauthenticated attacker packets rather than crash or error, with no attacker requiring a CA-signed certificate to send the triggering UDP data once such a path exists.

### Recommendation
Make `DecryptDanger` fail closed on a nil cipher, symmetric with `EncryptDanger`: return a non-nil error (e.g., `"no cipher state available to decrypt"`) instead of `[]byte{}, nil`. Additionally, add an explicit invariant check in `ConnectionState.Decrypt`/`VerifyRelay` (or at `HostInfo`/`ConnectionState` construction) that `dKey`/`eKey` are non-nil before any packet is fed to them, so a violated invariant surfaces immediately rather than degrading into a silent "packet authenticated" false positive.

### Proof of Concept
```go
// noiseutil package, illustrating the asymmetry:
var cs *CipherStateAESGCM // nil
_, err := cs.EncryptDanger(nil, nil, []byte("data"), 1, make([]byte, 12))
// err != nil: "no cipher state available to encrypt"

out, err := cs.DecryptDanger(nil, nil, []byte("attacker-controlled-ciphertext"), 1, make([]byte, 12))
// err == nil, out == []byte{}  -- reports "successful" authentication of arbitrary input
// mirrors: safeTransferFrom() on a non-existent/fee-on-transfer token returning success
// without verifying tokens were actually received.
```

### Citations

**File:** noiseutil/aesgcm.go (L24-34)
```go
func (s *CipherStateAESGCM) EncryptDanger(out, ad, plaintext []byte, n uint64, nb []byte) ([]byte, error) {
	if s == nil {
		return nil, errors.New("no cipher state available to encrypt")
	}
	nb[0] = 0
	nb[1] = 0
	nb[2] = 0
	nb[3] = 0
	binary.BigEndian.PutUint64(nb[4:], n)
	return s.c.Seal(out, nb, plaintext, ad), nil
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

**File:** outside.go (L124-132)
```go
	}

	out, err = hostinfo.ConnectionState.Decrypt(f.l, h.MessageCounter, out, packet, nb)
	if err != nil {
		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			hostinfo.logger(f.l).Debug("Failed to decrypt packet", "error", err, "from", via, "header", h)
		}
		return
	}
```
