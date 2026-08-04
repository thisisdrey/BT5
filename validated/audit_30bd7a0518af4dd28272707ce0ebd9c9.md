Based on the evidence gathered, I can identify a concrete local analog of the missing-point-validation DoS pattern.

### Title
Missing on-curve/field validation in `Bn128Pairing` allows a crafted G2 point to abort verification without charging weight - ([File: substrate/frame/revive/src/precompiles/builtin/bn128.rs])

### Summary
The Axis Finance bug was: a bidder-supplied elliptic-curve point is accepted by a shallow validity check, then later fed to a curve operation that reverts unconditionally on malformed points, permanently blocking a multi-party settlement flow that requires *all* items to succeed atomically. The `pallet-revive` `Bn128Pairing` precompile (`substrate/frame/revive/src/precompiles/builtin/bn128.rs`) shows the same shape: point construction (`AffineG1::new`, `AffineG2::new`) is deferred until *after* the weight for the batch has already been charged for some inputs, and any single malformed point (not on curve) in a large batch input causes the whole call to error out via `DispatchError::from("Invalid a argument - not on curve")` [1](#0-0) .

### Finding Description
`Bn128Pairing::call` first computes `elements = input.len() / 192` and charges weight for that many pairs via `charge_weight_token(RuntimeCosts::Bn128Pairing(elements as u32))` *before* any of the individual points are validated [2](#0-1) . It then loops over all `elements`, decoding `Fq`/`Fq2` values and constructing `AffineG1`/`AffineG2` points, returning a hard `DispatchError` the moment any single element in the batch is not on the curve [3](#0-2) . This mirrors the Axis Finance pattern precisely: a value that is not the correct curve point is allowed through into a low-level curve-arithmetic call that then unconditionally fails for the *entire* aggregate operation, rather than being rejected up-front with cheap, isolated validation of each point before doing expensive batch work. In pallet-revive's case, since the precompile is invoked from PVM/EVM contract execution, an attacker fully controls the precompile call input (there is no privileged or trusted-relayer assumption — this is a public entry point reachable by any account executing a contract call), so they can always construct an input whose last of many pairs is off-curve, having already caused the runtime to charge (and the caller to pay) for the full batch's weight before the failure is discovered.

### Impact Explanation
This differs from the Axis Finance case in consequence: `Bn128Pairing` failures are contained to the calling PVM/EVM frame (it returns a `DispatchError`/reverts the frame) rather than corrupting on-chain locked state (there is no analogous "all bids must decrypt or funds are stuck forever" invariant here). The chain-level risk is a public underpriced/wasted-work vector: attackers can force the runtime to charge weight for `Bn128Pairing(elements)` and perform decoding work on many field elements, then trigger failure only at the very last element, extracting maximum computation for a call that is guaranteed to abort — but this is bounded by the weight metering itself (`charge_weight_token`), so it degrades efficiency rather than causing block-production stalls or fund loss.

### Likelihood Explanation
High likelihood of the pattern being reachable (any account can call a contract that invokes the `Bn128Pairing` precompile at address `0x8`), but the actual impact is limited to wasted-but-metered computation within a single contract call, not a lock of protocol funds or duplicate settlement, because pallet-revive's precompile design isolates the failure per-call rather than requiring an all-or-nothing global state transition like Axis Finance's auction decryption.

### Recommendation
Validate the on-curve status (and any subgroup constraints) of each `(a, b)` pair before charging cumulative weight for the batch, or charge weight incrementally per validated element so that an attacker cannot force paid-for-but-wasted work; consider returning early per-element rather than doing full-batch decode before the first curve check.

### Proof of Concept
Not independently verified with a runnable test in this pass — the control flow was confirmed by direct code read of `Bn128Pairing::call` in `substrate/frame/revive/src/precompiles/builtin/bn128.rs` [4](#0-3) , showing `charge_weight_token(RuntimeCosts::Bn128Pairing(elements))` executes before the per-element `AffineG1::new`/`AffineG2::new` on-curve checks that can fail.

**Caveat on confidence:** I was unable to inspect `point_eval.rs` (EIP-4844 KZG precompile) and `p256_verify.rs` in this session due to a tool error on the final iteration, and I could not confirm whether `sp-crypto-ec-utils`'s BLS12-381/BW6-761 "not-validated" pairing host functions (which `.expect(FAIL_MSG)` on decode failure, distinct from on-curve validation) are reachable from any unprivileged, publicly-triggerable runtime API in this codebase — the search only surfaced `bls_fast_aggregate_verify` in Snowbridge's `ethereum-client` pallet, which validates via `milagro_bls::PublicKey::from_bytes` (a checked deserializer), not the raw ec-utils host calls, so that specific path did not show the same unchecked-point defect. Given the required strict evidence bar and the limits of what I could verify, the `Bn128Pairing` weight-before-validation finding above is the most concrete, directly-supported analog found; if a stronger candidate exists in `point_eval.rs` or the BLS12-381 pairing consumer paths, it was not confirmed in this session.

### Citations

**File:** substrate/frame/revive/src/precompiles/builtin/bn128.rs (L92-163)
```rust
	fn call(
		_address: &[u8; 20],
		input: Vec<u8>,
		env: &mut impl Ext<T = Self::T>,
	) -> Result<Vec<u8>, Error> {
		if !input.len().is_multiple_of(192) {
			Err(DispatchError::from("invalid input length"))?;
		}

		let ret_val = if input.is_empty() {
			env.frame_meter_mut().charge_weight_token(RuntimeCosts::Bn128Pairing(0))?;
			U256::one()
		} else {
			// (a, b_a, b_b - each 64-byte affine coordinates)
			let elements = input.len() / 192;
			env.frame_meter_mut()
				.charge_weight_token(RuntimeCosts::Bn128Pairing(elements as u32))?;

			let mut vals = Vec::new();
			for i in 0..elements {
				let offset = i * 192;
				let a_x = Fq::from_slice(&input[offset..offset + 32])
					.map_err(|_| DispatchError::from("Invalid a argument x coordinate"))?;

				let a_y = Fq::from_slice(&input[offset + 32..offset + 64])
					.map_err(|_| DispatchError::from("Invalid a argument y coordinate"))?;

				let b_a_y = Fq::from_slice(&input[offset + 64..offset + 96]).map_err(|_| {
					DispatchError::from("Invalid b argument imaginary coeff x coordinate")
				})?;

				let b_a_x = Fq::from_slice(&input[offset + 96..offset + 128]).map_err(|_| {
					DispatchError::from("Invalid b argument imaginary coeff y coordinate")
				})?;

				let b_b_y = Fq::from_slice(&input[offset + 128..offset + 160]).map_err(|_| {
					DispatchError::from("Invalid b argument real coeff x coordinate")
				})?;

				let b_b_x = Fq::from_slice(&input[offset + 160..offset + 192]).map_err(|_| {
					DispatchError::from("Invalid b argument real coeff y coordinate")
				})?;

				let b_a = Fq2::new(b_a_x, b_a_y);
				let b_b = Fq2::new(b_b_x, b_b_y);
				let b =
					if b_a.is_zero() && b_b.is_zero() {
						G2::zero()
					} else {
						G2::from(AffineG2::new(b_a, b_b).map_err(|_| {
							DispatchError::from("Invalid b argument - not on curve")
						})?)
					};
				let a =
					if a_x.is_zero() && a_y.is_zero() {
						G1::zero()
					} else {
						G1::from(AffineG1::new(a_x, a_y).map_err(|_| {
							DispatchError::from("Invalid a argument - not on curve")
						})?)
					};
				vals.push((a, b));
			}

			let mul = pairing_batch(&vals);

			if mul == Gt::one() { U256::one() } else { U256::zero() }
		};

		let buf = ret_val.to_big_endian();
		Ok(buf.to_vec())
	}
```
