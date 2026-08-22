### Title
Shared Anti-Replay Window and Nonce Counter Space Between Data-Plane `Decrypt` and Relay-Frame `VerifyRelay` on the Same ConnectionState - (File: `connection_state.go`)

### Summary
`ConnectionState` uses a single `window` (`*Bits`, the anti-replay bitmap) and a single `dKey`/nonce space that is checked and updated by two functionally distinct operations: `Decrypt` (regular tunnel `Message` payloads) and `VerifyRelay` (relay-frame authentication for traffic this host is relaying for a third party) [1](#0-0) . Both operations call the exact same `cs.window.Check`/`cs.window.Update` and the same `cs.dKey.DecryptDanger` with a caller-supplied `messageCounter`, without any separation of the counter space by message type/subtype [2](#0-1) .

### Finding Description
This mirrors the reported bug class of "state shared across functions not designed to share it": in the ERC20 report, one allowance was usable by multiple unrelated pool functions (`requestRedeem`, `redeem`, `removeShares`); here, one replay-window/counter state (`cs.window`, keyed by `cs.dKey`) is usable by two unrelated packet-processing functions (`Decrypt` for direct `header.Message` traffic and `VerifyRelay` for `header.MessageRelay` traffic) over the same tunnel to the same directly-connected peer [3](#0-2) .

The dispatch logic in `readOutsidePackets` decides which function to call purely based on `h.Subtype == header.MessageRelay`, but routes both cases through the *same* `hostinfo.ConnectionState` (i.e., same `dKey`/`window`) that is looked up via `f.hostMap.QueryIndex`/`QueryRelayIndex` for the tunnel to the adjacent peer [4](#0-3) . On the sending side, `sendNoMetrics` (regular messages) and `SendVia` (relay frames) both draw from the identical `ci.messageCounter`/`via.ConnectionState.messageCounter` atomic counter on the same `ConnectionState`, so the counter space used to seed AEAD nonces and populate the replay window is shared across both message classes [5](#0-4) [6](#0-5) .

Because a single peer-to-peer `ConnectionState` (and thus a single replay window) backs both "acting as a relay for someone else" traffic and "talking directly to this peer" traffic, an adjacent peer that legitimately controls when/how many relay frames vs. direct message frames it sends to this node can cause counter values consumed by one code path to mark the shared window as "seen" for a counter that the other code path will also want to use, and vice versa. Since `Check`/`Update` treat "already used counter" identically regardless of which function consumed it, this conflates two functionally distinct authorization/authentication contexts into one piece of mutable state — exactly the "misuse by exploiting shared state not intended to be shared across functions" pattern the analog report describes, rather than genuine attacker forgery.

### Impact Explanation
If the counter/replay-window state is unintentionally shared between the relay-authentication path and the direct-message decryption path, a directly connected peer can cause legitimate direct-message or relay traffic to be dropped as "already seen" (`ErrAlreadySeen`), a remote state-poisoning/DoS effect on the shared window. This is a low-severity design flaw analogous to the reported "Low/Acknowledged" ERC20 finding — it does not by itself grant an unauthenticated remote attacker who lacks a CA-signed certificate the ability to bypass authentication or forge traffic, because both `Decrypt` and `VerifyRelay` still require possession of the correct `dKey` derived from a completed, certificate-authenticated Noise handshake.

### Likelihood Explanation
Likelihood is limited to a peer that already has an established, authenticated `ConnectionState` with the victim node (i.e., a peer that completed handshake and is used as a relay hop) — it does not extend to arbitrary unauthenticated attackers. The mechanism it depends on (mixing counters between `Decrypt` and `VerifyRelay` on one shared window) is triggered simply by that peer's normal ability to send both direct-message and relay traffic through the same tunnel, which is an available capability rather than a crafted exploit.

### Recommendation
Give `VerifyRelay` its own dedicated anti-replay window (and consider a separate/derived key or explicit domain separation in the nonce) instead of sharing `cs.window` and `cs.dKey`/`messageCounter` with `Decrypt`. This mirrors the "introduce a separate wrapped mechanism" recommendation in the analog report: rather than reusing one shared piece of state for two distinct functional purposes, each purpose (direct-message replay protection vs. relay-frame replay protection) should maintain independent state scoped to its own function.

### Proof of Concept
Not exploitable end-to-end via tool inspection alone (would require running the two paths against a live tunnel to observe cross-function window collisions); the structural sharing is demonstrated directly in code:
- `Decrypt` and `VerifyRelay` both operate on `cs.window`/`cs.dKey` [2](#0-1) .
- `readOutsidePackets` routes both `header.Message` and `header.MessageRelay` subtypes to the same `hostinfo.ConnectionState` before calling `Decrypt` or `VerifyRelay` respectively [7](#0-6) .
- Both `sendNoMetrics` and `SendVia` increment the same `ConnectionState.messageCounter` [5](#0-4) [6](#0-5) .

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

**File:** outside.go (L86-124)
```go
	// Relay packets are special
	isMessageRelay := (h.Type == header.Message && h.Subtype == header.MessageRelay)

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
```

**File:** inside.go (L296-299)
```go
	}
	c := via.ConnectionState.messageCounter.Add(1)

	out = header.Encode(out, header.Version, header.Message, header.MessageRelay, relay.RemoteIndex, c)
```

**File:** inside.go (L364-367)
```go
	c := ci.messageCounter.Add(1)

	//l.WithField("trace", string(debug.Stack())).Error("out Header ", &Header{Version, t, st, 0, hostinfo.remoteIndexId, c}, p)
	out = header.Encode(out, header.Version, t, st, hostinfo.remoteIndexId, c)
```
