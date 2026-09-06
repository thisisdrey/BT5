### Title
Non-constant-time comparison of the RPC `authorization` token enables remote timing-based secret recovery and auth bypass - (File: `stackslib/src/net/api/postblock_proposal.rs`, `stackslib/src/net/api/postblock_v3.rs`, `stackslib/src/net/api/fastcallreadonly.rs`, `stackslib/src/net/api/blocksimulate.rs`, `stackslib/src/net/api/txsimulate.rs`, `stackslib/src/net/api/blockreplay.rs`)

### Summary
Six privileged `stacks-node` RPC endpoints protect themselves with a single shared secret (`connection_options.auth_token`) compared to the client-supplied `authorization` header using plain Rust string inequality (`auth_header != password`) rather than a constant-time comparison. This is the same class of bug as the reported CVE (a static/predictable secret guarding privileged access) but manifests here as a comparison that leaks timing information about how many leading bytes of the token match, allowing a remote, unauthenticated attacker to incrementally recover the exact secret and then use it to bypass authentication on state-mutating endpoints.

### Finding Description
Each of the following handlers implements the identical pattern in `try_parse_request`:
```rust
let Some(password) = &self.auth else { return Err(Error::Http(400, "Bad Request.".into())); };
let Some(auth_header) = preamble.headers.get("authorization") else { return Err(Error::Http(401, "Unauthorized".into())); };
if auth_header != password {
    return Err(Error::Http(401, "Unauthorized".into()));
}
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) 

Rust's `String`/`&str` `PartialEq` implementation (via `str::eq`, ultimately `memcmp`/byte-wise comparison) short-circuits and returns as soon as the first mismatching byte is found. This means the time taken by the comparison is a function of the number of correctly-guessed leading bytes of the attacker-supplied header versus the real, node-configured `auth_token`. This breaks the intended equality check: instead of "authenticated vs. not," the response timing encodes a byte-by-byte oracle of "how much of my guess matches the stored secret," fitting the report's category of "authenticated vs. stored" equality failing to be checked in a way immune to side channels.

This differs from the AdaptiveScale advisory's literal hardcoded key, but is functionally the same class of flaw: a fixed shared secret that is meant to be the sole gate to privileged behavior can be extracted/derived by a remote, unauthenticated party without needing any legitimate credential, because the equality check itself is not constant-time.

The `auth_token` gates non-trivial capabilities:
- `/v3/block_proposal` — submitting Nakamoto block proposals for validation [6](#0-5) 
- `/v3/blocks/upload/?broadcast=1` — authenticated broadcast of blocks [7](#0-6) 
- `/v3/contracts/fast-call-read/...` — read-only contract calls bypassing normal restrictions [8](#0-7) 
- `/v3/blocks/simulate/...` and `/v3/transactions/simulate` — block/tx replay & simulation [9](#0-8) , [10](#0-9) 
- Block replay endpoint [11](#0-10) 

The same `auth_token` value is documented as securing coordination between the node and `stacks-signer`, so recovering it also lets an attacker impersonate a signer toward the node's block-proposal channel. [12](#0-11) 

### Impact Explanation
An attacker who statistically recovers the `auth_token` gains unauthenticated write access to the block-proposal validation pipeline and other privileged endpoints, matching the "unauthenticated/unauthorized write to state" and "auth bypass" impact tiers. This requires no possession of any node secret or private key beyond what byte-wise timing analysis over the network can reveal — the vulnerability is that this "secret" is protected by a comparison whose timing profile is observable remotely.

### Likelihood Explanation
Exploitation requires performing many timing measurements per byte position over the network to statistically distinguish `memcmp`-level timing differences, which is noisy but a documented, practical attack class (well-known "timing attack on string compare," e.g., historically demonstrated against session-token comparisons in web frameworks). It is easier the fewer network hops/jitter exist (e.g., LAN, or signer-to-node deployments where the token length is bounded and requests can be repeated many times), and does not require any credentials, making it remotely triggerable by any network peer that can reach the RPC port.

### Recommendation
Replace `auth_header != password` with a constant-time comparison (e.g., `subtle::ConstantTimeEq`, or first compare fixed-length HMACs/hashes of both operands) in all six locations: `postblock_proposal.rs`, `postblock_v3.rs`, `fastcallreadonly.rs`, `blocksimulate.rs`, `txsimulate.rs`, `blockreplay.rs`. Consider centralizing the auth check into a single shared helper so all endpoints use the same hardened comparison and future endpoints inherit it automatically.

### Proof of Concept
1. Configure `connection_options.auth_token = "<secret>"` on a target `stacks-node` and expose `/v3/block_proposal`.
2. Send repeated POST requests to `/v3/block_proposal` with `authorization` headers that vary the guessed byte at increasing prefix positions (e.g., `"a", "b", ..., "aa", "ab", ...`), measuring server response latency for each guess.
3. Because `auth_header != password` short-circuits at the first mismatching byte, correct-prefix guesses take measurably longer (due to comparing more bytes) than incorrect ones at the same position, on average, over many trials.
4. Iterate byte-by-byte to reconstruct the full `auth_token`, then use it to submit unauthorized block proposals via the now-bypassed 401 check. [1](#0-0)

### Citations

**File:** stackslib/src/net/api/postblock_proposal.rs (L1091-1103)
```rust
#[derive(Clone, Default)]
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

**File:** stackslib/src/net/api/fastcallreadonly.rs (L44-72)
```rust
#[derive(Clone)]
pub struct RPCFastCallReadOnlyRequestHandler {
    pub call_read_only_handler: RPCCallReadOnlyRequestHandler,
    pub auth: Option<String>,
}

impl RPCFastCallReadOnlyRequestHandler {
    pub fn new(
        maximum_call_argument_size: u32,
        read_only_max_execution_time: Duration,
        read_only_call_max_mem_bytes: u64,
        auth: Option<String>,
    ) -> Self {
        Self {
            call_read_only_handler: RPCCallReadOnlyRequestHandler::new(
                maximum_call_argument_size,
                ExecutionCost {
                    write_length: 0,
                    write_count: 0,
                    read_length: 0,
                    read_count: 0,
                    runtime: 0,
                },
                read_only_max_execution_time,
                read_only_call_max_mem_bytes,
            ),
            auth,
        }
    }
```

**File:** stackslib/src/net/api/fastcallreadonly.rs (L102-110)
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

**File:** stackslib/src/net/api/blocksimulate.rs (L129-161)
```rust
/// Decode the HTTP request
impl HttpRequest for RPCNakamotoBlockSimulateRequestHandler {
    fn verb(&self) -> &'static str {
        "POST"
    }

    fn path_regex(&self) -> Regex {
        Regex::new(r#"^/v3/blocks/simulate/(?P<block_id>[0-9a-f]{64})$"#).unwrap()
    }

    fn metrics_identifier(&self) -> &str {
        "/v3/blocks/simulate/:block_id"
    }

    /// Try to decode this request.
    /// There's nothing to load here, so just make sure the request is well-formed.
    fn try_parse_request(
        &mut self,
        preamble: &HttpRequestPreamble,
        captures: &Captures,
        query: Option<&str>,
        body: &[u8],
    ) -> Result<HttpRequestContents, Error> {
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

**File:** stackslib/src/net/api/txsimulate.rs (L328-360)
```rust
/// Decode the HTTP request
impl HttpRequest for RPCTransactionSimulateRequestHandler {
    fn verb(&self) -> &'static str {
        "POST"
    }

    fn path_regex(&self) -> Regex {
        Regex::new(r#"^/v3/transactions/simulate$"#).unwrap()
    }

    fn metrics_identifier(&self) -> &str {
        "/v3/transactions/simulate"
    }

    /// Try to decode this request.
    /// There's nothing to load here, so just make sure the request is well-formed.
    fn try_parse_request(
        &mut self,
        preamble: &HttpRequestPreamble,
        _captures: &Captures,
        query: Option<&str>,
        body: &[u8],
    ) -> Result<HttpRequestContents, Error> {
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

**File:** stackslib/src/net/api/blockreplay.rs (L1-45)
```rust
// Copyright (C) 2025-2026 Stacks Open Internet Foundation
//
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published by
// the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU General Public License for more details.
//
// You should have received a copy of the GNU General Public License
// along with this program.  If not, see <http://www.gnu.org/licenses/>.

use std::time::Instant;

use clarity::vm::costs::ExecutionCost;
use clarity::vm::Value;
use regex::{Captures, Regex};
use stacks_common::codec::StacksMessageCodec;
use stacks_common::types::chainstate::{BlockHeaderHash, ConsensusHash, StacksBlockId, TrieHash};
use stacks_common::types::net::PeerHost;
use stacks_common::util::hash::Sha512Trunc256Sum;
use stacks_common::util::secp256k1::MessageSignature;
use stacks_common::util::serde_serializers::prefix_hex_codec;
use url::form_urlencoded;

use crate::burnchains::Txid;
use crate::chainstate::burn::db::sortdb::SortitionDB;
use crate::chainstate::nakamoto::miner::{MinerTenureInfoCause, NakamotoBlockBuilder};
use crate::chainstate::nakamoto::{NakamotoBlock, NakamotoChainState};
use crate::chainstate::stacks::db::{ClarityTx, StacksChainState};
use crate::chainstate::stacks::events::{StacksTransactionReceipt, TransactionOrigin};
use crate::chainstate::stacks::miner::{
    BlockBuilder, BlockLimitFunction, TransactionResourceBudgets, TransactionResult,
};
use crate::chainstate::stacks::{Error as ChainError, StacksTransaction, TransactionPayload};
use crate::config::DEFAULT_MAX_TENURE_BYTES;
use crate::net::http::{
    parse_json, Error, HttpNotFound, HttpRequest, HttpRequestContents, HttpRequestPreamble,
    HttpResponse, HttpResponseContents, HttpResponsePayload, HttpResponsePreamble, HttpServerError,
};
use crate::net::httpcore::{RPCRequestHandler, StacksHttpResponse};
use crate::net::{Error as NetError, StacksHttpRequest, StacksNodeState};
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
