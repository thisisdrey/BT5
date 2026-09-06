### Title
StackerDB Chunk Signature Malleability via Missing Low-S Check in `SlotMetadata::verify` - ([File: libstackerdb/src/libstackerdb.rs])

### Summary
`SlotMetadata::verify()`, used to authenticate every StackerDB chunk write and every chunk relayed over the p2p network, recovers the signer's public key with `recover_to_pubkey_without_validating_low_s`, explicitly skipping the low-S / signature-normalization check that the sibling `recover_to_pubkey` enforces. This is the ECDSA/secp256k1 analog of the Ed25519 "missing S < L check" bug class in the external report: for any valid signature `(R, S)`, the malleated signature `(R, n-S)` (with the recovery-id parity flipped) recovers to the *same* public key and is accepted as valid, producing a second, byte-distinct, but equally "valid" signed message for identical `(slot_id, slot_version, data_hash)`.

### Finding Description
`stacks-common/src/util/secp256k1/native.rs` defines two recovery paths:

- `recover_to_pubkey` → `recover_to_pubkey_possibly_with_low_s_verification(msg, sig, true)` — normalizes `S` and rejects high-S signatures.
- `recover_to_pubkey_without_validating_low_s` → same helper with `verify_low_s = false` — skips that check entirely. [1](#0-0) 

`SlotMetadata::verify` (the function that authenticates every StackerDB chunk against its claimed owner) calls the *unchecked* variant: [2](#0-1) 

The codebase itself proves the malleability path exists and is accepted: `MessageSignature::with_negated_s()` produces the "other" valid ECDSA signature (negate `S` mod `n`, flip the recovery-id parity bit), and the existing unit test explicitly documents that this high-S variant still verifies successfully against `SlotMetadata::verify`: [3](#0-2) [4](#0-3) 

Both signatures recover to the identical public key (proven by `test_with_negated_s`): [5](#0-4) 

This function is the sole authentication gate used both when a node stores a chunk locally (`StackerDBChunkData::verify` → `SlotMetadata::verify`) and when it validates a chunk received via download or gossip push, before storing and re-broadcasting it: [6](#0-5) [7](#0-6) 

Because the owner-check itself (`Secp256k1PublicKey::verify`, used elsewhere for e.g. transaction validation) *does* enforce low-S: [8](#0-7) 

the StackerDB code path is inconsistent with the rest of the codebase and deliberately bypasses that protection — the doc comment even flags it as a legacy/should-not-use-in-new-code function: "You shouldn't use this in new code." [9](#0-8) 

### Impact Explanation
Any remote, unprivileged peer who observes one legitimately signed chunk `(slot_id, slot_version, data_hash, sig)` for a StackerDB (chunks are gossiped/served openly to all replicating peers) can compute the malleated signature `sig' = with_negated_s(sig)` without knowing the private key, and produce a distinct `StackerDBChunkData`/`StackerDBPushChunkData` message that still passes `validate_received_chunk` / `SlotMetadata::verify` at every peer in the network: [10](#0-9) 

Since `slot_version` is unchanged, the store operation itself is a no-op (same version, same data), so this does not corrupt state or bypass version freshness/write-count checks. However, it produces a second wire-distinct, independently "validly signed" byte sequence for state that any node cache, log, or downstream consumer keying off the raw signature bytes (e.g., for message-uniqueness/anti-replay bookkeeping) would treat as a new, independent authenticated write, and it can be freely re-relayed through `handle_unsolicited_StackerDBPushChunk`, causing every node to re-run signature verification and re-process what is nominally "the same" chunk. This matches the report's core impact class ("signature malleability... systems deduplicating by signature value accept the same message twice") rather than a state-corruption or auth-bypass bug, since the underlying `(slot_id, slot_version, data)` tuple is unaffected and the recovered address is correct.

### Likelihood Explanation
Trivial and remote: no secrets are needed. An attacker only needs to observe a single valid, already-broadcast StackerDB chunk (which is intentionally publicly propagated) and perform a constant-time scalar negation to derive the second valid signature; the vulnerable path is reached by any node handling `StackerDBChunkData`/`StackerDBPushChunkData`, download replies, or unsolicited pushes on the p2p network via `validate_received_chunk`.

### Recommendation
Change `SlotMetadata::verify` in `libstackerdb/src/libstackerdb.rs` to call `StacksPublicKey::recover_to_pubkey` (the low-S-enforcing variant) instead of `recover_to_pubkey_without_validating_low_s`, and/or additionally verify with `Secp256k1PublicKey::verify` (which already performs the low-S check) once the recovered key is known. This closes the analog of the missing `S < L` (here, high-S) canonical-signature check.

### Proof of Concept
```rust
// From libstackerdb/src/tests/mod.rs (existing test demonstrates the malleability is accepted):
let mut slot_metadata = chunk_data.get_slot_metadata();
slot_metadata.sign(&pk).unwrap();
assert!(slot_metadata.verify(&addr).unwrap()); // original signature: valid

// Attacker, without knowing `pk`, negates S and flips recovery-id parity:
slot_metadata.signature = slot_metadata.signature.with_negated_s();
assert!(slot_metadata.verify(&addr).unwrap()); // malleated signature: ALSO valid
``` [11](#0-10) 

This second, distinct 65-byte signature can be substituted into a `StackerDBChunkData`/`StackerDBPushChunkData` message and will pass `validate_received_chunk` on every remote peer that receives it via gossip or direct chunk push. [7](#0-6)

### Citations

**File:** stacks-common/src/util/secp256k1/native.rs (L82-93)
```rust
    pub fn with_negated_s(&self) -> Self {
        let mut bytes = [0u8; 65];
        bytes.copy_from_slice(self.as_bytes());

        // A `PrivateKey` is just a number, and it conveniently has a .negate()
        // method (mod n), so we'll just use that.
        let s = LibSecp256k1PrivateKey::from_slice(&bytes[33..]).unwrap();
        let neg = s.negate();
        bytes[33..].copy_from_slice(&neg.secret_bytes()[..]);
        bytes[0] ^= 1; // invert the parity of the recovery id
        Self(bytes)
    }
```

**File:** stacks-common/src/util/secp256k1/native.rs (L189-239)
```rust
    /// recover message and signature to public key (will be compressed)
    pub fn recover_to_pubkey(
        msg: &[u8],
        sig: &MessageSignature,
    ) -> Result<Secp256k1PublicKey, &'static str> {
        Self::recover_to_pubkey_possibly_with_low_s_verification(msg, sig, true)
    }

    /// Recover message and signature to public key (will be compressed), while
    /// skipping validation that the signature is normalized to low-S. You shouldn't
    /// use this in new code.
    pub fn recover_to_pubkey_without_validating_low_s(
        msg: &[u8],
        sig: &MessageSignature,
    ) -> Result<Secp256k1PublicKey, &'static str> {
        Self::recover_to_pubkey_possibly_with_low_s_verification(msg, sig, false)
    }

    fn recover_to_pubkey_possibly_with_low_s_verification(
        msg: &[u8],
        sig: &MessageSignature,
        verify_low_s: bool,
    ) -> Result<Secp256k1PublicKey, &'static str> {
        _secp256k1.with(|ctx| {
            let msg = LibSecp256k1Message::from_slice(msg).map_err(|_e| {
                "Invalid message: failed to decode data hash: must be a 32-byte hash"
            })?;

            let secp256k1_sig = sig
                .to_secp256k1_recoverable()
                .ok_or("Invalid signature: failed to decode recoverable signature")?;

            if verify_low_s {
                let secp256k1_sig_standard = secp256k1_sig.to_standard();
                let mut secp256k1_sig_low_s = secp256k1_sig_standard;
                secp256k1_sig_low_s.normalize_s();
                if secp256k1_sig_low_s != secp256k1_sig_standard {
                    return Err("Invalid signature: high-S");
                }
            }

            let recovered_pubkey = ctx
                .recover_ecdsa(&msg, &secp256k1_sig)
                .map_err(|_e| "Invalid signature: failed to recover public key")?;

            Ok(Secp256k1PublicKey {
                key: recovered_pubkey,
                compressed: true,
            })
        })
    }
```

**File:** stacks-common/src/util/secp256k1/native.rs (L263-294)
```rust
    fn verify(&self, data_hash: &[u8], sig: &MessageSignature) -> Result<bool, &'static str> {
        _secp256k1.with(|ctx| {
            let msg = LibSecp256k1Message::from_slice(data_hash).map_err(|_e| {
                "Invalid message: failed to decode data hash: must be a 32-byte hash"
            })?;

            let secp256k1_sig = sig
                .to_secp256k1_recoverable()
                .ok_or("Invalid signature: failed to decode recoverable signature")?;

            let recovered_pubkey = ctx
                .recover_ecdsa(&msg, &secp256k1_sig)
                .map_err(|_e| "Invalid signature: failed to recover public key")?;

            if recovered_pubkey != self.key {
                test_debug!("{:?} != {:?}", &recovered_pubkey, &self.key);
                return Ok(false);
            }

            // libsecp256k1 doesn't ensure that the S is low,
            // we have to do it ourselves
            let secp256k1_sig_standard = secp256k1_sig.to_standard();

            let mut secp256k1_sig_low_s = secp256k1_sig_standard;
            secp256k1_sig_low_s.normalize_s();
            if secp256k1_sig_low_s != secp256k1_sig_standard {
                return Err("Invalid signature: high-S");
            }

            Ok(true)
        })
    }
```

**File:** stacks-common/src/util/secp256k1/native.rs (L792-837)
```rust
    #[test]
    fn test_with_negated_s() {
        let priv_key = Secp256k1PrivateKey::from_hex(
            "7b48329a5126dad83fc583c309c2698ae2843acfb9a7023fb081d850386c6950",
        )
        .unwrap();
        let pub_key = Secp256k1PublicKey::from_private(&priv_key);
        let message =
            &hex_bytes("77949dd27dabb40847564f40afcde8b91e0f7baf2cc710415a4ac8b777104866").unwrap()
                [..];
        let original_sig = priv_key.sign(message).unwrap();
        let high_s_sig = original_sig.with_negated_s();

        assert_ne!(
            original_sig, high_s_sig,
            "low-S and high-S signatures should not be the same"
        );

        assert_eq!(
            original_sig,
            high_s_sig.with_negated_s(),
            "negating twice should bring back the original"
        );

        let (recovered_from_orig, recovered_from_high_s) = _secp256k1.with(|ctx| {
            let msg = LibSecp256k1Message::from_slice(message).unwrap();

            let secp256k1_orig_sig = original_sig.to_secp256k1_recoverable().unwrap();
            let recovered_from_orig = ctx.recover_ecdsa(&msg, &secp256k1_orig_sig).unwrap();

            let secp256k1_high_s_sig = high_s_sig.to_secp256k1_recoverable().unwrap();
            let recovered_from_high_s = ctx.recover_ecdsa(&msg, &secp256k1_high_s_sig).unwrap();

            (recovered_from_orig, recovered_from_high_s)
        });

        assert_eq!(
            recovered_from_orig, recovered_from_high_s,
            "both signatures should recover to the same public key"
        );

        assert_eq!(
            recovered_from_high_s, pub_key.key,
            "the recovered key should be identical to the original key"
        );
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

**File:** libstackerdb/src/libstackerdb.rs (L239-245)
```rust
    /// Verify that this chunk was signed by the given
    /// public key hash (`addr`).  Only fails if the underlying signing library fails.
    pub fn verify(&self, addr: &StacksAddress) -> Result<bool, Error> {
        let md = self.get_slot_metadata();
        md.verify(addr)
    }
}
```

**File:** libstackerdb/src/tests/mod.rs (L45-53)
```rust
    let mut slot_metadata = chunk_data.get_slot_metadata();
    slot_metadata.sign(&pk).unwrap();

    assert!(slot_metadata.verify(&addr).unwrap());

    // succeeds with high-S signature (that's not necessarily good, but
    // since this has always worked, it can't just stop)
    slot_metadata.signature = slot_metadata.signature.with_negated_s();
    assert!(slot_metadata.verify(&addr).unwrap());
```

**File:** stackslib/src/net/stackerdb/mod.rs (L649-718)
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
    }
```
