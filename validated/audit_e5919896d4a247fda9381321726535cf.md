### No vulnerability found for this question.

The target function `RPCNakamotoTenureBlocksByHashRequestHandler::new` is a trivial constructor that only initializes `burnchain_block_hash` to `None` — it takes no arguments, especially no trait object argument, and performs no state mutation whatsoever. [1](#0-0) 

The premise of the question — "a read-only RPC call mutates through a trait arg" (a `callreadonly`/Clarity-trait-dispatch style bug) — does not correspond to anything in this code. There is no trait parameter passed into `new`, no Clarity contract-call or trait dispatch involved anywhere in this file. The actual request handling logic in `try_handle_request` only reads sortition/chainstate data via `node.with_node_state` to build a `TenureReply`, with no writes to any database or state. [2](#0-1) 

Since the targeted function has no trait argument, no mutation path, and no connection to the `callreadonly`/trait-dispatch exploit idea described in the question, the claimed vulnerability does not apply to this code.

### Citations

**File:** stackslib/src/net/api/gettenureblocksbyhash.rs (L60-66)
```rust
impl RPCNakamotoTenureBlocksByHashRequestHandler {
    pub fn new() -> Self {
        Self {
            burnchain_block_hash: None,
        }
    }
}
```

**File:** stackslib/src/net/api/gettenureblocksbyhash.rs (L110-150)
```rust
    fn try_handle_request(
        &mut self,
        preamble: HttpRequestPreamble,
        _contents: HttpRequestContents,
        node: &mut StacksNodeState,
    ) -> Result<(HttpResponsePreamble, HttpResponseContents), NetError> {
        let burnchain_block_hash = self
            .burnchain_block_hash
            .take()
            .ok_or(NetError::SendError("`burnchain_block_hash` not set".into()))?;

        let reply = node.with_node_state(|network, sortdb, chainstate, _mempool, _rpc_args| {
            let snapshot = get_block_snapshot_by_burnchain_block_hash(
                sortdb,
                &burnchain_block_hash,
                &preamble,
            )?;
            let last_sortition_ch = get_prior_last_sortition_consensus_hash(
                chainstate,
                sortdb,
                &snapshot,
                &preamble,
                &network.stacks_tip.block_id(),
            )?;
            TenureReply::try_from_header_or_snapshot(
                chainstate,
                &snapshot,
                last_sortition_ch,
                &preamble,
                || {
                    NakamotoChainState::find_highest_known_block_header_in_tenure_by_block_hash(
                        chainstate,
                        sortdb,
                        &burnchain_block_hash,
                    )
                },
            )
        });

        encode_tenure_reply(&preamble, reply)
    }
```
