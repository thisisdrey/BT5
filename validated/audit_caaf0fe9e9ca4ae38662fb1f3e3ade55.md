### Title
`MockProposal::miner_signature_hash` panics on non-ASCII (but valid UTF-8) `server_version` field - ([File: libsigner/src/v0/messages.rs])

### Summary
`PeerInfo::consensus_deserialize` only validates that the `server_version` field is valid UTF-8 [1](#0-0) , but `MockProposal::miner_signature_hash` later passes that same string into `Value::string_ascii_from_bytes(...).unwrap()`, which requires strict ASCII and returns `Err` on multi-byte UTF-8 characters [2](#0-1) . A `server_version` string containing any non-ASCII (but UTF-8-valid) character causes an unwrap panic whenever `miner_signature_hash`/`verify` is invoked on the deserialized struct.

### Finding Description
`PeerInfo::consensus_deserialize` reads a length-prefixed byte string and only checks that it parses as valid UTF-8: [1](#0-0) 

This is weaker than what is later assumed. `MockProposal::miner_signature_hash` builds a Clarity tuple and calls:
```rust
Value::string_ascii_from_bytes(self.peer_info.server_version.clone().into()).unwrap(),
``` [2](#0-1) 

`Value::string_ascii_from_bytes` validates that every byte is `< 128` (i.e., ASCII), and returns a `CheckErrors`/`Result::Err` for any string containing a multi-byte UTF-8 codepoint (e.g. `"é"`, byte sequence `0xC3 0xA9`, which is valid UTF-8 but not ASCII). The broken equality is: *"valid UTF-8" ≠ "valid ASCII"*, yet the deserialization guard only checks the former while the hash routine assumes the latter and `.unwrap()`s the result.

`MockProposal` (and the `MockSignature`/`MockBlock` structures that wrap it) are gossiped over the miners'/signers' StackerDB slots as part of the epoch-2.5 mock-signing protocol; a node that owns its StackerDB slot (miner or signer) legitimately writes these chunks, which get fetched and deserialized by peers, who then call `.verify()` (which internally calls `miner_signature_hash()`) to check the embedded signature. Setting `peer_info.server_version` to a non-ASCII-but-UTF8 string in a locally crafted `MockProposal`/`MockBlock` chunk is fully within the byte-level control of a StackerDB slot owner, satisfying the "attacker owns a StackerDB slot" precondition in scope.

### Impact Explanation
Any node (miner or signer) that receives and processes a crafted `MockProposal`/`MockSignature`/`MockBlock` StackerDB chunk with a non-ASCII UTF-8 `server_version` will panic inside `miner_signature_hash`, crashing the process. This is a remote, unauthenticated (from the perspective of other nodes receiving/relaying the chunk) DoS reachable by a single StackerDB chunk write from an attacker who merely owns their own slot — matching the "Critical: remote crash / unauthenticated DoS from few messages" category.

### Likelihood Explanation
Preconditions: the network must be in the epoch 2.5 mock-signing window (the code path is only exercised then), and the attacker needs to own a StackerDB slot in the `miners` or `signers` contract they are already entitled to write to as a participating miner/signer — this matches the allowed "own StackerDB slot" attacker capability. Constructing the malicious `server_version` byte string requires no cryptographic secret, only picking any multi-byte UTF-8 character, so cost is negligible and the attack is trivially repeatable against every peer that processes the chunk.

### Recommendation
Validate `server_version` for ASCII-only content at deserialization time in `PeerInfo::consensus_deserialize` (or use `str::is_ascii()` check and reject non-ASCII with a `CodecError::DeserializeError`), and/or replace the `.unwrap()` in `MockProposal::miner_signature_hash` with proper error propagation instead of panicking on invalid input.

### Proof of Concept
```rust
// In libsigner/src/v0/messages.rs test module
#[test]
fn mock_proposal_non_ascii_server_version_panics() {
    let peer_info = PeerInfo {
        burn_block_height: 1,
        stacks_tip_consensus_hash: ConsensusHash([0; 20]),
        stacks_tip: BlockHeaderHash([0; 32]),
        stacks_tip_height: 1,
        pox_consensus: ConsensusHash([1; 20]),
        server_version: "é".to_string(), // valid UTF-8, not ASCII
        network_id: 1,
    };

    // Round-trip through the wire codec, as a remote peer's bytes would be parsed
    let mut buf = vec![];
    peer_info.consensus_serialize(&mut buf).unwrap(); // succeeds: UTF-8 check only
    let decoded = PeerInfo::consensus_deserialize(&mut &buf[..]).unwrap(); // succeeds
    assert_eq!(decoded.server_version, "é");

    let proposal = MockProposal {
        peer_info: decoded,
        // signature field is private; construct via a helper or reflect internal API used by tests
        ..Default::default() // adjust per actual struct visibility
    };

    // This call panics inside Value::string_ascii_from_bytes(...).unwrap()
    let _ = proposal.miner_signature_hash();
}
```
Expected result: the process panics at `libsigner/src/v0/messages.rs:399` (`Value::string_ascii_from_bytes(...).unwrap()`) instead of returning an error, confirming the reachable panic from attacker-controlled `server_version` bytes.

### Citations

**File:** libsigner/src/v0/messages.rs (L333-341)
```rust
        let len_byte: u8 = read_next(fd)?;
        let mut bytes = vec![0u8; len_byte as usize];
        fd.read_exact(&mut bytes).map_err(CodecError::ReadError)?;
        // must encode a valid string
        let server_version = String::from_utf8(bytes).map_err(|_e| {
            CodecError::DeserializeError(
                "Failed to parse server version name: could not construct from utf8".to_string(),
            )
        })?;
```

**File:** libsigner/src/v0/messages.rs (L396-400)
```rust
                (
                    ClarityName::from_literal("server-version"),
                    Value::string_ascii_from_bytes(self.peer_info.server_version.clone().into())
                        .unwrap(),
                ),
```
