## Title
StackerDB Chunk Signatures Lack a Domain/Namespace Separator, Enabling Cross-Contract Replay of Valid Chunks - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest()` computes the signed digest for a StackerDB chunk from only `slot_id`, `slot_version`, and `data_hash` — it never includes the StackerDB's `contract_id` (its namespace/domain). Because slot ownership (which address owns which `slot_id`) is frequently identical across different StackerDB contracts (e.g. consecutive `.signers-0-N` / `.signers-1-N` reward-cycle contracts, whose slot assignment is a deterministic sort of the same signer set), a chunk signature that was valid and broadcast for one StackerDB contract can be replayed verbatim into a different StackerDB contract where the same address happens to own the same slot, and it will be accepted as an authentic write. This is the same "missing domain separator" bug class flagged in the external report (raw hash without a domain-bound EIP-712-style structure enabling cross-network replay), applied here to the Stacks P2P StackerDB chunk-signing scheme.

### Finding Description
The digest that a StackerDB writer signs is: [1](#0-0) 

`sign`/`verify` operate purely on this digest with no reference to which contract (StackerDB namespace) the chunk belongs to: [2](#0-1) 

When a node receives a chunk (either pulled via sync or pushed unsolicited), it validates the chunk by looking up the expected signer address *for that specific `smart_contract_id`* and then calls `slot_metadata.verify(&addr)`, which — per the digest above — is blind to `smart_contract_id`: [3](#0-2) 

`get_slot_signer` resolves an address purely from `(smart_contract_id, slot_id)`: [4](#0-3) 

The missing binding matters because slot assignment for a given signer address is deterministic and frequently identical across separate StackerDB contracts. `.signers-(0|1)-N` contracts hold the signer/slot list per reward cycle, and the ordering used to assign `slot_id` is a sort by `signing_key`: [5](#0-4) [6](#0-5) 

and the node-side listener assigns `slot_id` by enumerating the (sorted) reward-set signers: [7](#0-6) 

Whenever the signer set (or a subset sharing the same relative sort order) persists across cycles — a very common real-world condition since re-elected signers keep the same keys — the same address ends up owning the same `slot_id` in multiple, distinct `.signers-X-Y` StackerDB contracts. A chunk `(slot_id, slot_version, data_hash, sig)` that was validly signed and gossiped for reward-cycle contract `A` therefore carries a signature that is *also* valid for reward-cycle contract `B`, because the signed digest never encoded `A` vs `B`. An attacker who observes a broadcast, previously-valid chunk (chunk contents and signatures are gossiped in the clear over StackerDB sync/push) can resubmit it against a different StackerDB contract instance and have it accepted as an authentic write there, since `validate_received_chunk` only checks the version-monotonicity and chunk-size constraints of the *target* contract, not that the signature was ever intended for that contract: [8](#0-7) 

The equality that should hold — "signature is valid for the specific StackerDB context it was produced for" — is broken; the check actually enforced is only "signature recovers to the address that the *target* contract currently assigns to this slot," which is a strictly weaker, contract-agnostic condition.

### Impact Explanation
This allows an unauthenticated/unauthorized write into StackerDB state that the signer never intended for that specific contract, matching the "Critical: unauthenticated/unauthorized write to state or StackerDB" category. A replayed chunk can inject stale/foreign signer messages, votes, or protocol payloads into a different reward cycle's replica, corrupting state consumers rely on for signer coordination (e.g., mock signatures, block responses, state-machine updates) without the signer's consent for that context.

### Likelihood Explanation
Exploitation requires only a message the attacker can passively observe (StackerDB chunk gossip is public/unauthenticated to observe) plus the realistic condition that slot ownership coincides across contracts — a routine occurrence when a stable signer set persists across consecutive reward cycles or shares deterministic sort order. No secret key, node authority, or high traffic volume is needed; a single crafted POST of a previously valid chunk into a different `.signers-X-Y` StackerDB contract is sufficient.

### Recommendation
Bind the signed digest to the destination StackerDB context, mirroring the recommended EIP-712-style domain separation: include the `QualifiedContractIdentifier` (and ideally the network/chain identifier) in `SlotMetadata::auth_digest()` so that a signature is only valid for the specific StackerDB contract (and network) it was produced for. This requires updating `auth_digest`, `sign`, and `verify` in `libstackerdb/src/libstackerdb.rs`, plus threading the contract identifier through call sites in `stackslib/src/net/stackerdb/mod.rs` and `stackslib/src/net/stackerdb/db.rs`, with a versioned/backward-compatible rollout since this changes the wire-level signing contract.

### Proof of Concept
1. Reward cycle `N`: signer set is `[addr_A, addr_B, ...]` sorted by signing key; `addr_A` is assigned `slot_id = 0` in `.signers-0-N`. `addr_A` signs and gossips chunk `(slot_id=0, slot_version=1, data_hash=H, sig=S)`.
2. Reward cycle `N+2` (same parity, same/overlapping signer set with `addr_A` still sorted first): `.signers-0-(N+2)` is created with `addr_A` again owning `slot_id = 0`, version reset.
3. Attacker observed the tuple from step 1 (public gossip) and submits it verbatim to `.signers-0-(N+2)` via `POST /v2/stackerdb/.../chunks`.
4. `validate_received_chunk`/`RPCPostStackerDBChunkRequestHandler` calls `get_slot_signer(".signers-0-(N+2)", 0)` → `addr_A`, then `slot_metadata.verify(&addr_A)` — which recomputes `auth_digest()` from only `(0, 1, H)`, identical to step 1 — and succeeds, because `auth_digest()` never referenced `.signers-0-N` vs `.signers-0-(N+2)`.
5. The stale/foreign chunk is accepted and stored as if `addr_A` had freshly authored it for cycle `N+2`.

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

**File:** stackslib/src/net/stackerdb/mod.rs (L649-717)
```rust
    pub fn validate_received_chunk(
        &self,
        smart_contract_id: &QualifiedContractIdentifier,
        config: &StackerDBConfig,
        data: &StackerDBChunkData,
        expected_versions: &[u32],
    ) -> Result<bool, net_error> {
        // validate -- must not exceed this replica's configured chunk size.
        if (data.data.len() as u64) > config.chunk_size {
            info!(
                "Received StackerDBChunk for {} ID {}, which is oversized: {} bytes (max {} bytes)",
                smart_contract_id,
                data.slot_id,
                data.data.len(),
                config.chunk_size
            );
            return Ok(false);
        }

        // validate -- must be a valid chunk
        let Some(expected_version) = expected_versions.get(data.slot_id as usize) else {
            info!(
                "Received StackerDBChunk for {} ID {}, which is too big ({})",
                smart_contract_id,
                data.slot_id,
                expected_versions.len()
            );
            return Ok(false);
        };

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

        // validate -- must be the current or newer version
        if data.slot_version < *expected_version {
            info!(
                "Received StackerDBChunk for {} ID {} version {}, which is stale (expected {})",
                smart_contract_id, data.slot_id, data.slot_version, *expected_version
            );
            return Ok(false);
        }

        // validate -- must not exceed max writes
        if data.slot_version > config.max_writes {
            info!(
                "Write count exceeded for StackerDBChunk for {} ID {} version {} (max is {})",
                smart_contract_id, data.slot_id, data.slot_version, config.max_writes
            );
            return Ok(false);
        }

        Ok(true)
```

**File:** stackslib/src/net/stackerdb/db.rs (L530-543)
```rust
    /// Get the principal who signs a particular slot in a particular stacker DB.
    /// Returns Ok(Some(addr)) if this slot exists in the DB
    /// Returns Ok(None) if the slot does not exist
    /// Returns Err(..) if the DB doesn't exist of some other DB error happens
    pub fn get_slot_signer(
        &self,
        smart_contract: &QualifiedContractIdentifier,
        slot_id: u32,
    ) -> Result<Option<StacksAddress>, net_error> {
        let stackerdb_id = self.get_stackerdb_id(smart_contract)?;
        let sql = "SELECT signer FROM chunks WHERE stackerdb_id = ?1 AND slot_id = ?2";
        let args = params![stackerdb_id, slot_id];
        query_row(&self.conn, sql, args).map_err(|e| e.into())
    }
```

**File:** stackslib/src/chainstate/stacks/boot/mod.rs (L1039-1071)
```rust
        let mut signer_set = BTreeMap::new();
        for entry in entries.iter() {
            let signing_key = entry
                .signer
                .expect("BUG: signing keys should all be set in reward-sets with any signing keys");
            if let Some(existing_entry) = signer_set.get_mut(&signing_key) {
                *existing_entry += entry.amount_stacked;
            } else {
                signer_set.insert(signing_key, entry.amount_stacked);
            };
        }

        let mut signer_set: Vec<_> = signer_set
            .into_iter()
            .filter_map(|(signing_key, stacked_amt)| {
                let weight = u32::try_from(stacked_amt / threshold)
                    .expect("CORRUPTION: Stacker claimed > u32::max() reward slots");
                if weight == 0 {
                    return None;
                }
                Some(NakamotoSignerEntry {
                    signing_key,
                    stacked_amt,
                    weight,
                })
            })
            .collect();

        // finally, we must sort the signer set: the signer participation bit vector depends
        //  on a consensus-critical ordering of the signer set.
        signer_set.sort_by_key(|entry| entry.signing_key);

        Some(signer_set)
```

**File:** stackslib/src/chainstate/nakamoto/signer_set.rs (L581-601)
```rust
            signers
                .iter()
                .map(|signer| {
                    let signer_hash = Hash160::from_data(&signer.signing_key);
                    let signing_address = StacksAddress::p2pkh_from_hash(is_mainnet, signer_hash);
                    let tuple_data = TupleData::from_data(vec![
                        (
                            ClarityName::from_literal("signer"),
                            Value::Principal(PrincipalData::from(signing_address)),
                        ),
                        (ClarityName::from_literal("num-slots"), Value::UInt(1)),
                    ])
                    .map_err(|e| {
                        ChainstateError::Expects(format!(
                            "Failed to create tuple for stackerdb entry: {e}"
                        ))
                    })?;
                    Ok::<Value, ChainstateError>(Value::Tuple(tuple_data))
                })
                .collect::<Result<Vec<_>, _>>()?
        };
```

**File:** stacks-node/src/nakamoto_node/stackerdb_listener.rs (L227-239)
```rust
        let signer_entries = reward_set_signers
            .iter()
            .cloned()
            .enumerate()
            .map(|(idx, signer)| {
                let Ok(slot_id) = u32::try_from(idx) else {
                    return Err(ChainstateError::InvalidStacksBlock(
                        "Signer index exceeds u32".into(),
                    ));
                };
                Ok((slot_id, signer))
            })
            .collect::<Result<HashMap<_, _>, ChainstateError>>()?;
```
