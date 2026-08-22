#No Vulnerability found for this question.

Analysis: The header.H.RemoteIndex field selecting a candidate hostinfo via `HostMap.QueryIndex`/`QueryRelayIndex` is architecturally equivalent to WireGuard-style receiver indices — it is a session-selection hint, not a security boundary, and its unauthenticated nature is by design (this is standard across modern VPN protocols).

The specific invariant claimed to be violated — that a failed AEAD verification could corrupt or leak information about a victim session's replay-window state — does not hold in this codebase. In `ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` [1](#0-0) , the sequence is: `window.Check` (read-only, no mutation) → `dKey.DecryptDanger` (AEAD verify) → `window.Update` (mutating) is only called **after** a successful decrypt/verify. If `DecryptDanger` fails (which is what happens for forged ciphertext against a real RemoteIndex, since the attacker lacks `dKey`), the function returns immediately with the error, before `window.Update` is ever invoked [2](#0-1) [3](#0-2) . Therefore `window.current`/`window.bits` are provably unchanged after any Decrypt/VerifyRelay failure — the exact invariant the proof idea asks to test already holds by construction.

The dispatch path in `readOutsidePackets` [4](#0-3)  does select the hostinfo purely from the unauthenticated `h.RemoteIndex`, and a nonexistent index short-circuits earlier (`hostinfo == nil` branch calling `maybeSendRecvError`) than a valid index whose ciphertext fails AEAD verification. This produces an inherent, unavoidable timing/behavioral distinguisher between "index exists" and "index doesn't exist" — but that distinguisher only reveals whether a given 32-bit index is currently active, not any information about the session's key material, plaintext, or window position, and does not enable tag forgery (AEAD security is unaffected) or state corruption (window mutation is correctly gated on successful decryption). This matches the known, accepted design of index-based session lookup used in comparable protocols and is not treated as a bounty-eligible authentication bypass, decryption oracle, or state-poisoning bug under the stated rules, since real authentication bypass or state corruption is not achieved.

### Citations

**File:** connection_state.go (L61-108)
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
