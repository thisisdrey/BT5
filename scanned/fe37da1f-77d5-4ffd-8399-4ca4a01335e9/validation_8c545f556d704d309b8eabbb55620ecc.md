### Title
Ring-VRF authority ring built from unchecked (non-subgroup-validated) Bandersnatch public keys — malformed key silently coerced to padding point - (File: substrate/primitives/core/src/bandersnatch.rs)

### Summary
The external report's core defect is that Umbral does not restrict which elliptic curve/point representation is accepted, so downstream code that deserializes or does arithmetic on such points can hit undefined/ill-defined behavior — most concretely the failure to validate that a supplied point actually lies in the correct prime-order subgroup, opening the door to small-subgroup / invalid-point issues. The same class of defect exists in this repository's Bandersnatch ring-VRF machinery: `RingContext::make_ring_vector` deserializes authority public keys with `deserialize_compressed_unchecked`, which performs neither an on-curve nor a subgroup-membership check, and silently substitutes a fixed `padding_point` on any deserialization failure rather than rejecting the input.

### Finding Description
`sp_core::bandersnatch::ring_vrf::RingContext::make_ring_vector` is the single choke point used to build both the ring **prover** key and the ring **verifier** key from a list of `Public` keys: [1](#0-0) 

It calls `AffinePoint::deserialize_compressed_unchecked`, the explicitly "unchecked" Arkworks deserialization variant that skips both the on-curve check and the subgroup-membership check that the checked `deserialize_compressed` performs. Any bytes that fail to deserialize at all fall back to `RingSetup::padding_point()` — but bytes that *do* decode to a valid curve point that is **not** in the correct prime-order subgroup (e.g., a low-order/cofactor point on the twisted-Edwards Bandersnatch curve, which is exactly the "curve with a cofactor other than one" case called out in the external report) are accepted as-is and folded into the ring commitment used for both proving and verification.

This is used to construct the authority ring in both BEEFY/Sassafras-style ring-VRF flows, e.g. `Pallet::update_ring_verifier` in `substrate/frame/sassafras/src/lib.rs` builds `verifier_key(&pks)` directly from the stored `AuthorityId` list: [2](#0-1) 

Compare this to the individual (non-ring) VRF verification path, `vrf::vrf_verify`, which uses the **checked** `bandersnatch::Public::deserialize_compressed` (not the `_unchecked` variant): [3](#0-2) 

The asymmetry shows the checked path is available and used elsewhere, but the ring-construction path deliberately opts out of validation. The `RingVerifierKey::decode` and `RingContext::decode` codec paths also use `_unchecked` deserialization: [4](#0-3) [5](#0-4) 

The pattern matches the external report's warning almost exactly: "If a curve with a cofactor other than one is ever used, several parts of the application will require reengineering to, for example, prevent small subgroup attacks by validating the order of public keys." Bandersnatch (a twisted-Edwards curve) has cofactor > 1, and the ring-construction path never performs that order/subgroup validation.

### Impact Explanation
The ring-verifier/ring-prover key is a chain-state artifact derived from validator/authority public keys that ultimately get included through session-key style registration flows (public keys are self-declared bytes, not the product of any cryptographic proof-of-possession over the *ring-membership* operation itself — proof-of-possession only covers the plain public key, not its subgroup order). If an authority key is accepted into storage without a subgroup check anywhere upstream of `make_ring_vector`, the pallet builds a ring commitment containing a non-subgroup point. Because Arkworks' checked deserialization is available and deliberately not used here, the security assumption baked into the anonymized ring-VRF construction (that every ring member is a valid group element of the intended prime-order subgroup) is not enforced at the one place responsible for enforcing it. Depending on how the underlying `ark-vrf`/KZG-based ring proof system handles non-subgroup commitments, this can degrade to: incorrect (all mismatched) ring-verifier keys causing legitimate ring-VRF proofs to fail chain-wide (denial of block-production/finality for Sassafras-style slot claims), or, in the worst case, malformed algebraic relations that a careful attacker could exploit against the specific pairing/KZG equations to bias or forge the anonymized proof — the exact "undefined behavior" the external report warns about when curve/point validation is skipped.

### Likelihood Explanation
The vulnerable function itself requires no privileged actor to trigger — `deserialize_compressed_unchecked` runs unconditionally on every entry of the `public_keys` slice every time a ring is rebuilt (each authority-set rotation). The remaining question is only whether an untrusted, non-subgroup byte string can ever reach `Authorities::<T>`/session-key storage before this function consumes it; that boundary (e.g. `pallet_session::set_keys` or a Sassafras-specific key-registration extrinsic) was not fully traceable within the available index, so it cannot be confirmed with certainty whether an unprivileged, not-yet-validator account can inject a crafted key. That specific chain-of-custody gap is the main uncertainty; the flaw in the ring-key-construction primitive itself (using the unchecked, non-subgroup-validated deserializer as the *only* gate before consuming external public-key bytes) is directly confirmed in the code.

### Recommendation
Replace `AffinePoint::deserialize_compressed_unchecked` in `RingContext::make_ring_vector` (and `RingVerifierKeyImpl`/`RingSetup` decode paths) with the checked `deserialize_compressed`, which performs on-curve and subgroup-membership validation, and explicitly reject (rather than silently substitute a padding point for) any authority key that fails validation — surfacing an error so the pallet can refuse to register/rotate to an invalid key instead of silently degrading the ring's cryptographic soundness.

### Proof of Concept
Conceptual PoC (cannot be fully executed without the `ark-vrf`/`ark_ed_on_bls12_381_bandersnatch` crate versions pinned in this repo, but the code path is deterministic):
1. Construct a byte string that Arkworks' compressed twisted-Edwards deserializer accepts as *on-curve* but that lies outside the prime-order subgroup (a small-order/cofactor point — the same `y = 2` type non-subgroup point already used in this repo's own `ed_on_bls12_381_bandersnatch.rs` tests, e.g. `TEAffine::get_point_from_y_unchecked(Fq::from(2u64), false)`).
2. Encode this point into the compressed byte layout expected by `sp_core::bandersnatch::Public` (32/33-byte compressed form) and place it into the `public_keys` list passed to `RingContext::verifier_key`/`RingContext::prover_key`.
3. Call `RingContext::make_ring_vector` on this list — `AffinePoint::deserialize_compressed_unchecked` will accept the non-subgroup point without error (unlike `deserialize_compressed`, which would reject it), and it is folded directly into the resulting `RingVerifierKey`/prover key used for all subsequent ring-VRF verification for that ring.
4. Compare against calling the equivalent construction with `deserialize_compressed` (checked) on the same bytes to show the checked variant returns an error while the unchecked one used in production code accepts the malformed point silently.

### Citations

**File:** substrate/primitives/core/src/bandersnatch.rs (L294-310)
```rust
	pub(super) fn vrf_verify(
		public: &Public,
		ios: &[VrfIo],
		proof: &Signature,
		aux_data: &[u8],
	) -> bool {
		use ark_vrf::thin::Verifier;
		let Ok(public) = bandersnatch::Public::deserialize_compressed(public.as_slice()) else {
			return false;
		};
		let Ok(proof) =
			ark_vrf::thin::Proof::<BandersnatchSuite>::deserialize_compressed(proof.as_slice())
		else {
			return false;
		};
		public.verify(ios, aux_data, &proof).is_ok()
	}
```

**File:** substrate/primitives/core/src/bandersnatch.rs (L397-405)
```rust
	impl Decode for RingVerifierKey {
		fn decode<R: codec::Input>(input: &mut R) -> Result<Self, codec::Error> {
			let mut buf = vec![0; RING_VERIFIER_KEY_SERIALIZED_SIZE];
			input.read(&mut buf[..])?;
			let vk = RingVerifierKeyImpl::deserialize_compressed_unchecked(buf.as_slice())
				.map_err(|_| "RingVerifierKey decode error")?;
			Ok(RingVerifierKey(vk))
		}
	}
```

**File:** substrate/primitives/core/src/bandersnatch.rs (L470-479)
```rust
		fn make_ring_vector(public_keys: &[Public]) -> Vec<bandersnatch::AffinePoint> {
			use bandersnatch::AffinePoint;
			public_keys
				.iter()
				.map(|pk| {
					AffinePoint::deserialize_compressed_unchecked(pk.as_slice())
						.unwrap_or(RingSetup::padding_point())
				})
				.collect()
		}
```

**File:** substrate/primitives/core/src/bandersnatch.rs (L492-500)
```rust
	impl<const R: usize> Decode for RingContext<R> {
		fn decode<I: codec::Input>(input: &mut I) -> Result<Self, codec::Error> {
			let mut buf = vec![0; ring_context_serialized_size(R)];
			input.read(&mut buf[..])?;
			let ctx = RingSetup::deserialize_uncompressed_unchecked(buf.as_slice())
				.map_err(|_| "RingContext decode error")?;
			Ok(RingContext(ctx))
		}
	}
```

**File:** substrate/frame/sassafras/src/lib.rs (L552-565)
```rust
	pub(crate) fn update_ring_verifier(authorities: &[AuthorityId]) {
		debug!(target: LOG_TARGET, "Loading ring context");
		let Some(ring_ctx) = RingContext::<T>::get() else {
			debug!(target: LOG_TARGET, "Ring context not initialized");
			return;
		};

		let pks: Vec<_> = authorities.iter().map(|auth| *auth.as_ref()).collect();

		debug!(target: LOG_TARGET, "Building ring verifier (ring size: {})", pks.len());
		let verifier_data = ring_ctx.verifier_key(&pks);

		RingVerifierData::<T>::put(verifier_data);
	}
```
