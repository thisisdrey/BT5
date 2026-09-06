### Title
Non-constant-time auth-token comparison on privileged RPC endpoints enables remote timing side-channel to brute-force `auth_token` - (File: stackslib/src/net/api/postblock_proposal.rs)

### Summary
Every privileged RPC endpoint gated by the node's configured `auth_token` (`block_proposal`, `postblock_v3` broadcast, `blockreplay`, `blocksimulate`, `txsimulate`, `fastcallreadonly`) compares the client-supplied `authorization` header against the secret password using ordinary `String`/`&str` `!=` comparison, which short-circuits on the first mismatched byte. This is the same class of bug as the report's "equality not enforced correctly" issue (authenticated vs. stored value): here, the *comparison itself* leaks information about how many leading bytes matched, instead of being an atomic, timing-safe check.

### Finding Description
In `try_parse_request` for each of these handlers, the check is:
```rust
let Some(auth_header) = preamble.headers.get("authorization") else {
    return Err(Error::Http(401, "Unauthorized".into()));
};
if auth_header != password {
    return Err(Error::Http(401, "Unauthorized".into()));
}
``` [1](#0-0) 

The identical pattern (`auth_header != password`) appears in: [2](#0-1) [3](#0-2) [4](#0-3) 

Rust's `&str`/`String` `PartialEq` implementation is a byte-wise comparison that returns as soon as it finds a differing byte (after first checking length). This means the time taken to reject an incorrect `authorization` header is proportional to the length of the correct-byte prefix shared with the real secret. No constant-time comparison primitive (`subtle::ConstantTimeEq`, `ring::constant_time::verify_slices_are_equal`, or manual constant-time byte XOR-accumulation) is used anywhere in `stackslib/src/net/**` for this purpose — a repo-wide grep for `constant_time`/`ConstantTimeEq` under `stackslib/src/net/**` returned no hits.

Unlike the original Sherlock report (which concerned an *admin-trusted, permissioned* call), the `authorization` header check here is the actual authentication boundary presented to remote, unauthenticated clients on these HTTP RPC endpoints. `auth_token` gates functions with direct chain-affecting side effects (`postblock_v3` broadcast=1, `block_proposal` triggers block validation, `blocksimulate`/`txsimulate`/`fastcallreadonly` execute Clarity code against node state) as documented in `docs/rpc/openapi.yaml`: [5](#0-4) 

### Impact Explanation
A network-observable timing side channel on a secret-comparison in an authentication gate is a genuine remote auth-bypass vector: with enough measurement (statistical averaging over repeated requests, byte-at-a-time extension attack), a remote attacker can recover the full `auth_token` without ever knowing it, then use it to invoke privileged endpoints (e.g. `/v3/blocks/upload/?broadcast=1`, `/v3/block_proposal`) as if authenticated. This matches the rules' Critical category: "unauthenticated/unauthorized write to state ... auth bypass."

### Likelihood Explanation
Exploitability is nontrivial (network jitter reduces signal-to-noise, and the token length is presumably not tiny), but it requires no privileges, no valid credentials, and no assistance from any party — the attacker only sends repeated HTTP requests to a public RPC port and measures response latency. This is a well-known class of exploitable timing side-channel (similar to numerous historical CVEs against `==`-based token/password checks in web servers), and the fact that literally every privileged endpoint in this codebase repeats the same vulnerable pattern (6 occurrences) indicates a systemic gap rather than an isolated slip.

### Recommendation
Replace all `auth_header != password` comparisons with a constant-time comparison, e.g. using the `subtle` crate's `ConstantTimeEq`, or `ring::constant_time::verify_slices_are_equal`, applied uniformly across `blockreplay.rs`, `blocksimulate.rs`, `fastcallreadonly.rs`, `postblock_proposal.rs`, `postblock_v3.rs`, and `txsimulate.rs`. Factor this into a single shared helper (e.g. in `stackslib/src/net/httpcore.rs` alongside the `auth_token` field) so future endpoints inherit the safe comparison by default instead of re-implementing the check ad hoc.

### Proof of Concept
Conceptual PoC (not executed):
1. Configure a stacks-node with `connection_options.auth_token = "S3cr3tT0k3nValue"` and enable `/v3/block_proposal`.
2. From a remote, unauthenticated client, send repeated POST requests to `/v3/block_proposal` with candidate `authorization` header values, incrementing one guessed byte at a time (e.g. `"A", "B", ..., "S", "Sa", "Sb", ..., "S3", ...`), each time recording round-trip latency over many trials to average out network noise.
3. Because `auth_header != password` short-circuits at the first mismatching byte, correct-prefix guesses will show a measurably larger average processing time than incorrect ones (extra byte comparisons executed before returning `false`), allowing incremental byte-by-byte recovery of `auth_token`.
4. Once recovered, use the token to call `/v3/blocks/upload/?broadcast=1` or `/v3/block_proposal` as an authenticated caller, evidencing full auth bypass.

### Citations

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

**File:** stackslib/src/net/api/postblock_v3.rs (L104-111)
```rust
        if let Some(password) = &self.auth {
            if let Some(auth_header) = preamble.headers.get("authorization") {
                if auth_header != password {
                    return Err(Error::Http(401, "Unauthorized".into()));
                }
                authenticated = true;
            }
        }
```

**File:** stackslib/src/net/api/blockreplay.rs (L578-583)
```rust
        let Some(auth_header) = preamble.headers.get("authorization") else {
            return Err(Error::Http(401, "Unauthorized".into()));
        };
        if auth_header != password {
            return Err(Error::Http(401, "Unauthorized".into()));
        }
```

**File:** stackslib/src/net/api/fastcallreadonly.rs (L105-110)
```rust
        let Some(auth_header) = preamble.headers.get("authorization") else {
            return Err(Error::Http(401, "Unauthorized".into()));
        };
        if auth_header != password {
            return Err(Error::Http(401, "Unauthorized".into()));
        }
```

**File:** docs/rpc/openapi.yaml (L39-47)
```yaml
  securitySchemes:
    rpcAuth:
      type: apiKey
      in: header
      name: authorization
      description: |
        Plain-text secret value that must exactly equal the node's
        configured password, which is set as `connection_options.auth_token`
        in the node's configuration file.
```
