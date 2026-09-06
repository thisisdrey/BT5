### Title
Non-constant-time comparison of the node's HTTP `Authorization` secret enables timing-based token recovery - ([File: stackslib/src/net/api/postblock_v3.rs])

### Summary
Several privileged Stacks node RPC endpoints authenticate callers by comparing the `Authorization` HTTP header value against the operator-configured `auth_token` (`connection_options.auth_token`) using a plain Rust `!=` string comparison. This is not constant-time, so a remote, unauthenticated attacker can use response-timing differences to recover the secret token byte-by-byte, exactly analogous to the phpMyAdmin CSRF-token comparison flaw (CVE-2016-2041).

### Finding Description
The node's shared secret is compared with ordinary `!=`/`PartialEq` on `String`/`&str`, which is implemented as a byte-wise slice comparison that returns as soon as a mismatching byte is found (after a length check). This creates a data-dependent, early-exit comparison whose execution time leaks how many leading bytes of the guess matched the real secret.

This pattern is repeated across every endpoint that gates access with `self.auth`:
- `try_parse_request` in `RPCNakamotoBlockReplayRequestHandler`: [1](#0-0) 
- `try_parse_request` in `RPCPostBlockRequestHandler` (`/v3/blocks?broadcast=1`): [2](#0-1) 
- `try_parse_request` in `RPCTransactionSimulateRequestHandler`: [3](#0-2) 

The same `auth_token` value is shared by the block-proposal endpoint (`RPCBlockProposalRequestHandler`) and `RPCNakamotoBlockSimulateRequestHandler`, both of which are wired up with the identical secret in `register_rpc_methods`: [4](#0-3) [5](#0-4) 

The configuration/documentation itself confirms the intended semantics — the client-supplied header value "must exactly equal" the server's stored password: [6](#0-5) 

and that this single token secures block-proposal submission and authenticated block broadcast: [7](#0-6) 

Because the same token gates multiple network-reachable, unprivileged endpoints (`/v3/blocks?broadcast=1`, `/v3/block_proposal`, `/v3/blocks/replay/...`, `/v3/transactions/simulate`, `/v3/blocks/simulate`), an attacker can pick whichever endpoint yields the cleanest timing signal to run the byte-recovery attack, then reuse the recovered token against the higher-impact endpoints.

### Impact Explanation
Once the shared `auth_token` is recovered via timing analysis, the attacker gains **authentication bypass** for endpoints that are otherwise access-controlled: they can broadcast attacker-chosen blocks through `/v3/blocks?broadcast=1` (authenticated broadcast path in `postblock_v3.rs`) and submit block proposals through `/v3/block_proposal`, i.e., an unauthenticated/unauthorized write into node-controlled protocol surfaces that were meant to be restricted to the paired `stacks-signer`/miner. This matches the "auth bypass" / "unauthenticated write" Critical-impact category, since the whole point of `auth_token` is to prevent exactly these writes from untrusted remote parties.

### Likelihood Explanation
The comparison happens on every request to these endpoints and requires no prior authentication or privileged position — any remote, unauthenticated party can send repeated guesses and time the responses. Statistical timing attacks over a network are noisier than local ones but are a well-established, practical attack class (this is precisely the bug class the referenced CVE-2016-2041 patch addressed by moving to constant-time comparison). The number of requests needed scales with token length and alphabet size, not with volume/flooding, so this is not merely a DDoS/traffic-volume issue.

### Recommendation
Replace all `auth_header != password` (and equivalent `!=`) comparisons of the configured `auth_token` with a constant-time comparison, e.g. using `subtle::ConstantTimeEq` or an HMAC-based comparison, in:
- `stackslib/src/net/api/blockreplay.rs`
- `stackslib/src/net/api/postblock_v3.rs`
- `stackslib/src/net/api/txsimulate.rs`
- `stackslib/src/net/api/blocksimulate.rs`
- `stackslib/src/net/api/postblock_proposal.rs`

Ideally centralize the check into a single shared helper (e.g., in `httpcore.rs`) used by all handlers, so future endpoints automatically inherit constant-time comparison instead of re-implementing `!=` checks individually.

### Proof of Concept
1. Configure a node with `connection_options.auth_token = "<secret>"` as shown in `sample/conf/mainnet-miner-conf.toml`.
2. As a remote, unauthenticated client, repeatedly send POST requests to `/v3/blocks/replay/<block_id>` (or `/v3/transactions/simulate`) with varying `Authorization` header guesses, each differing by one trailing byte from a previously-confirmed correct prefix.
3. Measure response latency for the 401 rejection path in `try_parse_request` (`auth_header != password` branch) — guesses whose prefix matches more of the real token take measurably longer due to the extra bytes compared before divergence.
4. Iterate byte-by-byte (as in classic HTTP timing-attack tooling) to reconstruct the full `auth_token`, then replay it against `/v3/blocks?broadcast=1` or `/v3/block_proposal` to perform an unauthorized action.

### Citations

**File:** stackslib/src/net/api/blockreplay.rs (L574-583)
```rust
        // If no authorization is set, then the block replay endpoint is not enabled
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

**File:** stackslib/src/net/api/postblock_v3.rs (L99-111)
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
```

**File:** stackslib/src/net/api/txsimulate.rs (L351-360)
```rust
        // If no authorization is set, then the transaction simulation endpoint is not enabled
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

**File:** stackslib/src/net/api/mod.rs (L82-102)
```rust
        self.register_rpc_endpoint(blockreplay::RPCNakamotoBlockReplayRequestHandler::new(
            self.auth_token.clone(),
        ));
        self.register_rpc_endpoint(blocksimulate::RPCNakamotoBlockSimulateRequestHandler::new(
            self.auth_token.clone(),
        ));
        self.register_rpc_endpoint(txsimulate::RPCTransactionSimulateRequestHandler::new(
            self.auth_token.clone(),
        ));
        self.register_rpc_endpoint(callreadonly::RPCCallReadOnlyRequestHandler::new(
            self.maximum_call_argument_size,
            self.read_only_call_limit.clone(),
            self.read_only_max_execution_time,
            self.read_only_call_max_mem_bytes,
        ));
        self.register_rpc_endpoint(fastcallreadonly::RPCFastCallReadOnlyRequestHandler::new(
            self.maximum_call_argument_size,
            self.read_only_max_execution_time,
            self.read_only_call_max_mem_bytes,
            self.auth_token.clone(),
        ));
```

**File:** stackslib/src/net/api/mod.rs (L164-170)
```rust
        self.register_rpc_endpoint(postblock::RPCPostBlockRequestHandler::new());
        self.register_rpc_endpoint(postblock_proposal::RPCBlockProposalRequestHandler::new(
            self.auth_token.clone(),
        ));
        self.register_rpc_endpoint(postblock_v3::RPCPostBlockRequestHandler::new(
            self.auth_token.clone(),
        ));
```

**File:** docs/rpc/openapi.yaml (L40-47)
```yaml
    rpcAuth:
      type: apiKey
      in: header
      name: authorization
      description: |
        Plain-text secret value that must exactly equal the node's
        configured password, which is set as `connection_options.auth_token`
        in the node's configuration file.
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
