### Title
BEEFY authority-set Merkle commitment can silently include the zero Ethereum address as a "valid" authority - ([File: substrate/frame/beefy-mmr/src/lib.rs])

### Summary
`BeefyEcdsaToEthereum::convert` maps each BEEFY validator's compressed ECDSA public key to an Ethereum address via `to_eth_address()`. If that conversion fails for any reason, the code silently substitutes the sentinel value `FAILED_BEEFY_TO_ETH_ADDRESS = [0u8; 20]` (the zero address) instead of rejecting the authority set or excluding the entry. This zero address is then folded into `keyset_commitment`, the Merkle root that downstream (Ethereum-side / Snowbridge-style) BEEFY light clients treat as the canonical, cryptographically-committed set of valid signers for that authority-set id.

### Finding Description
`BeefyEcdsaToEthereum::convert` in [1](#0-0)  converts a public key with `.map(|v| v.to_vec()).unwrap_or_else(|_| { ... FAILED_BEEFY_TO_ETH_ADDRESS.to_vec() })`. The sentinel is explicitly defined as the all-zero 20-byte address: [2](#0-1) .

This converted list of addresses is used, unfiltered, to build the committed authority set: [3](#0-2) 
Note that `compute_authority_set` only *logs* an error when it detects `FAILED_BEEFY_TO_ETH_ADDRESS` entries (`uninitialized_addresses > 0`) — it does not exclude them from the Merkle tree, does not abort authority-set rotation, and does not prevent the resulting `keyset_commitment` from being published on-chain and consumed by light clients.

This is the structural analog of the reported bug: an ECDSA-based access-control commitment silently accepts `address(0)` as a legitimate entry instead of treating conversion failure as "no valid signer." The upstream reported bug relied on `ecrecover` returning `address(0)` for a malformed signature (`v` not 27/28) and a downstream authorization check that didn't reject `address(0)`. Here, the exact same zero-address value is deliberately inserted into the *authority set commitment itself* whenever the address-derivation step fails — and any BEEFY light client that verifies validator signatures against this committed set using standard `ecrecover` semantics (which return `address(0)` for many classes of malformed/invalid signatures, e.g. wrong `v`, or bytes crafted so that recovery incidentally yields `address(0)`) would treat a bogus/attacker-crafted signature as belonging to a "valid" authority, because `address(0)` is present with a valid Merkle-inclusion proof in `keyset_commitment`.

The existing test `ecdsa_to_eth_falls_back_to_zero_address_on_invalid_key` confirms this fallback path is real and reachable for malformed BEEFY public keys: [4](#0-3) .

Existing guards do not stop this path: the only defense is a `log::error!` diagnostic in `compute_authority_set`, which has no on-chain effect — it neither halts the authority-set rotation nor filters the zero entry out of the committed Merkle tree [5](#0-4) .

### Impact Explanation
If a validator's BEEFY key ever fails `to_eth_address()` conversion (e.g., due to a malformed/edge-case public key that is nonetheless accepted as a valid BEEFY session key on the Substrate side), the chain will publish an authority-set commitment (`BeefyAuthoritySet`/`BeefyNextAuthoritySet`, exposed via the `BeefyMmrApi` runtime API and consumed by bridge light clients) that contains `address(0)` as a committed "authority." Any relayer/light-client verification logic on the consuming side that accepts standard `ecrecover`-style signature recovery (which can yield `address(0)` for crafted/invalid signatures) against this committed set would accept forged BEEFY commitments without any real validator signature — enabling forged finality/commitment proofs to be accepted by a bridge, i.e., forged or mis-bound proof acceptance with cross-chain fund-safety implications (a live-scope impact per the bridge/proof-binding pivot).

### Likelihood Explanation
The trigger condition (an ECDSA public key that fails `to_eth_address()`/decompression) is a narrow, key-dependent edge case rather than something any signer can force at will, so likelihood is lower than a fully attacker-controlled trigger. However, no validation exists anywhere in the authority-set rotation pipeline (`on_new_validator_set` → `compute_authority_set` → storage) to reject or filter such degenerate keys, so once triggered, the flaw propagates automatically into an on-chain, externally-consumed commitment without any additional privileged action.

### Recommendation
- In `compute_authority_set`, treat any address equal to `FAILED_BEEFY_TO_ETH_ADDRESS` as a hard error: fail the authority-set update (or exclude the entry and adjust `len`/commitment) rather than only logging.
- Alternatively, change `BeefyEcdsaToEthereum::convert`'s signature to return `Option<Vec<u8>>`/`Result` and have callers reject the whole authority-set computation on any conversion failure, guaranteeing the zero address can never enter `keyset_commitment`.
- Document and enforce that consuming light clients (Snowbridge/BEEFY bridge contracts) explicitly reject `address(0)` as a recovered signer, mirroring the OpenZeppelin-style zero-address guard recommended in the original report.

### Proof of Concept
1. Register (or otherwise get accepted as) a BEEFY session key whose compressed ECDSA public key fails `sp_core::ecdsa::Public::to_eth_address()` (demonstrated feasible by the existing unit test using `Public::from_raw([0xFF; 33])`, per [4](#0-3) ).
2. Trigger a BEEFY authority-set rotation including this validator; `on_new_validator_set` calls `compute_authority_set`, which calls `BeefyEcdsaToEthereum::convert` for every validator [6](#0-5) .
3. The failing key is silently mapped to `[0u8;20]` and merged into `beefy_addresses`, then hashed into `keyset_commitment` via `binary_merkle_tree::merkle_root` [7](#0-6) ; only a log line is emitted, with no abort.
4. A relayer submits this authority-set commitment (with Merkle proof for the `address(0)` leaf) to a downstream BEEFY light client and pairs it with a deliberately malformed BEEFY commitment signature engineered so standard `ecrecover` recovers `address(0)`.
5. The light client, verifying the malformed signature against the Merkle-committed authority set that legitimately contains `address(0)`, accepts the forged commitment as signed by a "valid" authority — reproducing the exact "phony signature accepted due to zero-address authority" pattern described in the source report.

### Citations

**File:** substrate/frame/beefy-mmr/src/lib.rs (L88-93)
```rust
/// Sentinel returned by [`BeefyEcdsaToEthereum`] when an ECDSA public key cannot be
/// converted to an Ethereum address. Both producer and consumer must reference this
/// constant so the two ends of the conversion can never drift to different sentinels
/// (see `Pallet::compute_authority_set`, which counts failed conversions by matching
/// against this value).
pub const FAILED_BEEFY_TO_ETH_ADDRESS: [u8; 20] = [0u8; 20];
```

**File:** substrate/frame/beefy-mmr/src/lib.rs (L96-107)
```rust
pub struct BeefyEcdsaToEthereum;
impl Convert<sp_consensus_beefy::ecdsa_crypto::AuthorityId, Vec<u8>> for BeefyEcdsaToEthereum {
	fn convert(beefy_id: sp_consensus_beefy::ecdsa_crypto::AuthorityId) -> Vec<u8> {
		sp_core::ecdsa::Public::from(beefy_id)
			.to_eth_address()
			.map(|v| v.to_vec())
			.unwrap_or_else(|_| {
				log::debug!(target: "runtime::beefy", "Failed to convert BEEFY PublicKey to ETH address!");
				FAILED_BEEFY_TO_ETH_ADDRESS.to_vec()
			})
	}
}
```

**File:** substrate/frame/beefy-mmr/src/lib.rs (L188-202)
```rust
impl<T> sp_consensus_beefy::OnNewValidatorSet<<T as pallet_beefy::Config>::BeefyId> for Pallet<T>
where
	T: pallet::Config,
{
	/// Compute and cache BEEFY authority sets based on updated BEEFY validator sets.
	fn on_new_validator_set(
		current_set: &BeefyValidatorSet<<T as pallet_beefy::Config>::BeefyId>,
		next_set: &BeefyValidatorSet<<T as pallet_beefy::Config>::BeefyId>,
	) {
		let current = Pallet::<T>::compute_authority_set(current_set);
		let next = Pallet::<T>::compute_authority_set(next_set);
		// cache the result
		BeefyAuthorities::<T>::put(&current);
		BeefyNextAuthorities::<T>::put(&next);
	}
```

**File:** substrate/frame/beefy-mmr/src/lib.rs (L346-375)
```rust
	fn compute_authority_set(
		validator_set: &BeefyValidatorSet<<T as pallet_beefy::Config>::BeefyId>,
	) -> BeefyAuthoritySet<MerkleRootOf<T>> {
		let id = validator_set.id();
		let beefy_addresses = validator_set
			.validators()
			.into_iter()
			.cloned()
			.map(T::BeefyAuthorityToMerkleLeaf::convert)
			.collect::<Vec<_>>();
		let len = beefy_addresses.len() as u32;
		let uninitialized_addresses = beefy_addresses
			.iter()
			.filter(|&addr| addr.as_slice().eq(&FAILED_BEEFY_TO_ETH_ADDRESS))
			.count();
		if uninitialized_addresses > 0 {
			log::error!(
				target: "runtime::beefy",
				"Failed to convert {} out of {} BEEFY PublicKeys to ETH addresses!",
				uninitialized_addresses,
				len,
			);
		}
		let keyset_commitment = binary_merkle_tree::merkle_root::<
			<T as pallet_mmr::Config>::Hashing,
			_,
		>(beefy_addresses)
		.into();
		BeefyAuthoritySet { id, len, keyset_commitment }
	}
```

**File:** substrate/frame/beefy-mmr/src/tests.rs (L392-401)
```rust
#[test]
fn ecdsa_to_eth_falls_back_to_zero_address_on_invalid_key() {
	use sp_runtime::traits::Convert;

	// Malformed ECDSA public key — `to_eth_address` fails on this input.
	let malformed = BeefyId::from(sp_core::ecdsa::Public::from_raw([0xFF; 33]));
	assert_eq!(
		crate::BeefyEcdsaToEthereum::convert(malformed),
		crate::FAILED_BEEFY_TO_ETH_ADDRESS.to_vec(),
	);
```
