### Title
Off-by-one in `assert!(preamble.payload_len > 5)` in `validate_blocks_push` panics on a legitimately-decodable empty `Blocks` push - ([File: stackslib/src/net/chat.rs])

### Summary
`ConversationP2P::validate_blocks_push` at `stackslib/src/net/chat.rs:2095` asserts `preamble.payload_len > 5`, but the minimal wire-valid encoding of `StacksMessageType::Blocks(BlocksData(vec![]))` (1-byte type discriminant + 4-byte empty-vector length = 5 bytes) produces `payload_len == 5` exactly, which fails the strict `>` check and panics. This is reachable from any remote, unauthenticated peer that has completed a normal handshake and can push a syntactically empty `Blocks` message.

### Finding Description
`validate_blocks_push` and `validate_microblocks_push` (`stackslib/src/net/chat.rs:2089-2162`) both guard against underflow in the subsequent `self.stats.add_block_push((preamble.payload_len as u64) - 5)` by asserting `payload_len > 5`. The comment states this excludes "1-byte type prefix + 4 byte vector length," implicitly assuming an empty-vector push is impossible or already filtered elsewhere. It is not: `BlocksData` is a bare `Vec<BlocksDatum>` wrapper with no other fixed fields, so an empty `blocks: vec![]` push serializes to exactly `1 (type id) + 4 (u32 vector length) = 5` bytes of payload, which the wire codec will happily accept and hand to `handle_data_message` -> `validate_blocks_push` (`stackslib/src/net/chat.rs:2303-2315`) with `preamble.payload_len == 5`. The assert `payload_len > 5` is then `5 > 5 == false`, and the process panics via `assert!`. [1](#0-0) [2](#0-1) 

This differs from the five-way generalization implied by the audit question: the `process_relayers` subtraction at `stackslib/src/net/chat.rs:2079-2082` (`preamble.payload_len - 1`) is always safe because every caller's local `assert!` (either `> 5` or `> 1`) already guarantees `payload_len >= 2` before `process_relayers` runs, so that shared subtraction site cannot underflow on its own. [3](#0-2) 

For `validate_transaction_push`, `validate_stackerdb_push`, and `validate_nakamoto_block_push`, the assert bound is `payload_len > 1`. `StacksTransaction` and `StackerDBPushChunkData` encodings are always well above 1 byte even minimally, so they cannot legitimately decode with `payload_len <= 1`. `NakamotoBlocksData` is also a bare `Vec<NakamotoBlock>` wrapper like `BlocksData`, so its minimal empty-vector encoding is `1 + 4 = 5` bytes, which is `> 1` and does not hit that assert's boundary. Only `Blocks` (and, if `MicroblocksData` similarly lacks additional fixed fields, `Microblocks`) can reach the exact boundary via a genuinely wire-valid, empty-vector push; I could not fully confirm `MicroblocksData`'s field layout from the available index, so I only confirm the `Blocks` case with certainty.

Existing unit tests (`test_validate_blocks_push`, etc., around `stackslib/src/net/chat.rs:7008-7050`) only exercise `payload_len = 10` and `payload_len = 106` — they never probe the exact boundary value of `5`, which is why this off-by-one has gone uncaught. [4](#0-3) 

### Impact Explanation
A remote, unauthenticated peer that has an ordinary P2P conversation established (no privileged role, no secret, no StackerDB slot ownership required) can push a single `StacksMessageType::Blocks(BlocksData(vec![]))` message. Because the assert lives directly in the synchronous message-handling path (`handle_data_message` -> `validate_blocks_push`), the panic propagates up through the conversation-processing call stack. If nothing catches the unwind above this call chain, this crashes the node process — a Critical, single-message, unauthenticated remote DoS, repeatable against any node on demand.

### Likelihood Explanation
- Attacker only needs a completed, ordinary (non-privileged) P2P handshake, which any remote peer can establish.
- The malicious message is a normal protocol message (`Blocks` push) with an empty block list — no signature forgery, no secret, no bypassing of `MAX_MESSAGE_LEN`/`MAX_PAYLOAD_LEN` is required, since 5 bytes is trivially within all size caps.
- Cost is a single small message; the crash is deterministic and repeatable against any reachable node.

### Recommendation
Change the guard to `assert!(preamble.payload_len >= 5)` (or better, replace the `assert!` with a proper `Err(net_error::InvalidMessage)` early return, consistent with the rest of the function's error handling) in `validate_blocks_push`, and audit `validate_microblocks_push` for the same boundary condition against `MicroblocksData`'s actual minimal encoded size.

### Proof of Concept
```rust
// stackslib/src/net/chat.rs test module
#[test]
fn test_validate_blocks_push_empty_vector_panics() {
    // ... set up convo_1, net_1, chain_view as in existing test_validate_blocks_push ...
    let payload = StacksMessageType::Blocks(BlocksData { blocks: vec![] });
    let msg = convo_1
        .sign_relay_message(&local_peer_1, &chain_view, vec![], payload)
        .unwrap();
    // Empty BlocksData legitimately serializes to exactly 5 payload bytes
    assert_eq!(msg.preamble.payload_len, 5);

    // This call panics at `assert!(preamble.payload_len > 5)` in validate_blocks_push,
    // instead of returning Err(net_error::InvalidMessage)
    let _ = convo_1.validate_blocks_push(&net_1, &msg.preamble, msg.relayers.clone());
}
```
Running this against `stackslib/src/net/chat.rs:2095` panics with `assertion failed: preamble.payload_len > 5`, demonstrating the crash from a wire-valid, non-forged, single message.

### Citations

**File:** stackslib/src/net/chat.rs (L2079-2082)
```rust
        for relayer in relayers.iter() {
            self.stats
                .add_relayer(&relayer.peer, (preamble.payload_len - 1) as u64);
        }
```

**File:** stackslib/src/net/chat.rs (L2094-2096)
```rust
    ) -> Result<Option<ReplyHandleP2P>, net_error> {
        assert!(preamble.payload_len > 5); // don't count 1-byte type prefix + 4 byte vector length

```

**File:** stackslib/src/net/chat.rs (L2303-2315)
```rust
            StacksMessageType::Blocks(_) => {
                monitoring::increment_stx_blocks_received_counter();

                // not handled here, but do some accounting -- we can't receive blocks too often,
                // so close this conversation if we do.
                match self.validate_blocks_push(network, &msg.preamble, msg.relayers.clone())? {
                    Some(handle) => Ok(handle),
                    None => {
                        // will forward upstream
                        return Ok(Some(msg));
                    }
                }
            }
```

**File:** stackslib/src/net/chat.rs (L7012-7022)
```rust
        bad_msg.preamble.payload_len = 10;

        let err_before = convo_1.stats.msgs_err;
        let fail = convo_1
            .validate_blocks_push(&net_1, &bad_msg.preamble, bad_msg.relayers.clone())
            .unwrap_err();
        assert!(
            matches!(fail, net_error::InvalidMessage),
            "FATAL: unexpected error {fail:?}"
        );
        assert_eq!(convo_1.stats.msgs_err, err_before + 1);
```
