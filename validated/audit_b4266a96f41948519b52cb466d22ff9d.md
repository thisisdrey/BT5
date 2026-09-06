### Title
`SlotMetadata::auth_digest` omits the smart-contract identifier, enabling cross-StackerDB chunk replay - (File: `libstackerdb/src/libstackerdb.rs`)

### Summary
`SlotMetadata::auth_digest` computes the signed digest from only `slot_id`, `slot_version`, and `data_hash`, never the target StackerDB's `QualifiedContractIdentifier`. Both the RPC chunk-write path (`stackerdb/db.rs::try_replace_chunk`) and the P2P push-chunk path (`stackerdb/mod.rs::validate_received_chunk`) verify the signature against a contract-specific signer address they look up independently, but the digest itself carries no binding to which contract it was signed for, so a signature produced for slot 3 of contract A verifies equally well against slot 3 of contract B whenever the two contracts happen to assign the same address to that slot index.

### Finding Description
The signed digest is built here: [1](#0-0) , and used by both `SlotMetadata::sign`/`verify` [2](#0-1)  and `StackerDBChunkData::sign`/`verify` [3](#0-2) .

On the write path, `StackerDBs::try_replace_chunk` receives a `smart_contract: &QualifiedContractIdentifier` argument, looks up the *contract-specific* slot owner via `get_slot_validation(smart_contract, slot_desc.slot_id)`, and then calls `slot_desc.verify(&slot_validation.signer)` [4](#0-3) . Because `verify` only recovers a pubkey hash from `auth_digest` (which excludes the contract id) and compares it to the supplied address, any signature that is cryptographically valid for `(slot_id, slot_version, data_hash)` verifies successfully against contract B's slot-3 owner address, even if it was produced with contract A in mind - as long as the two contracts happen to list the same `StacksAddress` for slot 3 (a realistic condition, since slot-to-signer assignment for `.signers-*` contracts in the same reward cycle is independently computed per contract from the same underlying PoX signer set, per `eval_signer_slots`/`parse_slot_entry` [5](#0-4) ).

The identical logic exists on the P2P/gossip validation path: `validate_received_chunk` fetches the address via `get_slot_signer(smart_contract_id, data.slot_id)` and then calls `slot_metadata.verify(&addr)` [6](#0-5) , with the same missing contract binding.

An attacker who is not the private-key holder, and who does not own slot 3 in contract B, can observe a legitimately-signed `StackerDBChunkData{slot_id:3, slot_version:7, data}` broadcast/posted for contract A (StackerDB chunk traffic is not confidential), and resubmit the identical bytes to contract B's `POST /v2/stackerdb/{contract_B}/{name}/chunks` endpoint or gossip it over P2P. Because `auth_digest` never included contract A's identifier, the same signature authenticates against contract B's slot-3 owner too, and the chunk is written to contract B's replica without the signer ever intending or authorizing that write for contract B.

### Impact Explanation
This is an unauthenticated write into a StackerDB replica: the attacker, holding no private key and owning no slot in contract B, forces storage of attacker-chosen (though signer-produced) bytes into contract B's slot 3 at a version of the attacker's choosing (bounded by `max_writes` and requiring it to exceed B's current version for that slot). This is repeatable for every such cross-contract slot-owner collision and for every future signed chunk the attacker observes, and it propagates via the normal StackerDB sync/relay path to all replicas of contract B. This matches the "unauthenticated/unauthorized write to StackerDB" Critical category.

### Likelihood Explanation
Preconditions: two StackerDB configs (realistically achievable with the `.signers-*` family of contracts used for the same reward cycle) whose slot-3 owner resolves to the same `StacksAddress`, which the code independently derives per contract from `stackerdb-get-signer-slots` [7](#0-6) . Attacker cost is minimal: observe one publicly-relayed signed chunk and resend it to a different contract's chunk endpoint or gossip channel. No secrets, admin role, or privileged position are required, and the write and RPC/P2P surfaces are remotely reachable.

### Recommendation
Include the target `QualifiedContractIdentifier` (and ideally a `chain_id`/network discriminator) inside `SlotMetadata::auth_digest`, requiring `SlotMetadata::sign`/`verify` and `StackerDBChunkData::sign`/`verify` to take the contract id as an explicit parameter, so a signature computed for one StackerDB contract can never validate against another.

### Proof of Concept
Rust test in `stackslib::net::stackerdb::tests`:
1. Build two `StackerDBConfig`s (`config_a`, `config_b`) for contracts A and B where `signers[3] == addr` (same `StacksAddress`) in both.
2. Create `StackerDBChunkData{slot_id:3, slot_version:7, data}`, sign with `addr`'s private key (this is intended for contract A).
3. Call `stackerdbs.try_replace_chunk(&contract_b_id, &chunk.get_slot_metadata(), &chunk.data)` (or `validate_received_chunk(&contract_b_id, &config_b, &chunk, expected_versions)`).
4. Assert the call returns `Ok(())`/`Ok(true)` (accepted) despite the signature never having been produced with contract B's identity in mind - proving `auth_digest`'s missing contract binding lets a chunk signed for A be forged/replayed into B.

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

**File:** libstackerdb/src/libstackerdb.rs (L171-193)
```rust
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

**File:** libstackerdb/src/libstackerdb.rs (L226-244)
```rust
    pub fn sign(&mut self, privk: &StacksPrivateKey) -> Result<(), Error> {
        let mut md = self.get_slot_metadata();
        md.sign(privk)?;
        self.sig = md.signature;
        Ok(())
    }

    pub fn recover_pk(&self) -> Result<StacksPublicKey, Error> {
        let digest = self.get_slot_metadata().auth_digest();
        StacksPublicKey::recover_to_pubkey_without_validating_low_s(digest.as_bytes(), &self.sig)
            .map_err(|ve| Error::VerifyingError(ve.to_string()))
    }

    /// Verify that this chunk was signed by the given
    /// public key hash (`addr`).  Only fails if the underlying signing library fails.
    pub fn verify(&self, addr: &StacksAddress) -> Result<bool, Error> {
        let md = self.get_slot_metadata();
        md.verify(addr)
    }
```

**File:** stackslib/src/net/stackerdb/db.rs (L411-423)
```rust
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

**File:** stackslib/src/net/stackerdb/config.rs (L165-203)
```rust
    fn parse_slot_entry(
        entry: ClarityValue,
        contract_id: &QualifiedContractIdentifier,
    ) -> Result<(StacksAddress, u32), String> {
        let ClarityValue::Tuple(slot_data) = entry else {
            let reason = format!(
                "StackerDB fn `{contract_id}.{STACKERDB_SLOTS_FUNCTION}` returned non-tuple slot entry",
            );
            return Err(reason);
        };

        let Ok(ClarityValue::Principal(signer_principal)) = slot_data.get("signer") else {
            let reason = format!(
                "StackerDB fn `{contract_id}.{STACKERDB_SLOTS_FUNCTION}` returned tuple without `signer` entry of type `principal`",
            );
            return Err(reason);
        };

        let Ok(ClarityValue::UInt(num_slots)) = slot_data.get("num-slots") else {
            let reason = format!(
                "StackerDB fn `{contract_id}.{STACKERDB_SLOTS_FUNCTION}` returned tuple without `num-slots` entry of type `uint`",
            );
            return Err(reason);
        };

        let num_slots = u32::try_from(*num_slots)
            .map_err(|_| format!("Contract `{contract_id}` set too many slots for one signer (max = {STACKERDB_INV_MAX})"))?;
        if num_slots > STACKERDB_INV_MAX {
            return Err(format!("Contract `{contract_id}` set too many slots for one signer (max = {STACKERDB_INV_MAX})"));
        }

        let PrincipalData::Standard(standard_principal) = signer_principal else {
            return Err(format!(
                "StackerDB contract `{contract_id}` set a contract principal as a writer, which is not supported"
            ));
        };
        let addr = StacksAddress::from(standard_principal.clone());
        Ok((addr, num_slots))
    }
```

**File:** stackslib/src/net/stackerdb/config.rs (L205-278)
```rust
    fn eval_signer_slots(
        chainstate: &mut StacksChainState,
        burn_dbconn: &dyn BurnStateDB,
        contract_id: &QualifiedContractIdentifier,
        tip: &StacksBlockId,
    ) -> Result<Vec<(StacksAddress, u32)>, NetError> {
        let value = chainstate.eval_read_only(
            burn_dbconn,
            tip,
            contract_id,
            &format!("({STACKERDB_SLOTS_FUNCTION})"),
        )?;

        let result = value.expect_result()?;
        let slot_list = match result {
            Err(err_val) => {
                let err_code = err_val.expect_u128()?;
                let reason = format!(
                    "Contract {} failed to run `stackerdb-get-signer-slots`: error u{}",
                    contract_id, &err_code
                );
                warn!("{}", &reason);
                return Err(NetError::InvalidStackerDBContract(
                    contract_id.clone(),
                    reason,
                ));
            }
            Ok(ok_val) => ok_val.expect_list()?,
        };

        let mut total_num_slots = 0u32;
        let mut ret = vec![];
        for slot_value in slot_list.into_iter() {
            let (addr, num_slots) =
                Self::parse_slot_entry(slot_value, contract_id).map_err(|e| {
                    warn!("Failed to parse StackerDB slot entry: {}", &e);
                    NetError::InvalidStackerDBContract(contract_id.clone(), e)
                })?;

            if num_slots > STACKERDB_INV_MAX {
                let reason = format!(
                    "Contract {} stipulated more than maximum number of slots for one signer ({})",
                    contract_id, STACKERDB_INV_MAX
                );
                warn!("{}", &reason);
                return Err(NetError::InvalidStackerDBContract(
                    contract_id.clone(),
                    reason,
                ));
            }

            total_num_slots =
                total_num_slots
                    .checked_add(num_slots)
                    .ok_or(NetError::OverflowError(format!(
                        "Contract {} stipulates more than u32::MAX slots",
                        &contract_id
                    )))?;

            if total_num_slots > STACKERDB_INV_MAX {
                let reason = format!(
                    "Contract {contract_id} stipulated more than the maximum number of slots"
                );
                warn!("{reason}");
                return Err(NetError::InvalidStackerDBContract(
                    contract_id.clone(),
                    reason,
                ));
            }

            ret.push((addr, num_slots));
        }
        Ok(ret)
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
