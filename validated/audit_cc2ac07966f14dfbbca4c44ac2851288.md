### Title
Timing side-channel in HTTP `Authorization` header comparison across `/v3` endpoints - (File: `stackslib/src/net/api/postblock_proposal.rs`, `postblock_v3.rs`, `blockreplay.rs`, `blocksimulate.rs`, `txsimulate.rs`, `fastcallreadonly.rs`)

### Summary
Six privileged `/v3` HTTP RPC handlers in `stackslib/src/net/api/` authenticate the caller by comparing the request's `authorization` header against a locally configured secret (`auth_token`) using Rust's default (non-constant-time) `!=` string comparison, instead of a constant-time comparison. This is directly analogous to the `django-basic-auth-ip-whitelist` CWE-208 timing-attack advisory, where character-by-character comparison of credentials leaked timing information usable to reconstruct the secret.

### Finding Description
Each of the following handlers implements `try_parse_request` and gates access with the same pattern: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) 

In every case, the check is:
```rust
let Some(auth_header) = preamble.headers.get("authorization") else {
    return Err(Error::Http(401, "Unauthorized".into()));
};
if auth_header != password {
    return Err(Error::Http(401, "Unauthorized".into()));
}
```

`String`/`&str` equality in Rust (`PartialEq`) is implemented as a length check followed by `memcmp`-style byte comparison via `slice::eq`, which does not guarantee constant time and can short-circuit on length mismatch or on the first differing byte on many implementations/optimization levels. This is the same bug class as CVE-2020-4071: a remote, unauthenticated attacker measuring response latency for different guessed prefixes of the `Authorization` value can, in principle, incrementally infer the correct `auth_token` byte-by-byte, since a longer matching prefix causes the comparison to run marginally longer before failing.

This `auth_token` (configured via `connection_options.auth_token`, see `stackslib/src/config/mod.rs`) is the sole gate protecting:
- `/v3/block_proposal` — accepts attacker-controlled Nakamoto block proposals for validation [7](#0-6) 
- `/v3/blocks/upload/?broadcast=1` — authenticated block broadcast [8](#0-7) 
- `/v3/blocks/replay/*`, `/v3/blocks/simulate/*`, `/v3/transactions/simulate` — internal debug/replay compute endpoints.

### Impact Explanation
If an attacker recovers the `auth_token` via timing analysis, they gain unauthorized access to privileged endpoints, most notably the ability to submit forged `/v3/block_proposal` requests or force `broadcast=1` block uploads that would otherwise require authentication — i.e., an auth-bypass leading to unauthorized interaction with node/miner internals. This aligns with the "auth bypass" category in the rules' Critical bucket, though it requires successfully mounting a timing attack, which is intrinsically probabilistic and requires either close network proximity/low jitter or many samples (as also caveated in the original advisory: “this attack is understood not to be realistic over the Internet... may be achieved from within local networks”).

### Likelihood Explanation
Low-to-Medium. The comparison happens once per HTTP request and involves network-induced jitter, so exploiting it over the open Internet is impractical; it is more feasible from a low-latency vantage point (same data center/LAN as the node, or a colocated attacker). This mirrors the CVSS vector in the reference advisory (`AV:P`, `AC:H`), i.e., a physically/network-proximate attacker with high attack complexity. No memory corruption or crash is involved — this is a purely observational side channel.

### Recommendation
Replace the direct `!=`/`==` string comparisons against `password`/`auth_token` in `postblock_proposal.rs`, `postblock_v3.rs`, `blockreplay.rs`, `blocksimulate.rs`, `txsimulate.rs`, and `fastcallreadonly.rs` with a constant-time comparison (e.g., `subtle::ConstantTimeEq`, or a length-first-then-XOR-accumulate compare-and-only-branch-once approach) so that the time taken does not depend on how many leading bytes match.

### Proof of Concept
1. Configure a node with `connection_options.auth_token = Some("<secret>")` enabling `/v3/block_proposal`.
2. From a low-latency vantage point, send repeated POST requests to `/v3/block_proposal` with `authorization` headers guessing byte-by-byte prefixes of the token (e.g., `"a"`, `"b"`, ... then `"<correct_prefix>a"`, `"<correct_prefix>b"`, ...).
3. Statistically aggregate response latencies for the early-exit path in the `auth_header != password` comparison (`stackslib/src/net/api/postblock_proposal.rs:1142`) to distinguish prefixes that match longer (slightly higher latency before returning 401) from those that mismatch immediately.
4. Repeat to reconstruct the full token, then use it to submit an authenticated block proposal/broadcast the attacker would otherwise be denied.

Note: I could not execute this PoC or measure real-world timing variance in this environment; the finding is based on static code review of the comparison logic, consistent with the reported bug class (CWE-208).

### Citations

**File:** stackslib/src/net/api/postblock_proposal.rs (L1092-1103)
```rust
pub struct RPCBlockProposalRequestHandler {
    pub block_proposal: Option<NakamotoBlockProposal>,
    pub auth: Option<String>,
}

impl RPCBlockProposalRequestHandler {
    pub fn new(auth: Option<String>) -> Self {
        Self {
            block_proposal: None,
            auth,
        }
    }
```

**File:** stackslib/src/net/api/postblock_proposal.rs (L1136-1144)
```rust
        let Some(password) = &self.auth else {
            return Err(Error::Http(400, "Bad Request.".into()));
        };
        let Some(auth_header) = preamble.headers.get("authorization") else {
            return Err(Error::Http(401, "Unauthorized".into()));
        };
        if auth_header != password {
            return Err(Error::Http(401, "Unauthorized".into()));
        }
```

**File:** stackslib/src/net/api/postblock_v3.rs (L99-122)
```rust
        // if broadcast=1 is set, then the requester must be authenticated
        let mut broadcast = false;
        let mut authenticated = false;

        // look for authorization header
        if let Some(password) = &self.auth {
            if let Some(auth_header) = preamble.headers.get("authorization") {
                if auth_header != password {
                    return Err(Error::Http(401, "Unauthorized".into()));
                }
                authenticated = true;
            }
        }

        // see if broadcast=1 is set
        for (key, value) in form_urlencoded::parse(query.as_ref().unwrap_or(&"").as_bytes()) {
            if key == "broadcast" {
                broadcast = broadcast || value == "1";
            }
        }

        if broadcast && !authenticated {
            return Err(Error::Http(401, "Unauthorized".into()));
        }
```

**File:** stackslib/src/net/api/blockreplay.rs (L575-583)
```rust
        let Some(password) = &self.auth else {
            return Err(Error::Http(400, "Bad Request.".into()));
        };
        let Some(auth_header) = preamble.headers.get("authorization") else {
            return Err(Error::Http(401, "Unauthorized".into()));
        };
        if auth_header != password {
            return Err(Error::Http(401, "Unauthorized".into()));
        }
```

**File:** stackslib/src/net/api/blocksimulate.rs (L153-161)
```rust
        let Some(password) = &self.auth else {
            return Err(Error::Http(400, "Bad Request.".into()));
        };
        let Some(auth_header) = preamble.headers.get("authorization") else {
            return Err(Error::Http(401, "Unauthorized".into()));
        };
        if auth_header != password {
            return Err(Error::Http(401, "Unauthorized".into()));
        }
```

**File:** stackslib/src/net/api/txsimulate.rs (L352-360)
```rust
        let Some(password) = &self.auth else {
            return Err(Error::Http(400, "Bad Request.".into()));
        };
        let Some(auth_header) = preamble.headers.get("authorization") else {
            return Err(Error::Http(401, "Unauthorized".into()));
        };
        if auth_header != password {
            return Err(Error::Http(401, "Unauthorized".into()));
        }
```

**File:** stackslib/src/net/api/fastcallreadonly.rs (L44-48)
```rust
#[derive(Clone)]
pub struct RPCFastCallReadOnlyRequestHandler {
    pub call_read_only_handler: RPCCallReadOnlyRequestHandler,
    pub auth: Option<String>,
}
```
