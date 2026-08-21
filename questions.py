import json
import os

from decouple import config

# todo: if scope_files is: 500 > 50, 300 > 30 , 100 > 10
MAX_REPO = 10
# todo: the GitLab namespace/project path, for example group/project
SOURCE_REPO = "slackhq/nebula"
# todo: the name of the repository
REPO_NAME = "nebula"

run_number = os.environ.get('GITHUB_RUN_NUMBER', '0')


def get_cyclic_index(run_number, max_index=100):
    """Convert run number to a cyclic index between 1 and max_index"""
    return (int(run_number) - 1) % max_index + 1


def load_repository_urls():
    """Load repository URLs from repositories.json."""
    repo_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "repositories.json")
    if not os.path.exists(repo_file):
        return []

    try:
        with open(repo_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    if not isinstance(data, list):
        return []

    return [url for url in data if isinstance(url, str) and url.strip()]


if run_number == "0":
    BASE_URL = f"https://deepwiki.com/{SOURCE_REPO}"
else:
    repository_urls = load_repository_urls()
    if repository_urls:
        run_index = get_cyclic_index(run_number, len(repository_urls))
        BASE_URL = repository_urls[run_index - 1]
    else:
        BASE_URL = f"https://deepwiki.com/{SOURCE_REPO}"

scope_files = [
    # =================================================================================
    # Untrusted UDP ingress, packet headers, and wire parsing
    # =================================================================================
    "outside.go",
    "header/header.go",
    "iputil/packet.go",
    "udp/conn.go",
    "udp/errors.go",
    "udp/udp_generic.go",
    "udp/udp_linux.go",
    "udp/udp_linux_64.go",
    "udp/udp_linux_32.go",
    "udp/udp_bsd.go",
    "udp/udp_darwin.go",
    "udp/udp_android.go",
    "udp/udp_windows.go",
    "udp/udp_rio_windows.go",
    "udp/netchange.go",
    "udp/netchange_generic.go",

    # =================================================================================
    # Handshake state machine and peer authentication trust boundary
    # =================================================================================
    "handshake_manager.go",
    "handshake/machine.go",
    "handshake/credential.go",
    "handshake/payload.go",
    "handshake/patterns.go",
    "handshake/errors.go",
    "connection_state.go",
    "connection_manager.go",
    "pki.go",

    # =================================================================================
    # Certificate parsing, signature verification, and CA trust decisions
    # =================================================================================
    "cert/cert.go",
    "cert/cert_v1.go",
    "cert/cert_v2.go",
    "cert/ca_pool.go",
    "cert/asn1.go",
    "cert/crypto.go",
    "cert/sign.go",
    "cert/pem.go",
    "cert/errors.go",
    "cert/p256/p256.go",

    # =================================================================================
    # Session crypto: cipher state, nonce/counter handling, and replay windows
    # =================================================================================
    "noiseutil/cipher_state.go",
    "noiseutil/aesgcm.go",
    "noiseutil/chachapoly.go",
    "noiseutil/nist.go",
    "noiseutil/boring.go",
    "noiseutil/notboring.go",
    "boring.go",
    "notboring.go",
    "bits.go",

    # =================================================================================
    # Firewall, group/CIDR policy enforcement, and inside-to-outside gating
    # =================================================================================
    "firewall.go",
    "firewall/packet.go",
    "firewall/cache.go",
    "allow_list.go",
    "inside.go",
    "inside_generic.go",
    "inside_bsd.go",

    # =================================================================================
    # Peer address trust: hostmap, lighthouse, relays, roaming, and punching
    # =================================================================================
    "hostmap.go",
    "lighthouse.go",
    "remote_list.go",
    "relay_manager.go",
    "calculated_remote.go",
    "punchy.go",
    "routing/balance.go",
    "routing/gateway.go",

    # =================================================================================
    # Tunnel egress, virtual device, and route installation
    # =================================================================================
    "interface.go",
    "overlay/tun.go",
    "overlay/device.go",
    "overlay/route.go",
    "overlay/user.go",
    "overlay/tun_linux.go",
    "overlay/tun_darwin.go",
    "overlay/tun_windows.go",
    "overlay/tun_notwin.go",
    "overlay/tun_disabled.go",
    "wintun/tun.go",
    "wintun/device.go",
    "wfp/wfp_windows.go",

    # =================================================================================
    # Local control surfaces, config reload, and process lifecycle
    # =================================================================================
    "main.go",
    "control.go",
    "config/config.go",
    "config/default.go",
    "ssh.go",
    "sshd/server.go",
    "sshd/session.go",
    "sshd/command.go",
    "sshd/writer.go",
    "dns_server.go",
    "service/service.go",
    "service/listener.go",
    "scheduler.go",
    "timeout.go",
    "stats.go",
    "message_metrics.go",
    "util/error.go",
    "logging/logger.go",
    "cmd/nebula/main.go",
    "cmd/nebula-service/main.go",
    "cmd/nebula-service/service.go",
]


target_scopes = [
    "Critical. An unprivileged attacker holding no CA-signed Nebula certificate, sending only UDP packets to a node's listener, can complete or be granted a tunnel session and reach the overlay network as an authenticated host.",
    "Critical. An unprivileged attacker can get a forged, self-signed, malformed, or version-confused certificate accepted by CA-pool verification, so an untrusted identity, expiry, network/subnet, or group set is treated as CA-signed and authorized.",
    "Critical. An unprivileged attacker can break session crypto confidentiality or integrity, such as forcing nonce/counter reuse, key or cipher confusion, or replay-window bypass, so tunnel traffic can be decrypted, forged, or replayed into an established session.",
    "Critical. An unprivileged attacker can bypass firewall or unsafe-route policy so packets they inject reach inside services, tun-routed hosts, or the host network that the configured rules, groups, or CIDRs must deny.",
    "High. An unprivileged attacker spoofing UDP source addresses or sending unauthenticated lighthouse, relay, or handshake packets can poison hostmap/remote-list state, hijack roaming, or steer another host's traffic through an attacker-chosen path.",
    "High. An unprivileged attacker can use malformed packet headers, certificates, or handshake payloads to panic, deadlock, exhaust memory, or wedge a remote node's packet-processing path, taking the tunnel down for all its peers.",
]


scope_scan = [
]


def question_generator(target_file: str) -> str:
    """
    Generate exploit-focused audit and fuzzing questions for one nebula target.

    ```
    target_file format:
    "'File Name: outside.go -> Scope: Critical. ...'"
    """

    prompt = f"""
    ```

    Generate exploit-focused security audit and fuzzing questions for this exact nebula target:

    {target_file}

    Project focus:
    nebula is a Go overlay networking tool. Focus on untrusted UDP packet parsing, Noise handshake authentication, certificate and CA-pool verification, session crypto and nonce handling, firewall/group policy enforcement, and hostmap/lighthouse/relay address trust.

    Rules:
    * Treat `File Name:` as the exact file/package.
    * Treat `Scope:` as the ONLY impact to target.
    * Assume full repo context is accessible.
    * Do not ask for code or say anything is missing.
    * Use exact Go symbols (func, method, struct, field) when possible.
    * Attacker is unprivileged only: a network host with NO certificate signed by a trusted CA, no host/root access, no leaked keys, no config control, no CA compromise. They may send arbitrary UDP to the listener, spoof source addresses, and present self-signed or malformed certificates.
    * Never assume a malicious peer, malicious node, compromised lighthouse, or any attacker who already holds a valid CA-signed certificate.
    * Ignore test files, mocks, docs, generated files (*.pb.go), build scripts, config-only findings, and dependency-only issues.
    * Generate 12 to 16 high-signal questions.
    * At least 70% must target authentication bypass, certificate/CA verification flaws, session crypto or replay flaws, firewall policy bypass, or remote address/state poisoning.
    * Every question must be testable by unit test, integration test, fuzz test, invariant test, or differential test.
    * Avoid generic checklist questions and repeated root causes.

    Core invariants:
    * No packet is trusted before authentication: unauthenticated input must never mutate hostmap, lighthouse, relay, or session state, or select a decryption key.
    * Certificate verification is fail-closed: signature, CA chain, version, expiry, network/subnet, and group checks must all pass, and unknown or malformed fields must reject rather than default-allow.
    * Session crypto is sound: each key is used with a unique nonce, counters never rewind or wrap into reuse, and replayed or out-of-window packets are dropped.
    * Firewall enforcement is total: every inbound tunnel packet and every routed packet is checked against the sending certificate's identity, groups, and networks before it reaches the tun device.
    * Peer addressing is authenticated: a remote's underlay address, relay path, or roam only changes on cryptographically verified traffic from that peer.
    * Untrusted lengths and offsets never drive slicing, allocation, or unbounded loops in packet, header, ASN.1, or protobuf parsing.

    Each question must include:
    1. target function/package;
    2. attacker action;
    3. preconditions;
    4. call sequence;
    5. invariant tested;
    6. scoped impact;
    7. proof idea.

    Output only valid Python. No markdown. No explanations.

    questions = [
    "[File: {target_file}] [Function: symbol_or_package] Can an unprivileged ATTACKER_ACTION under PRECONDITIONS trigger CALL_SEQUENCE, violating INVARIANT, causing scoped impact: SCOPE_IMPACT? Proof idea: unit/integration/fuzz PARAMETERS and assert AUTH_ENFORCEMENT, CERT_VERIFICATION, CRYPTO_INTEGRITY, or FIREWALL_ENFORCEMENT.",
    ]
    """
    return prompt


def audit_format(security_question: str) -> str:
    """
    Generate a focused nebula exploit-validation prompt.
    """

    prompt = f"""# SECURITY AUDIT PROMPT

## Question
{security_question}

## Rules
- Use existing repo context only. Analyze only this question and scoped impact.
- Attacker is unprivileged only: no CA-signed certificate, no host/root access, no leaked keys, no config or CA control, no social engineering.
- Never assume a malicious peer, malicious node, or compromised lighthouse.
- Reject anything depending only on test/mock/config/docs/generated files, dependency bugs alone, or best-practice cleanup without exploitable impact.
- Focus on paths reachable from attacker-sent UDP packets, spoofed source addresses, or attacker-presented certificates.

## Validate
- Trace the exact reachable Go path from attacker input into handshake, certificate verification, session crypto, firewall, or hostmap/relay state.
- Check whether existing verification, drop paths, replay windows, or firewall checks already stop it.
- Accept only real authentication bypass, certificate/CA verification bypass, traffic decryption/forgery/replay, firewall bypass, remote state poisoning, or remote crash/wedge.
- Require exact file/function support and a reproducible unit/integration/fuzz/invariant PoC.

## Output
If valid, output exactly:

### Title
[Bug statement] - ([File: file_path])

### Summary
[2-3 sentences]

### Finding Description
[Code path, root cause, attacker inputs, exploit flow, and why checks fail]

### Impact Explanation
[Concrete scoped impact and matching Nebula bounty impact]

### Likelihood Explanation
[Preconditions, feasibility, repeatability]

### Recommendation
[Specific fix]

### Proof of Concept
[Unit/integration test or fuzz/invariant test plan with expected assertions]

If invalid, output exactly:
#NoVulnerability found for this question.

No extra text.
"""
    return prompt


def validation_format(report: str) -> str:
    """
    Generate a strict bounty-style validation prompt for nebula security claims.
    """
    prompt = f"""# VALIDATION PROMPT

## Security Claim
{report}

## Rules
- Validate only the submitted claim.
- Check SECURITY.md and Researcher.Md for scope, exclusions, and valid impact classes.
- Do not create a new vulnerability if the submitted claim is weak or invalid.
- Do not upgrade severity unless the provided evidence proves the higher impact.
- Reject malicious-peer, malicious-node, compromised-lighthouse, valid-certificate-holder, root/host-access, leaked-key, CA-compromise, local-network-only, physical-access, dependency-only, docs/style, generated-file, test/mock/config-only, and purely theoretical issues.
- Reject volumetric DDoS, resource exhaustion requiring flooding, and issues needing operator misconfiguration.
- A valid report must be triggerable by a network attacker holding no CA-signed certificate, unless the claim proves escalation from that unprivileged position.
- The final impact must map to an in-scope Nebula impact such as authentication bypass onto the overlay, certificate/CA verification bypass, tunnel traffic decryption/forgery/replay, firewall policy bypass, hostmap/relay/roaming poisoning, or remote node crash from a single crafted packet.
- Prefer #NoVulnerability over speculative reports.

## Required Validation Checks
All must pass:
1. Exact in-scope file, function, and line/code references.
2. Clear root cause and broken security assumption.
3. Reachable exploit path: preconditions -> attacker action -> trigger -> bad result.
4. Existing checks/guards reviewed and shown insufficient.
5. Concrete in-scope impact with realistic likelihood.
6. Reproducible proof path: unit PoC, integration test, invariant/fuzz test, or exact packet-level steps.
7. No obvious rejection reason from SECURITY.md, known issues, privilege assumptions, or scope exclusions.

## Silent Triage Questions
Before output, internally answer:
- Can an attacker with no valid certificate and no host access trigger this?
- Does the code actually behave as claimed?
- Is the impact caused by this code, not by configuration, a dependency, or a trusted peer's behavior?
- Is the bypass, disclosure, forgery, or crash concrete, not hypothetical?
- Would a bounty triager accept the proof?
- What exact test would prove it?

## Output
If valid, output exactly:

Audit Report

## Title
[Clear vulnerability statement] - ([File: file_path])

## Summary
[2-3 sentence summary of the bug and impact]

## Finding Description
[Exact code path, root cause, exploit flow, and why existing checks fail]

## Impact Explanation
[Concrete in-scope impact, severity rationale, and bounty category]

## Likelihood Explanation
[Attacker capability, required conditions, feasibility, repeatability]

## Recommendation
[Specific fix guidance]

## Proof of Concept
[Minimal reproducible steps or fuzz/invariant/integration test plan]

If invalid, output exactly:
#NoVulnerability found for this question.

Output only one of the two outcomes above. No extra text.
"""
    return prompt


def scan_format(report: str) -> str:
    """
    Generate a short cross-project analog scan prompt for nebula.
    """
    prompt = f"""# ANALOG SCAN PROMPT

## External Report
{report}

## Rules
- Use in-scope production repo context only. Do not ask for code or claim missing files.
- Use the external report only as a bug-class hint, not as proof.
- Keep only analogs reachable by an attacker with no CA-signed certificate: packet/header/ASN.1 parsing, handshake authentication, certificate and CA-pool verification, nonce/replay handling, firewall enforcement, or hostmap/lighthouse/relay address trust.
- Reject malicious-peer/node/lighthouse analogs, valid-certificate-holder analogs, host-access analogs, test-only paths, dependency-only bugs, and no-impact analogs.

## Validate
- Map the bug class to the strongest reachable nebula path.
- Prove root cause with exact file/function support.
- Accept only concrete authentication bypass, certificate verification bypass, traffic decryption/forgery/replay, firewall bypass, remote state poisoning, or remote crash impact.

## Output (Strict)
If valid analog exists, output:

### Title
[Clear vulnerability statement] - ([File: file_path])

### Summary
### Finding Description
### Impact Explanation
### Likelihood Explanation
### Recommendation
### Proof of Concept

If not, output exactly:
#NoVulnerability found for this question.

No extra text.
"""
    return prompt
