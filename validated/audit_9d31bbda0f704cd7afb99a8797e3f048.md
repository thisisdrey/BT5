### Title
Unbounded pending-handshake resource allocation on every unauthenticated stage-1 packet allows remote CPU/memory exhaustion - (File: handshake_manager.go)

### Summary
The "lack of supply limit" report describes an unbounded resource-consuming operation (selling collateral) with no cap tied to actual capacity, allowing an attacker to overwhelm the system. The analogous condition in Nebula is `HandshakeManager.HandleIncoming` / `beginHandshake`, which builds a full Noise `handshake.Machine` (ECDH key derivation, `noise.HandshakeState` construction) and processes a full handshake message for *every* inbound UDP packet that merely looks like a stage-1 handshake (`h.MessageCounter == 1`, `h.RemoteIndex == 0`), before any certificate belonging to a trusted CA has been verified. There is no limit on the rate or number of such unauthenticated handshake attempts a remote, uncertified attacker can trigger.

### Finding Description
`HandleIncoming` dispatches any packet with `h.MessageCounter == 1` and `h.RemoteIndex == 0` straight to `beginHandshake`, gated only by subtype and a coarse "remote allow list" check — no proof of possession of a CA-signed certificate is required to reach this point: [1](#0-0) 

`beginHandshake` then unconditionally constructs a brand-new `handshake.Machine` (which builds a Noise `HandshakeState`, i.e., performs elliptic-curve operations) and calls `machine.ProcessPacket`, for every such packet, regardless of whether the packet's embedded certificate will ultimately validate: [2](#0-1) 

`NewMachine` itself performs cryptographic handshake-state setup (`cred.buildHandshakeState`) synchronously on every call: [3](#0-2) 

Only *after* this expensive Machine is built and the packet is processed does certificate verification occur (via `hm.certVerifier()`, which calls `CAPool.VerifyCertificate`): [4](#0-3) 

Nebula does have a per-vpn-ip cap on *established* host infos (`MaxHostInfosPerVpnIp = 5`) in the main hostmap: [5](#0-4) 

However, there is no equivalent "supply limit" gating the *rate or volume* of unauthenticated stage-1 handshake attempts that are allowed to consume CPU/crypto resources before a certificate is validated — exactly the missing control described in the report ("no supply limit... to match market liquidity/capacity"). `HandleIncoming` performs no per-source-IP throttling, no global concurrency cap on in-flight `beginHandshake` invocations, and no early lightweight cert-signature/CA check before allocating the Noise state.

### Impact Explanation
An attacker with no CA-signed certificate can flood the UDP listener with forged stage-1 handshake packets (`MessageCounter == 1`, `RemoteIndex == 0`, arbitrary payload). Each such packet forces the responder to perform full Noise handshake-state construction and message processing before the (invalid) embedded certificate is ever checked against the CA pool. Because this cost is paid unconditionally and without any supply/rate limit, a sufficiently high volume of such packets can exhaust CPU on the lighthouse/relay/responder node, denying service to legitimate peers — analogous to how an unbounded liquidation sale with no supply limit can overwhelm market capacity and produce systemic damage (bad debt).

### Likelihood Explanation
High likelihood of reachability: the check gating entry to `beginHandshake` only inspects header fields (`MessageCounter`, `RemoteIndex`) that are entirely attacker-controlled and require no valid certificate, and the remote allow list check is a coarse IP/CIDR filter, not an authentication mechanism. Any host that can reach the UDP port (which is the entire threat model for lighthouses/public relays) can trigger this path without ever holding a valid certificate.

### Recommendation
Introduce a "supply limit" analog for unauthenticated handshake processing: rate-limit or cap the number of concurrent/in-flight `beginHandshake` invocations per source address (and globally), and/or perform a cheap pre-check (e.g., basic packet/cert structural sanity and issuer-lookup in the CA pool) before constructing the expensive `handshake.Machine`/Noise state, so that cryptographic work is not performed for packets that can never pass certificate verification.

### Proof of Concept
Not independently executed; based on static code review only. Conceptually:
1. Attacker crafts UDP packets with a valid Nebula header (`Type = Handshake`, `Subtype = HandshakeIXPSK0`, `MessageCounter = 1`, `RemoteIndex = 0`) and an arbitrary/garbage handshake payload (no valid CA-signed certificate).
2. Attacker sends a high volume of such packets to a Nebula lighthouse/relay UDP port from many source addresses/ports.
3. Each packet reaches `beginHandshake`, which builds a new `handshake.Machine` and runs `ProcessPacket` (Noise crypto operations) before the embedded certificate is checked against the CA pool, consuming CPU disproportionate to the attacker's cost of generating garbage packets, with no built-in limiting mechanism to throttle this work.

### Citations

**File:** handshake_manager.go (L172-185)
```go
	// First message of a new handshake. The wire format requires RemoteIndex
	// to be zero here (the initiator has no responder index to fill in yet),
	// and generateIndex never allocates 0, so any non-zero RemoteIndex on a
	// stage-1 packet is malformed or someone probing for an index collision.
	// Drop without paying the cost of running noise on a pending Machine.
	if h.MessageCounter == 1 {
		if h.RemoteIndex != 0 {
			hm.l.Debug("dropping stage-1 handshake with non-zero RemoteIndex",
				"from", via, "remoteIndex", h.RemoteIndex)
			return
		}
		hm.beginHandshake(via, packet, h)
		return
	}
```

**File:** handshake_manager.go (L701-726)
```go
func (hm *HandshakeManager) beginHandshake(via ViaSender, packet []byte, h *header.H) {
	f := hm.f
	cs := f.pki.getCertState()

	v := cs.DefaultVersion()
	if cs.GetCredential(v) == nil {
		f.l.Error("Unable to handshake with host because no certificate is available",
			"from", via, "certVersion", v)
		return
	}

	machine, err := handshake.NewMachine(
		v, cs.GetCredential,
		hm.certVerifier(), func() (uint32, error) { return generateIndex(f.l) },
		false, header.HandshakeIXPSK0,
	)
	if err != nil {
		f.l.Error("Failed to create handshake machine", "from", via, "error", err)
		return
	}

	response, result, err := machine.ProcessPacket(nil, packet)
	if err != nil {
		f.l.Error("Failed to process handshake packet", "from", via, "error", err)
		return
	}
```

**File:** handshake_manager.go (L1161-1166)
```go
// certVerifier returns a CertVerifier that validates certs against the current CA pool.
func (hm *HandshakeManager) certVerifier() handshake.CertVerifier {
	return func(c cert.Certificate) (*cert.CachedCertificate, error) {
		return hm.f.pki.GetCAPool().VerifyCertificate(time.Now(), c)
	}
}
```

**File:** handshake/machine.go (L76-112)
```go
func NewMachine(
	version cert.Version,
	getCred GetCredentialFunc,
	verifier CertVerifier,
	allocIndex IndexAllocator,
	initiator bool,
	subtype header.MessageSubType,
) (*Machine, error) {
	info, err := subtypeInfoFor(subtype)
	if err != nil {
		return nil, err
	}

	cred := getCred(version)
	if cred == nil {
		return nil, fmt.Errorf("%w: %v", ErrNoCredential, version)
	}

	hs, err := cred.buildHandshakeState(initiator, info.pattern)
	if err != nil {
		return nil, fmt.Errorf("build noise state: %w", err)
	}

	return &Machine{
		hs:         hs,
		subtype:    subtype,
		msgs:       info.msgs,
		getCred:    getCred,
		allocIndex: allocIndex,
		verifier:   verifier,
		myVersion:  version,
		result: &Result{
			Initiator: initiator,
			Cipher:    cred.cipherSuite,
		},
	}, nil
}
```

**File:** hostmap.go (L29-31)
```go
// MaxHostInfosPerVpnIp is the max number of hostinfos we will track for a given vpn ip
// 5 allows for an initial handshake and each host pair re-handshaking twice
const MaxHostInfosPerVpnIp = 5
```
