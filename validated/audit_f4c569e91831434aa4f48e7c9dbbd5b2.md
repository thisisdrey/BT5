### Title
Timing side-channel in RPC authorization-token comparison enables remote auth-token recovery - (File: stackslib/src/net/api/postblock_proposal.rs, blocksimulate.rs, blockreplay.rs, fastcallreadonly.rs, txsimulate.rs, postblock_v3.rs)

### Summary
Every privileged RPC endpoint that gates access behind `connection_options.auth_token` compares the client-supplied `authorization` header to the configured password using Rust's built-in, non-constant-time `!=` string comparison. Because this comparison short-circuits on the first mismatching byte (and on length), a remote, unauthenticated attacker can use response-timing measurements to recover the secret `auth_token` byte-by-byte, ultimately achieving full authentication bypass against endpoints that would otherwise require the secret.

### Finding Description
The pattern `if auth_header != password { return Err(...401...) }` is repeated verbatim across all HTTP handlers that implement `auth_token`-gated RPC: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) 

`String`/`&str` equality in Rust is implemented as a byte-wise comparison that first checks length and then compares bytes left-to-right, returning `false` as soon as a mismatch is found. This is a textbook non-constant-time comparison: the number of matching leading bytes between the guess and the real secret directly influences the number of comparison operations performed (and, at network scale, measurably influences response latency), unlike a constant-time comparison (e.g., `subtle::ConstantTimeEq` or a HMAC-based check) which always takes the same time regardless of where the mismatch occurs.

This breaks the intended security equality: "authenticated" should be indistinguishable, from the attacker's perspective, from "not authenticated" for any wrong guess. Instead, "almost correct" guesses are observably distinguishable from "very wrong" guesses via timing, letting an attacker perform a classic byte-at-a-time timing attack to reconstruct the `auth_token` without ever needing to know it.

Once the token is recovered, an attacker gains legitimate access to every endpoint gated the same way, including:
- `/v3/block_proposal` (`postblock_proposal.rs`) — normally reserved for the node's paired stacks-signer to request block validation,
- `/v2/blocks?broadcast=1` via `postblock_v3.rs` — used to force block broadcast,
- `/v3/blocks/simulate`, `/v3/transactions/simulate`, `/v3/contracts/fast-call-read/...`, `/v3/blocks/replay/...`.

The configuration documentation itself states the auth token "secures the communication channel between this node and a connected stacks-signer instance" and gates block-broadcast requests, confirming this is a trust boundary, not merely a convenience flag: [7](#0-6) .

### Impact Explanation
This maps to the in-scope "auth-gate that fails open" / auth-bypass category. Practically, an attacker who is not supposed to hold the `auth_token` can, given sufficient time and network samples, recover it via timing analysis and then submit requests as an authenticated party (e.g., trigger `/v3/block_proposal` validation load, or set `broadcast=1` on `/v2/blocks` to force propagation) — i.e., unauthorized write/trigger access to functionality that is documented to require the shared secret. This lines up with the reported bug class (Jenkins CasC) in spirit: a supposedly gated resource is reachable by someone without the correct credential due to an implementation flaw in the check itself, not a flaw in what data is protected.

That said, exploitability in practice depends on the attacker being able to reliably measure small timing differences over the network (jitter, TLS/TCP overhead, and Rust's fast native byte comparisons make the per-byte timing delta extremely small), which significantly raises the difficulty compared to a local/library timing side channel.

### Likelihood Explanation
Low-to-Medium. The auth gate is only active when the node operator has actually configured `auth_token` (it's `None`/disabled by default), and the attacker needs network path visibility with low-enough jitter to observation-average out noise across many requests to recover even one byte, and the secret can be arbitrarily long/random. This is a real code-quality/security defect but a high-effort, statistical attack rather than a one-shot bypass.

### Recommendation
Replace all `auth_header != password` byte-comparisons with a constant-time comparison, e.g. using the `subtle` crate's `ConstantTimeEq` (or compare fixed-length HMACs/digests of the header and configured secret instead of comparing the raw strings), applied uniformly across `postblock_proposal.rs`, `blocksimulate.rs`, `blockreplay.rs`, `fastcallreadonly.rs`, `txsimulate.rs`, and `postblock_v3.rs`.

### Proof of Concept
Not independently reproducible as a network PoC within this review (requires precise timing measurement infrastructure), but the code path is directly demonstrated by the existing unit tests that exercise the exact `!=` comparison, e.g. `test_wrong_auth` in `stackslib/src/net/api/tests/fastcallreadonly.rs:414-443`, which shows the header is compared byte-for-byte against the configured password and confirms a 401 is returned on mismatch — the same code path used for the timing-observable comparison. [8](#0-7)

### Citations

**File:** stackslib/src/net/api/postblock_proposal.rs (L1139-1144)
```rust
        let Some(auth_header) = preamble.headers.get("authorization") else {
            return Err(Error::Http(401, "Unauthorized".into()));
        };
        if auth_header != password {
            return Err(Error::Http(401, "Unauthorized".into()));
        }
```

**File:** stackslib/src/net/api/blocksimulate.rs (L156-161)
```rust
        let Some(auth_header) = preamble.headers.get("authorization") else {
            return Err(Error::Http(401, "Unauthorized".into()));
        };
        if auth_header != password {
            return Err(Error::Http(401, "Unauthorized".into()));
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

**File:** stackslib/src/net/api/txsimulate.rs (L355-360)
```rust
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

**File:** stackslib/src/config/mod.rs (L3802-3816)
```rust
    /// HTTP auth password to use when communicating with stacks-signer binary.
    ///
    /// This token is used in the `Authorization` header for certain requests.
    /// Primarily, it secures the communication channel between this node and a
    /// connected `stacks-signer` instance.
    ///
    /// It is also used to authenticate requests to `/v2/blocks?broadcast=1`.
    /// ---
    /// @default: `None` (authentication disabled for relevant endpoints)
    /// @notes:
    ///   - This field **must** be configured if the node needs to receive
    ///     block proposals from a configured `stacks-signer` [[events_observer]]
    ///     via the `/v3/block_proposal` endpoint.
    ///   - The value must match the token configured on the signer.
    pub auth_token: Option<String>,
```

**File:** stackslib/src/net/api/tests/fastcallreadonly.rs (L414-443)
```rust
#[test]
fn test_wrong_auth() {
    let addr = SocketAddr::new(IpAddr::V4(Ipv4Addr::new(127, 0, 0, 1)), 33333);

    let mut requests = vec![];

    // query confirmed tip
    let mut request = StacksHttpRequest::new_fastcallreadonlyfunction(
        addr.into(),
        StacksAddress::from_string("ST2DS4MSWSGJ3W9FBC6BVT0Y92S345HY8N3T6AV7R").unwrap(),
        "hello-world".try_into().unwrap(),
        StacksAddress::from_string("ST2DS4MSWSGJ3W9FBC6BVT0Y92S345HY8N3T6AV7R")
            .unwrap()
            .to_account_principal(),
        None,
        "ro-confirmed".try_into().unwrap(),
        vec![],
        TipRequest::UseLatestAnchoredTip,
    );
    request.add_header("authorization".into(), "wrong".into());

    requests.push(request);

    let mut responses = test_rpc(function_name!(), requests);

    let response = responses.remove(0);
    let (preamble, contents) = response.destruct();

    assert_eq!(preamble.status_code, 401);
}
```
