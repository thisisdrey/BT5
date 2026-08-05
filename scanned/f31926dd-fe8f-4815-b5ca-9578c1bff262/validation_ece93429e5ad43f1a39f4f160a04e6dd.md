Based on my investigation, I found a concrete local analog to the AmbireAccount `tryCatch()` gas-griefing bug inside `pallet-revive`'s resource metering for nested Ethereum-style contract calls.

### Title
Ratio-based weight/deposit split in `substrate_execution::new_nested_meter` lets a caller contract force an "allowed-to-fail" nested call to run out of gas while the outer call still succeeds - ([File: substrate/frame/revive/src/metering/math.rs])

### Summary
`pallet-revive` emulates EVM `CALL` gas semantics on top of Substrate's two-dimensional weight (`ref_time`/`proof_size`) plus storage-deposit resource model. When a contract issues a nested call with `CallResources::Ethereum { gas, .. }` while running under "Substrate execution mode" (weight+deposit limits rather than a pure Ethereum gas limit), `new_nested_meter` converts the caller's remaining weight and remaining deposit into a single scalar "gas" value, then distributes the requested `gas` back into concrete `weight_limit`/`deposit_limit` for the nested frame using **one shared ratio** applied to both resources independently of their true consumption profile.

### Finding Description [1](#0-0) 

```
CallResources::Ethereum { gas, add_stipend } => {
    let weight_gas_left = SignedGas::<T>::from_weight_fee(...weight_left...);
    let deposit_gas_left = SignedGas::<T>::from_adjusted_deposit_charge(...deposit_left...);
    let remaining_gas = (weight_gas_left + deposit_gas_left)...;
    let gas_limit = remaining_gas.min(*gas);
    let ratio = gas_limit / remaining_gas;             // single scalar
    let weight_limit = ratio * weight_left;            // applied to weight
    let deposit_limit = ratio * deposit_left;          // applied to deposit
    ...
}
```

`weight_left` and `deposit_left` are two *independent* resources (compute vs. storage-rent), yet they are summed into one linear "gas" quantity and then split back with the *same* ratio. A calling contract fully controls its own frame's `weight_left`/`deposit_left` proportions (by pre-consuming weight or deposit before issuing the nested call, or by choosing a deposit limit at instantiation time) and also controls the exact `gas` value requested for the nested call. By skewing the ratio between `weight_gas_left` and `deposit_gas_left` (e.g. leaving abundant deposit headroom but very little weight headroom), the caller can make the nested frame's derived `weight_limit` collapse toward zero even though the nominal combined "gas" appears sufficient - causing the sub-call to fail with `Error::OutOfGas` [2](#0-1)  purely due to the accounting/ratio artifact, not due to genuine lack of resources.

This is the same broken invariant as the AmbireAccount `tryCatch()` bug: a resource-forwarding rule (there, EIP-150's 63/64ths; here, a mis-derived weight/deposit ratio) lets an attacker precisely engineer the *sub*-call's failure while the *calling* contract - which decides whether to treat the sub-call's failure as fatal - retains enough of its own weight/deposit budget to continue and successfully finish execution (e.g., emit an "operation failed" log and return success, exactly like `AmbireAccount.tryCatch`). Parity's own regression test and fix in `PR-10924` acknowledge this failure mode [3](#0-2) , but only patch the degenerate case where `deposit_left` equals the `u128::MAX` "unlimited deposit" sentinel by capping `remaining_gas` to `u64::MAX`. The underlying single-ratio proportional split for ordinary (non-sentinel, finite) `weight_left`/`deposit_left` combinations remains unfixed in `new_nested_meter` [4](#0-3) , so the general griefing pattern (forcing an under-forward of weight to a nested call relative to what the call actually needs, while the outer call remains funded) is still reachable by any contract deployer choosing adversarial resource ratios for their own contract - fully within their own account, requiring no privileged role, relayer, or admin.

### Impact Explanation
Any protocol or wrapper contract deployed on `pallet-revive` that implements a "call and tolerate sub-call failure" pattern (the same design pattern the report's `tryCatch()` exemplifies, and one `pallet-revive`'s own EVM-compat CALL opcode encourages developers to replicate) inherits an *engine-level* resource-accounting flaw, not merely an application-level EVM quirk. Because the ratio distortion is deterministic and fully computable off-chain from public state (the caller's own weight/deposit consumption and the deposit limit it chooses), an attacker who is also the contract's deployer/caller can reliably manufacture "false negative" nested-call failures (state supposed to be updated is skipped) while the outer transaction still commits, is billed as successful, and advances nonces/fees - i.e., the sub-call's success/failure signal becomes untrustworthy for any logic gating fund transfers, settlement, or payout decisions on it.

### Likelihood Explanation
Exploiting this requires only: (1) deploying/calling a contract through the standard `call`/`instantiate` extrinsics under weight+deposit limits (the default, non-`eth_transact` mode), and (2) choosing a nested-call `gas` request together with a deliberately skewed weight/deposit balance in the calling frame. No relayer, validator, governance, or leaked-key assumption is needed - the "attacker" is simply the unprivileged deployer of their own contract, matching the required "public entrypoint, unprivileged attacker" profile.

### Recommendation
Do not linearly conflate `weight_left` and `deposit_left` into a single scalar "gas" and redistribute with one shared ratio. Instead, independently cap the nested frame's `weight_limit` and `deposit_limit` against the requested `gas`'s weight-equivalent and deposit-equivalent components separately (bounding each resource by its own remaining supply), so that a large surplus in one resource cannot be used to mask/produce an artificial deficit in the other. Additionally, extend the `PR-10924` cap-to-`u64::MAX` fix into a general bound applied per-resource rather than only for the `u128::MAX` sentinel case.

### Proof of Concept
1. Deploy contract `A` under `TransactionLimits::WeightAndDeposit` with a large `deposit_limit` (finite but large, e.g., near `u64::MAX`-equivalent gas) and a moderate `weight_limit`.
2. In `A`'s constructor/entry, consume weight down to a small remainder (e.g., via a loop) while leaving deposit largely unconsumed, driving `weight_left` low and `deposit_left` high.
3. Have `A` call victim contract `B` via `CallResources::Ethereum { gas: G }` where `G` is chosen (computable from public `weight_left`/`deposit_left`) such that the resulting `ratio = G / remaining_gas` applied to `weight_left` yields a `weight_limit` too small for `B` to execute its intended logic (e.g., a token transfer requiring more `ref_time`/`proof_size` than the derived limit), forcing `B` to fail with `Error::OutOfGas` as computed in `new_nested_meter` (`substrate/frame/revive/src/metering/math.rs:107-163`).
4. `A` observes `B`'s call failure (as a normal `Err` return from `self.ext.call(...)`), does not propagate it, and completes successfully, returning `Ok` for the whole transaction - reproducing the "outer succeeds, sub-call forced to fail via resource-forwarding math" primitive from the AmbireAccount report, entirely from unprivileged contract code and caller-controlled parameters.

### Citations

**File:** substrate/frame/revive/src/metering/math.rs (L84-173)
```rust
	) -> Result<FrameMeter<T>, DispatchError> {
		let self_consumed_weight = meter.weight.weight_consumed();
		let self_consumed_deposit = meter.deposit.consumed();

		let total_consumed_weight =
			meter.total_consumed_weight_before.saturating_add(self_consumed_weight);
		let total_consumed_deposit =
			meter.total_consumed_deposit_before.saturating_add(&self_consumed_deposit);

		let weight_left = meter
			.weight
			.weight_limit
			.checked_sub(&self_consumed_weight)
			.ok_or(<Error<T>>::OutOfGas)?;

		let deposit_limit = meter.deposit.limit.expect(
			"Deposit limits are always defined for `ResourceMeter` in Substrate \
				execution mode (i.e., when its `transaction_limits` are `WeightAndDeposit`); qed",
		);
		let deposit_left = self_consumed_deposit
			.available(&deposit_limit)
			.ok_or(<Error<T>>::StorageDepositLimitExhausted)?;

		let (nested_weight_limit, nested_deposit_limit, stipend) = {
			match limit {
				CallResources::NoLimits => (weight_left, deposit_left, None),

				CallResources::Ethereum { gas, add_stipend } => {
					// Convert leftover weight and deposit to an ethereum-gas equivalent,
					// then cap that gas by the requested `gas`. Distribute the capped gas
					// back into weight and deposit portions using the same ratio so that
					// the nested frame receives proportional limits.
					let weight_gas_left = SignedGas::<T>::from_weight_fee(
						T::FeeInfo::weight_to_fee_average(&weight_left),
					);
					let deposit_gas_left = SignedGas::<T>::from_adjusted_deposit_charge(
						&StorageDeposit::Charge(deposit_left),
					);
					let Some(remaining_gas) =
						(weight_gas_left.saturating_add(&deposit_gas_left)).to_ethereum_gas()
					else {
						return Err(<Error<T>>::OutOfGas.into());
					};

					// Cap to u64::MAX since Ethereum gas is u64. Without this, large deposit_left
					// (e.g., u128::MAX) causes ratio ≈ 0, giving nested calls almost no weight.
					let remaining_gas = remaining_gas.min(u64::MAX.saturated_into());

					let gas_limit = remaining_gas.min(*gas);

					let ratio = if remaining_gas.is_zero() {
						FixedU128::one()
					} else {
						FixedU128::from_rational(
							gas_limit.saturated_into(),
							remaining_gas.saturated_into(),
						)
					};

					let mut weight_limit = Weight::from_parts(
						ratio.saturating_mul_int(weight_left.ref_time()),
						ratio.saturating_mul_int(weight_left.proof_size()),
					);
					let deposit_limit = ratio.saturating_mul_int(deposit_left);

					let stipend = if *add_stipend {
						let weight_stipend = determine_call_stipend::<T>();
						if weight_left.any_lt(weight_stipend) {
							return Err(<Error<T>>::OutOfGas.into());
						}

						weight_limit.saturating_accrue(weight_stipend);

						Some(weight_stipend)
					} else {
						None
					};

					(weight_left.min(weight_limit), deposit_left.min(deposit_limit), stipend)
				},

				CallResources::WeightDeposit { weight, deposit_limit } =>
				// when explicit weight+deposit requested, take the minimum of parent's left
				// and the requested per-call limits.
				{
					(weight_left.min(*weight), deposit_left.min(*deposit_limit), None)
				},
			}
		};

```

**File:** prdoc/stable2603/pr_10924.prdoc (L13-30)
```text
    ## Problem

    When calculating resource limits for nested calls through
    `substrate_execution::new_nested_meter`, the ratio-based scaling fails when
    `deposit_left` is very large (e.g., `u128::MAX` default for unlimited deposit).

    The calculation flow:
    1. `remaining_gas = weight_gas + deposit_gas` → huge number (deposit dominates at ~10^38)
    2. Contract requests all gas: `requested_gas = u64::MAX` (~10^19)
    3. `ratio = requested_gas / remaining_gas` ≈ 0.0000000000000027
    4. `nested_weight_limit = ratio × weight_left` ≈ 0
    5. Nested call immediately fails with OutOfGas

    ## Solution

    Cap `remaining_gas` to `u64::MAX` since Ethereum gas is a u64 value. This ensures
    the ratio is 1.0 when a contract requests all gas, giving the nested call the full
    remaining weight.
```
