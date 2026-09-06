### Title
Missing StackerDB context/domain binding in chunk signatures allows cross-instance replay of stale signed chunks - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
StackerDB chunk authenticity is verified by recovering a public key from a digest computed only over `slot_id`, `slot_version`, and `data_hash` — it never binds the signature to the specific StackerDB smart contract (`contract_id`), reward cycle, or network. Because the Stacks signer-slot allocation scheme (`signers.clar`) recycles only two persistent slot pages (`reward_cycle % 2`), the same signer address is very commonly re-assigned to the same `slot_id` across many different reward-cycle contracts. An unprivileged network peer who has observed (via ordinary gossip) a validly-signed chunk for one reward cycle's StackerDB instance can later replay that exact chunk (same bytes/signature) into a different reward-cycle's StackerDB instance where the identical signer still owns that slot, and it will pass signature verification because the digest carries no information distinguishing the two contexts.

### Finding Description
The signed digest for a StackerDB chunk is: [1](#0-0) 

and verification only checks that the recovered public key hash equals the `addr` supplied by the caller — the caller resolves `addr` purely from `(smart_contract_id, slot_id)`, but that `contract_id` is never mixed into the signed bytes: [2](#0-1) 

The actual acceptance path, `validate_received_chunk`, looks up the expected signer strictly from `(smart_contract_id, slot_id)` and then calls `slot_metadata.verify(&addr)`, with no additional binding to which contract/DB instance the chunk was originally produced for: [3](#0-2) 

The only other guards are chunk size, "version must be >= expected" (not strict equality), and a `max_writes` ceiling: [4](#0-3) 

Because Stacks only maintains **two** persistent StackerDB signer-slot pages, cycled via `reward_cycle % 2`, and because slot assignment is written wholesale per cycle without incorporating cycle identity into the per-chunk cryptographic material: [5](#0-4) [6](#0-5) 

a signer that remains active across cycles frequently keeps the same `slot_id` in the alternating `.signers-{0,1}-N` contracts. This is the direct structural analog of the EIP-155 issue: just as the zkSync legacy-tx digest can omit `chainId` (the domain separator distinguishing networks), the StackerDB chunk digest omits `contract_id`/cycle identity (the domain separator distinguishing StackerDB instances). A signature that is valid for one instance/context therefore remains a cryptographically valid signature for a different instance/context, with no consent from the signer for that particular reuse.

### Impact Explanation
An attacker who never controls any signer's private key can capture any previously broadcast, validly-signed `StackerDBChunkData` (these are gossiped in cleartext over the P2P network and via `StackerDBPushChunk`/`StackerDBGetChunk`) and later re-submit it — unsolicited — into a different StackerDB replica/contract where the same signer address currently owns the identical `slot_id`. Since `validate_received_chunk` only requires `data.slot_version >= expected_version` (not equality) and the target contract is a fresh instance whose slots typically start at low/zero version, the replayed stale chunk will satisfy the check, be accepted into local state via `handle_unsolicited_StackerDBPushChunk`, and be relayed onward to other peers as legitimate new data for that reward cycle's signer set. This is an unauthorized write into StackerDB state and network-wide propagation of forged/stale data performed entirely by an unprivileged remote peer, matching the Critical bar ("unauthenticated/unauthorized write to state or StackerDB" and "network-wide propagation of forged data").

### Likelihood Explanation
Exploitation requires no privileged access or secret key — only observing gossip traffic (which the protocol broadcasts to any connected peer) and knowledge of which `slot_id` a given signer address occupies in two different reward-cycle contracts, which is public, on-chain, queryable information (`stackerdb-get-signer-slots-page`, `get_parsed_signer_slots`). Given the `reward_cycle % 2` slot-page reuse, the precondition (same signer, same slot, different cycle-contract) recurs routinely for any signer that stays active across consecutive cycles, making this readily reachable rather than a rare edge case.

### Recommendation
Bind the StackerDB chunk signature to its full context by including the `contract_id` (and ideally a chain/network identifier) inside `SlotMetadata::auth_digest`, e.g. hash `contract_id` bytes together with `slot_id`, `slot_version`, and `data_hash`, so a signature produced for one StackerDB replica cannot verify against another. This mirrors enforcing EIP-155's chainId inclusion in the signed payload.

### Proof of Concept
1. Signer `S` legitimately signs and broadcasts a chunk for `slot_id=K`, `slot_version=V` in cycle `C`'s contract `.signers-0-C` (verified via `SlotMetadata::verify` at [2](#0-1) ).
2. An unprivileged observer records this `StackerDBChunkData` (slot_id, slot_version, sig, data) from gossip.
3. Two cycles later, `.signers-0-(C+2)` is created (`stackerdb-set-signer-slots`, page 0) with `S` again assigned `slot_id=K` (per [5](#0-4) ), and the fresh slot's `expected_version` is low.
4. The observer submits the recorded chunk verbatim as an unsolicited `StackerDBPushChunk` targeting `.signers-0-(C+2)`. `validate_received_chunk` resolves the expected signer to `S` for `slot_id=K` in the new contract, calls `slot_metadata.verify(&S)`, which succeeds because the digest never encoded which contract it was originally signed for ( [3](#0-2) ), and the version check passes since `V >= expected_version` ( [7](#0-6) ).
5. The stale/forged chunk is accepted and relayed network-wide as if newly authored for cycle `C+2`.

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

**File:** libstackerdb/src/libstackerdb.rs (L181-193)
```rust
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

**File:** stackslib/src/net/stackerdb/mod.rs (L699-716)
```rust
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

```

**File:** stackslib/src/chainstate/stacks/boot/signers.clar (L12-24)
```text
;; Called internally by the Stacks node.
;; Stores the stackerdb signer slots for a given reward cycle.
;; Since there is one stackerdb per signer message, the `num-slots` field will always be u1.
(define-private (stackerdb-set-signer-slots 
                   (signer-slots (list 4000 { signer: principal, num-slots: uint }))
                   (reward-cycle uint)
                   (set-at-height uint))
	(let ((cycle-mod (mod reward-cycle u2)))
        (map-set cycle-set-height reward-cycle set-at-height)
        (var-set last-set-cycle reward-cycle)
        (if (is-eq cycle-mod u0)
            (ok (var-set stackerdb-signer-slots-0 signer-slots))
            (ok (var-set stackerdb-signer-slots-1 signer-slots)))))
```

**File:** stacks-signer/src/client/stacks_client.rs (L245-271)
```rust
    /// Get the stackerdb signer slots for a specific reward cycle
    pub fn get_parsed_signer_slots(
        &self,
        reward_cycle: u64,
    ) -> Result<HashMap<StacksAddress, SignerSlotID>, ClientError> {
        debug!("StacksClient: Getting parsed signer slots";
            "reward_cycle" => reward_cycle,
        );
        let signer_set =
            u32::try_from(reward_cycle % 2).expect("FATAL: reward_cycle % 2 exceeds u32::MAX");
        let signer_stackerdb_contract_id = boot_code_id(SIGNERS_NAME, self.mainnet);
        // Get the signer writers from the stacker-db to find the signer slot id
        let stackerdb_signer_slots =
            self.get_stackerdb_signer_slots(&signer_stackerdb_contract_id, signer_set)?;
        Ok(stackerdb_signer_slots
            .into_iter()
            .enumerate()
            .map(|(index, (address, _))| {
                (
                    address,
                    SignerSlotID(
                        u32::try_from(index).expect("FATAL: number of signers exceeds u32::MAX"),
                    ),
                )
            })
            .collect())
    }
```
