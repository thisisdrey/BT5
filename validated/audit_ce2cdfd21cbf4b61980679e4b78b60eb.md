### Title
Unauthenticated stage-1 handshake packets force expensive Noise crypto work with no per-source cost or rate limit, enabling a cheap flood that starves legitimate handshakes - (File: handshake_manager.go)

### Summary
The Sherlock finding describes a cost asymmetry: an attacker can trigger an expensive/valuable operation (point minting) with a cheap, repeatable action (deposit+withdraw), and because the mitigation is a *shared* rate limit rather than a per-attacker cost, the attacker can also starve legitimate users of the same resource. The reachable analog in Nebula is the handshake responder path: any unauthenticated UDP sender can force a peer to run full Noise `IX` handshake cryptography for every spoofed stage-1 packet, with no per-source throttle, so a flood of cheap forged packets can consume the responder's handshake-processing capacity and delay/deny legitimate peers' handshakes.

### Finding Description
`HandshakeManager.HandleIncoming` dispatches any packet with `MessageCounter == 1` and `RemoteIndex == 0` straight into `beginHandshake`, gated only by the subtype check and the optional `remote_allow_list` (which defaults to allow-all and is not a cost function): [1](#0-0) 

`beginHandshake` unconditionally constructs a new `handshake.Machine` and calls `machine.ProcessPacket`, which performs the Noise IX `ReadMessage` (ephemeral/static Diffie-Hellman operations) *before* any certificate is verified: [2](#0-1) 

Certificate verification (`validateCert`, which calls the CA-pool verifier) only happens after the DH computation has already been paid for by the responder: [3](#0-2) 

The only pre-crypto filter is the structural check that `RemoteIndex == 0` for a stage-1 message, which an attacker can trivially satisfy since it is a fixed wire-format requirement, not a proof of work or a per-source credential: [4](#0-3) 

There is no per-source-IP rate limiter, cookie/puzzle mechanism, or accounting on stage-1 packets anywhere in this path (confirmed via `handshake_manager.go`, `outside.go`, and the `RemoteAllowList` check, which is an optional allow/deny IP list, not a rate limiter): [5](#0-4) 

This mirrors the reported bug class: a cheap, freely repeatable action (send a 1-packet-sized forged handshake) forces the victim to spend a disproportionate, valuable resource (asymmetric crypto CPU cycles) with no cost or limit tied to the attacker's identity, and because the responder's handshake-processing capacity is shared, one attacker flooding forged packets can starve legitimate peers of timely handshake completion — directly analogous to "the attacker can mint a large amount of points and prevent other users from receiving them."

### Impact Explanation
An attacker with no CA-signed certificate can send arbitrary garbage stage-1 UDP packets from spoofed or unspoofed source addresses. Each packet forces the target Nebula node to run full Noise IX handshake cryptography (ECDH operations) before it can detect the packet is invalid. Because this cost is paid by the responder for every packet regardless of validity, and there is no rate limit or increasing cost per source, an attacker can degrade or deny the responder's ability to process legitimate handshakes from real peers — a remote resource-exhaustion / denial-of-service condition reachable without holding a valid certificate.

### Likelihood Explanation
Likelihood is high for triggering the condition (sending a well-formed stage-1 header with `RemoteIndex=0` and `MessageCounter=1` requires no secret, no certificate, and can be scripted trivially and sent at line rate/from many sources), though the ability to fully deny service depends on the target's available CPU margin and network bandwidth, similar to how the original finding's severity depended on the rate limit's global vs. per-user scope.

### Recommendation
Add a per-source (or per-source-subnet) rate limit / cost gate on stage-1 handshake processing before invoking `handshake.NewMachine`/`ProcessPacket`, e.g., a lightweight stateless cookie/puzzle exchange, or a token-bucket keyed by underlay address, so that the crypto cost of `beginHandshake` cannot be triggered for free and at unlimited rate by a single attacker. Ensure any such limit is per-source rather than global, so a single attacker cannot exhaust a shared budget and block other peers' handshakes — mirroring the audit's point about a global rate limit still allowing starvation of other users.

### Proof of Concept
1. Attacker crafts a UDP packet with a valid Nebula header: `Type=Handshake`, `Subtype=HandshakeIXPSK0`, `RemoteIndex=0`, `MessageCounter=1`, and a body containing arbitrary/garbage bytes in place of a real Noise ephemeral key + certificate payload (as exercised by `makeHandshakePacket` in the test suite, e.g. `TestHandshakeMessageCounter0Dropped`/`TestHandshakeUnknownMessageCounter` patterns): [6](#0-5) 
2. Attacker sends a high rate of such packets (varying the garbage payload/source port) to the victim's UDP listener.
3. `outside.go` routes every one of them to `HandshakeManager.HandleIncoming` → `beginHandshake`, and each one causes a full `handshake.Machine.ProcessPacket` call (Noise DH) before failing certificate validation and being discarded — with no per-source throttling anywhere in this chain.
4. Repeating this at volume consumes the responder's CPU handling illegitimate handshakes, delaying/denying `ProcessPacket` cycles available to legitimate peers' real stage-1 packets, analogous to the reported "attacker mints points at no cost and can prevent other users from earning them."

Note: I was not able to fully verify the exact CPU cost of a single `ReadMessage` call or measure real-world flood throughput needed to cause denial of service, since that requires runtime benchmarking rather than static code reading — this should be validated experimentally before treating the DoS impact as proven at scale.

### Citations

**File:** handshake_manager.go (L151-185)
```go
func (hm *HandshakeManager) HandleIncoming(via ViaSender, packet []byte, h *header.H) {
	// Gate on known handshake subtypes. Unknown subtypes (or future ones we
	// don't yet support) are dropped here rather than silently routed through
	// the IX path. Add a case when introducing a new pattern.
	switch h.Subtype {
	case header.HandshakeIXPSK0:
		// supported
	default:
		hm.l.Debug("dropping handshake with unsupported subtype",
			"from", via, "subtype", h.Subtype)
		return
	}

	// First remote allow list check before we know the vpnIp
	if !via.IsRelayed {
		if !hm.lightHouse.GetRemoteAllowList().AllowUnknownVpnAddr(via.UdpAddr.Addr()) {
			hm.l.Debug("lighthouse.remote_allow_list denied incoming handshake", "from", via)
			return
		}
	}

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

**File:** handshake/machine.go (L342-380)
```go
func (m *Machine) validateCert(payload Payload) error {
	cred := m.getCred(m.myVersion)
	if cred == nil {
		m.failed = true
		return fmt.Errorf("%w: %v", ErrNoCredential, m.myVersion)
	}
	rc, err := cert.Recombine(
		cert.Version(payload.CertVersion),
		payload.Cert,
		m.hs.PeerStatic(),
		cred.Cert.Curve(),
	)
	if err != nil {
		m.failed = true
		return fmt.Errorf("recombine cert: %w", err)
	}

	if !bytes.Equal(rc.PublicKey(), m.hs.PeerStatic()) {
		m.failed = true
		return ErrPublicKeyMismatch
	}

	// Version negotiation, if the peer sent a different version and we have it, switch
	if rc.Version() != m.myVersion {
		if m.getCred(rc.Version()) != nil {
			m.myVersion = rc.Version()
		}
	}

	verified, err := m.verifier(rc)
	if err != nil {
		m.failed = true
		return fmt.Errorf("verify cert: %w", err)
	}

	m.result.RemoteCert = verified
	m.remoteCertSet = true
	return nil
}
```

**File:** outside.go (L76-84)
```go
	switch h.Type {
	case header.Handshake:
		f.handshakeManager.HandleIncoming(via, packet, h)
		return

	case header.RecvError:
		f.handleRecvError(via.UdpAddr, h)
		return
	}
```

**File:** e2e/handshake_manager_test.go (L330-349)
```go
func TestHandshakeMessageCounter0Dropped(t *testing.T) {
	t.Parallel()
	// MessageCounter=0 is not a valid handshake message and should be dropped.

	ca, _, caKey, _ := cert_test.NewTestCaCert(cert.Version1, cert.Curve_CURVE25519, time.Now(), time.Now().Add(10*time.Minute), nil, nil, []string{})
	myControl, _, myUdpAddr, _ := newSimpleServer(cert.Version1, ca, caKey, "me", "10.128.0.1/24", nil)
	_, _, theirUdpAddr, _ := newSimpleServer(cert.Version1, ca, caKey, "them", "10.128.0.2/24", nil)

	myControl.Start()

	t.Log("Inject handshake with MessageCounter=0")
	myControl.InjectUDPPacket(makeHandshakePacket(theirUdpAddr, myUdpAddr, header.HandshakeIXPSK0, 0, 0))

	time.Sleep(100 * time.Millisecond)
	assert.Empty(t, myControl.ListHostmapHosts(false))
	assert.Empty(t, myControl.ListHostmapHosts(true))
	assert.Nil(t, myControl.GetFromUDP(false))

	myControl.Stop()
}
```
