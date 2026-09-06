### Title
StackerDB chunk signature omits the target smart-contract identifier, allowing cross-contract replay of a signer's legitimately signed chunk - (File: libstackerdb/src/libstackerdb.rs)

### Summary
The external report's bug class is "a signature over a data structure that omits an identifier for the context (marketplace) it's bound to, letting an attacker replay a victim's signature into a different context with a different (worthless) target." The Stacks StackerDB chunk-authentication scheme has the same structural flaw: `SlotMetadata::auth_digest()` commits only to `(slot_id, slot_version, data_hash)` and never to the smart contract (StackerDB instance) the chunk is being written to. Because the Nakamoto signer StackerDBs are provisioned so that the *same signer address occupies the same slot_id across every message-type contract* for a given reward cycle, a chunk (data + signature) legitimately produced by a signer for one StackerDB contract is a fully valid, remotely-postable/relayable chunk for a *different* StackerDB contract as well.

### Finding Description
`SlotMetadata::auth_digest()` hashes only `slot_id`, `slot_version`, and `data_hash`: [1](#0-0) 
and `sign`/`verify` operate purely over that digest, with no reference to which smart contract (StackerDB instance) the chunk belongs to: [2](#0-1) 

Verification on the write path (`try_replace_chunk`) fetches the expected signer strictly by `(smart_contract, slot_id)`, then calls `slot_desc.verify(&slot_validation.signer)` — an address-only check that says nothing about which contract the signature was originally produced for: [3](#0-2) 

The same pattern repeats on the gossip/relay validation path (`validate_received_chunk`), which resolves the signer address purely from `(smart_contract_id, data.slot_id)` and then calls `slot_metadata.verify(&addr)`: [4](#0-3) 

Critically, in the Nakamoto signer architecture, slot assignment is *shared* across all message-type StackerDB contracts for a given reward cycle parity. The `.signers` boot contract stores a single slot list per parity (`stackerdb-signer-slots-0` / `stackerdb-signer-slots-1`), and every per-message-type contract (`signers-0-{message_id}`, `signers-1-{message_id}`) simply re-reads that same page: [5](#0-4) [6](#0-5) 

This is confirmed by the test that walks every `message_id` for both signer sets and asserts identical slot assignment output from `stackerdb-get-signer-slots` across all of them: [7](#0-6) 

Consequently, a given signer address `S` occupies slot_id `X` in *every* `signers-{set}-{message_id}` contract for that reward cycle. Each contract has its own independent chunk row (`stackerdb_id, slot_id` primary key) and its own independent version-clock: [8](#0-7) 

Because the signed digest never binds to `stackerdb_id`/contract, a chunk `(slot_id=X, slot_version=v, data, sig)` that signer `S` legitimately produced and broadcast for contract A (e.g., the `Transactions` message-type StackerDB) is *also* a valid, correctly-signed chunk for contract B (e.g., the `BlockResponse` message-type StackerDB), as long as B's current version at slot `X` is `< v`. Any unprivileged remote peer that observes this chunk on the network (chunks are gossiped/broadcast StackerDB content, not secret) can resubmit it — via the HTTP `POST /v2/stackerdb/{contract}/chunks` RPC (`stackslib/src/net/api/poststackerdbchunk.rs`) or via unsolicited `StackerDBPushChunk` p2p relay — into contract B. `try_replace_chunk`/`validate_received_chunk` will accept it as an authentic write by `S` to contract B, and the node will replicate/broadcast it to its peers as legitimate StackerDB state for contract B.

This exactly mirrors the ParaSpace analog: the signed structure (Credit / SlotMetadata) lacks a field binding it to the specific "marketplace"/contract it was intended for, so a signature that is valid in one context is silently valid in another.

### Impact Explanation
This breaks the equality "a chunk stored under contract B was actually written/intended by signer S for contract B" — a forged/misattributed write is injected into a different StackerDB instance and propagated to all replicas that pull chunks from that instance (via `StackerDBSync`) or receive the unsolicited push. This is unauthenticated/unauthorized-write-class and network-wide forged-data-propagation-class impact: content a signer produced for message type A (e.g. a transaction-related payload) is planted, as if authored by that signer, into message type B's StackerDB (e.g. block-response/vote channel), potentially confusing downstream consumers (miners, other signers, monitoring tooling) that trust "chunk in contract X, signed by S" as meaning "S wrote this for X's purpose." Even where deserialization of the replayed bytes as the target message type fails harmlessly, the write still corrupts/overwrites that signer's legitimate slot state in contract B (denial of a signer's own slot / stale-state injection) until the real signer overwrites it with a higher version — this is a legitimate DoS/state-corruption vector reachable by any unprivileged network participant who merely observes public StackerDB traffic.

### Likelihood Explanation
High. Chunks are not secret — they are actively broadcast/gossiped and can also be fetched by any peer via the read RPC. No signer/node compromise is needed; an attacker simply captures a previously broadcast, validly-signed chunk for contract A and resubmits it (as-is) against contract B where the same signer address holds the same slot_id. The only "difficulty" is knowing the current version count in the target contract for that slot to ensure `slot_version_replay > current_version_in_B`, which is public information retrievable via the ordinary StackerDB inventory/read RPCs.

### Recommendation
Bind the smart-contract identifier (or at minimum a stable per-contract discriminator, e.g. the StackerDB's `QualifiedContractIdentifier`) into `SlotMetadata::auth_digest()` so that a signature is only valid for the specific StackerDB instance it was produced for. This requires updating `SlotMetadata`/`StackerDBChunkData` (and their wire format/versioning) to include the contract identifier in the signed digest, plus corresponding updates to `sign`/`verify` call sites and the RPC/relay validation logic that currently resolve `(smart_contract, slot_id)` before checking only an address-bound signature.

### Proof of Concept
1. Reward cycle N is active; signer `S` (address `A`) is assigned `slot_id = 3` in every `signers-{0|1}-{message_id}` contract for this cycle (confirmed by `signers_tests.rs::signers_db_get_slots`, showing identical slot lists returned for all `message_id` values).
2. `S` legitimately signs and pushes chunk `C = (slot_id=3, slot_version=5, data=D, sig=Sig)` to `signers-0-<TRANSACTIONS_ID>` (contract A). The chunk propagates over gossip / is fetchable via the chunk-read RPC.
3. Attacker (any unprivileged network peer) fetches `C`, then determines contract B = `signers-0-<BLOCKRESPONSE_ID>` currently has slot 3 at version `< 5` (via normal inventory/read RPC).
4. Attacker POSTs `C` unmodified to contract B via `POST /v2/stackerdb/{B}/chunks` (`stackslib/src/net/api/poststackerdbchunk.rs`) or relays it as an unsolicited `StackerDBPushChunk`.
5. `try_replace_chunk`/`validate_received_chunk` resolve signer address for `(B, slot_id=3)` → `A`; `slot_desc.verify(&A)` succeeds because the signed digest never referenced contract A vs B; freshness check passes since `5 > current_version_B`. The chunk is accepted as `A`'s legitimate write to contract B and rebroadcast to B's replicas.

### Citations

**File:** libstackerdb/src/libstackerdb.rs (L159-166)
```rust
    /// Get the digest to sign that authenticates this chunk data and metadata
    fn auth_digest(&self) -> Sha512Trunc256Sum {
        let mut hasher = Sha512_256::new();
        hasher.update(self.slot_id.to_be_bytes());
        hasher.update(self.slot_version.to_be_bytes());
        hasher.update(self.data_hash.0);
        Sha512Trunc256Sum::from_hasher(hasher)
    }
```

**File:** libstackerdb/src/libstackerdb.rs (L168-193)
```rust
    /// Sign this slot metadata, committing to slot_id, slot_version, and
    /// data_hash.  Sets self.signature to the signature.
    /// Fails if the underlying crypto library fails
    pub fn sign(&mut self, privkey: &StacksPrivateKey) -> Result<(), Error> {
        let auth_digest = self.auth_digest();
        let sig = privkey
            .sign(&auth_digest.0)
            .map_err(|se| Error::SigningError(se.to_string()))?;

        self.signature = sig;
        Ok(())
    }

    /// Verify that a given principal signed this chunk metadata.
    /// Note that the address version is ignored.
    pub fn verify(&self, principal: &StacksAddress) -> Result<bool, Error> {
        let sigh = self.auth_digest();
        let pubk = StacksPublicKey::recover_to_pubkey_without_validating_low_s(
            sigh.as_bytes(),
            &self.signature,
        )
        .map_err(|ve| Error::VerifyingError(ve.to_string()))?;

        let pubkh = Hash160::from_node_public_key(&pubk);
        Ok(pubkh == *principal.bytes())
    }
```

**File:** stackslib/src/net/stackerdb/db.rs (L52-75)
```rust
    r#"
    CREATE TABLE chunks(
        -- associated stacker DB
        stackerdb_id INTEGER NOT NULL,
        -- slot ID
        slot_id INTEGER NOT NULL,
        -- lamport clock of the chunk.
        version INTEGER NOT NULL,
        -- hash of the data to be stored
        data_hash TEXT NOT NULL,
        -- secp256k1 recoverable signature from the stacker over the above columns
        signature TEXT NOT NULL,

        -- the following is NOT covered by the signature
        -- address of the creator of this chunk
        signer TEXT NOT NULL,
        -- the chunk data itself
        data BLOB NOT NULL,
        -- UNIX timestamp when the chunk was written.
        write_time INTEGER NOT NULL,
        
        PRIMARY KEY(stackerdb_id,slot_id),
        FOREIGN KEY(stackerdb_id) REFERENCES databases(stackerdb_id) ON DELETE CASCADE
    );
```

**File:** stackslib/src/net/stackerdb/db.rs (L400-423)
```rust
    pub fn try_replace_chunk(
        &self,
        smart_contract: &QualifiedContractIdentifier,
        slot_desc: &SlotMetadata,
        chunk: &[u8],
    ) -> Result<(), net_error> {
        // Check per-replica chunk-size cap.
        if (chunk.len() as u64) > self.config.chunk_size {
            return Err(net_error::StackerDBChunkTooBig(chunk.len()));
        }

        let slot_validation = self
            .get_slot_validation(smart_contract, slot_desc.slot_id)?
            .ok_or(net_error::NoSuchSlot(
                smart_contract.clone(),
                slot_desc.slot_id,
            ))?;

        if !slot_desc.verify(&slot_validation.signer)? {
            return Err(net_error::BadSlotSigner(
                slot_validation.signer,
                slot_desc.slot_id,
            ));
        }
```

**File:** stackslib/src/net/stackerdb/mod.rs (L679-697)
```rust
        // validate -- must be signed by the expected author
        let addr = match self
            .stackerdbs
            .get_slot_signer(smart_contract_id, data.slot_id)?
        {
            Some(addr) => addr,
            None => {
                return Ok(false);
            }
        };

        let slot_metadata = data.get_slot_metadata();
        if !slot_metadata.verify(&addr)? {
            info!(
                "StackerDBChunk for {} ID {} is not signed by {}",
                smart_contract_id, data.slot_id, &addr
            );
            return Ok(false);
        }
```

**File:** stackslib/src/chainstate/stacks/boot/signers.clar (L39-43)
```text
;; called by .signers-(0|1)-xxx contracts to get the signers for their respective signing sets
(define-read-only (stackerdb-get-signer-slots-page (page uint))
    (if (is-eq page u0)     (ok (var-get stackerdb-signer-slots-0))
        (if (is-eq page u1)  (ok (var-get stackerdb-signer-slots-1))
            (err ERR_NO_SUCH_PAGE))))
```

**File:** stackslib/src/chainstate/stacks/boot/signers-0-xxx.clar (L1-8)
```text
;; A StackerDB for a specific message type for signer set 0.
;; The contract name indicates which -- it has the form `signers-0-{:message_id}`.

(define-read-only (stackerdb-get-signer-slots)
    (contract-call? .signers stackerdb-get-signer-slots-page u0))

(define-read-only (stackerdb-get-config)
    (contract-call? .signers stackerdb-get-config))
```

**File:** stackslib/src/chainstate/stacks/boot/signers_tests.rs (L320-341)
```rust
    for signer_set in 0..2 {
        for message_id in 0..SIGNER_SLOTS_PER_USER {
            let contract_name =
                ContractName::try_from(format!("signers-{}-{}", &signer_set, &message_id)).unwrap();
            let signers = readonly_call(
                &mut peer,
                &latest_block_id,
                contract_name.clone(),
                ClarityName::from_literal("stackerdb-get-signer-slots"),
                vec![],
            )
            .expect_result_ok()
            .unwrap();

            debug!("Check .{}", contract_name);
            if signer_set == 0 {
                assert_eq!(signers.expect_list().unwrap(), vec![]);
            } else {
                assert_eq!(signers, expected_stackerdb_slots);
            }
        }
    }
```
