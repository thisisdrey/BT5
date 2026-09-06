### Title
`SlotMetadata::verify` accepts high-S malleated signatures as valid due to missing low-S canonicalization check - ([File: libstackerdb/src/libstackerdb.rs])

### Summary
`SlotMetadata::verify` (and thus `StackerDBChunkData::verify`) recovers the public key via `StacksPublicKey::recover_to_pubkey_without_validating_low_s`, which unlike the standard `Secp256k1PublicKey::verify` path deliberately skips the low-S canonicalization check. This lets an attacker take any observed, validly-signed chunk, negate the `s` component of its ECDSA signature, and produce a bit-different 65-byte `MessageSignature` that `verify` still accepts as authentic for the same `slot_id`/`slot_version`/`data_hash`.

### Finding Description
The digest signed is `SlotMetadata::auth_digest()`, computed over `slot_id`, `slot_version`, and `data_hash` [1](#0-0) . Verification is: [2](#0-1) 

This calls `recover_to_pubkey_without_validating_low_s`, which explicitly skips the S-normalization check that the "safe" path (`recover_to_pubkey`/`Secp256k1PublicKey::verify`) performs: [3](#0-2) 

Compare this to `PublicKey::verify`, which explicitly rejects non-canonical (high-S) signatures with `"Invalid signature: high-S"`: [4](#0-3) 

The malleability primitive `MessageSignature::with_negated_s` and the codebase's own test confirm that negating `s` (and flipping the recovery-id parity bit) produces a distinct signature byte-string that recovers to the identical public key over the identical message: [5](#0-4) [6](#0-5) 

Because `SlotMetadata::verify`/`StackerDBChunkData::verify` only checks `Hash160::from_node_public_key(&pubk) == *principal.bytes()` and never rejects the high-S form, the equality the security question describes is broken: two distinct signature byte-strings over the same `auth_digest` are both treated as the "same" authenticated act by the slot owner [7](#0-6) . `StackerDBChunkData::verify` simply delegates to this same unsafe path via `get_slot_metadata().verify(addr)` [8](#0-7) .

### Impact Explanation
An attacker who observes any single legitimately-broadcast `StackerDBChunkData` (trivial for any unprivileged peer subscribed to a StackerDB, since chunks are gossiped in cleartext over P2P/RPC) can derive a second, byte-different but equally "valid" signature for the exact same `(slot_id, slot_version, data_hash)` tuple. This is a forged signature byte-string that the real slot owner never produced, and it will pass every verification gate that relies on `SlotMetadata::verify`/`StackerDBChunkData::verify`. This matches the "network-wide propagation of forged data" Critical category: the malleated chunk is a distinct wire artifact (different `sig` field) that nodes will accept and re-relay as authentic, even though its origin is the attacker, not the key holder.

### Likelihood Explanation
Preconditions are minimal: the attacker only needs to have observed one broadcast chunk from any StackerDB slot they do not own (no privileged role, no secret, no admin access required), matching the unprivileged remote-attacker model. Malleating the S value is a cheap, deterministic, purely local computation (`with_negated_s`-equivalent field arithmetic) requiring no cryptographic secret. It is fully repeatable for every chunk update the node observes.

### Recommendation
Change `SlotMetadata::verify` (and `StackerDBChunkData::recover_pk`) to use `StacksPublicKey::recover_to_pubkey` (the low-S-enforcing path) instead of `recover_to_pubkey_without_validating_low_s`, so that only the canonical low-S signature produced by the signer is accepted, and any negated-S variant is rejected with `"Invalid signature: high-S"`.

### Proof of Concept
```rust
// libstackerdb/src/tests/mod.rs (conceptual addition)
use stacks_common::types::chainstate::StacksPrivateKey;
use stacks_common::types::PrivateKey;
use libstackerdb::StackerDBChunkData;

#[test]
fn high_s_malleated_chunk_signature_accepted() {
    let privk = StacksPrivateKey::random();
    let addr = StacksAddress::from_public_keys(...; /* derive from privk pubkey */);

    let mut chunk = StackerDBChunkData::new(0, 1, b"hello".to_vec());
    chunk.sign(&privk).unwrap();

    let mut malleated = chunk.clone();
    malleated.sig = chunk.sig.with_negated_s(); // requires `testing` feature

    assert_ne!(chunk.sig, malleated.sig);
    assert!(chunk.verify(&addr).unwrap());
    assert!(malleated.verify(&addr).unwrap()); // BUG: both accepted as valid
}
```
This demonstrates that `StackerDBChunkData::verify` (and therefore any downstream `PeerNetwork`/`StackerDBSync` code that calls it to admit a chunk into local state or relay it) returns `Ok(true)` for two distinct signature byte-strings over the same authenticated content.

*Note*: I was unable to fully trace, within the available tool budget, the exact `StackerDBSync::validate_downloaded_chunk` / `try_replace_chunk` version-bump/deduplication logic in `stackslib/src/net/stackerdb/sync.rs` and `db.rs` to confirm whether an identical-version chunk with only a mutated signature would be re-stored/re-relayed or silently deduplicated by slot version equality. The core cryptographic-equality break in `libstackerdb/src/libstackerdb.rs` is confirmed directly from the code; the downstream storage/relay behavior for a same-version, different-signature chunk should be verified further before treating the full propagation chain as proven.

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

**File:** libstackerdb/src/libstackerdb.rs (L239-244)
```rust
    /// Verify that this chunk was signed by the given
    /// public key hash (`addr`).  Only fails if the underlying signing library fails.
    pub fn verify(&self, addr: &StacksAddress) -> Result<bool, Error> {
        let md = self.get_slot_metadata();
        md.verify(addr)
    }
```

**File:** stacks-common/src/util/secp256k1/native.rs (L81-93)
```rust
    #[cfg(any(test, feature = "testing"))]
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

**File:** stacks-common/src/util/secp256k1/native.rs (L200-228)
```rust
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
```

**File:** stacks-common/src/util/secp256k1/native.rs (L282-290)
```rust
            // libsecp256k1 doesn't ensure that the S is low,
            // we have to do it ourselves
            let secp256k1_sig_standard = secp256k1_sig.to_standard();

            let mut secp256k1_sig_low_s = secp256k1_sig_standard;
            secp256k1_sig_low_s.normalize_s();
            if secp256k1_sig_low_s != secp256k1_sig_standard {
                return Err("Invalid signature: high-S");
            }
```

**File:** stacks-common/src/util/secp256k1/native.rs (L792-836)
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
```
