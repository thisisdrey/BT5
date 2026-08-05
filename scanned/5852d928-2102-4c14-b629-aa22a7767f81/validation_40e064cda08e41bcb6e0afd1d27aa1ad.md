### Title
`eth_estimate_gas` dry-run success is not reproducible on actual dispatch for near-block-limit transactions - ([File: substrate/frame/revive/src/evm/call.rs])

### Summary
`pallet-revive`'s `into_call()` computes the weight limit assigned to an Ethereum transaction differently depending on whether the transaction is a dry run (used by `eth_estimateGas`/`eth_call`) or an actual extrinsic submission. For real submissions the code subtracts an `overweight_by` correction derived from `evm_max_extrinsic_weight()`, but for dry runs this correction is skipped entirely. This mirrors exactly the class of bug in the external report: work that "succeeds" when measured/estimated in one context can become impossible to reproduce when executed in the real context that has additional overhead deducted from the same nominal budget.

### Finding Description
In `GenericTransaction::into_call` [1](#0-0) , the `weight_limit` assigned to the dispatchable (`eth_call` / `eth_instantiate_with_code`) is first computed from the fee the user is willing to pay (`remaining_fee_weight - info.total_weight()`), and then:

- If `is_dry_run` is `false` (real extrinsic execution), the code additionally computes `overweight_by = info.total_weight().saturating_sub(evm_max_extrinsic_weight())` and subtracts it from `weight_limit`, capping the assigned weight.
- If `is_dry_run` is `true` (used by `eth_estimate_gas`/`eth_call`), this correction is skipped and the raw `weight_limit` is used unchanged.

`eth_estimate_gas` performs a binary search entirely through the `is_dry_run = true` path [2](#0-1) , calling `dry_run_eth_transact` repeatedly and returning the smallest `gas` value for which the dry run succeeds. Because the dry-run path never applies the `overweight_by` capping that the real-dispatch path applies, `eth_estimate_gas` can return (and previously enforced no such cap at all until PR `pr_10902` limited the *unbounded* case, but the *asymmetry between dry-run and real dispatch weight capping* documented above still exists in `into_call`) a gas estimate for which `info.total_weight()` is at or very near `evm_max_extrinsic_weight()` (the maximum weight budget for an Ethereum extrinsic). When the user then submits the real transaction with that estimated gas, `into_call` is invoked with `is_dry_run = false`; the extra `overweight_by` subtraction now reduces the `weight_limit` handed to the contract-execution meter, which can push previously-successful execution (e.g. a call chain that exhausts most of its weight budget on unbounded-cost precompiles such as `Bn128Pairing`, whose weight scales linearly and steeply with the number of pairing elements, `Weight::from_parts(4_968_864_584, 0).saturating_add(Weight::from_parts(6_023_077_711,0).saturating_mul(n))` [3](#0-2) ) into an out-of-weight failure that never happened during estimation.

This is the direct structural analog of the original report: an operation (an L2 precompile call / here, a contract call whose cost is dominated by unbounded-cost precompiles) is validated as executable in a "generous" measurement context (L1 dispute step / here, `eth_estimate_gas` dry run) but the real execution context (fault-proof `step()` / here, actual on-chain dispatch) silently subtracts additional overhead from the same nominal resource pool, making the previously-successful operation irreproducible.

### Impact Explanation
A user who follows the standard Ethereum tooling workflow (call `eth_estimateGas`, then submit a transaction using the returned gas) can have their transaction unexpectedly run out of weight and revert/fail on-chain, even though the identical call succeeded during estimation. For contracts that are close to the `evm_max_extrinsic_weight()` boundary — which is exactly the situation an attacker can engineer by driving up costs with unbounded precompiles like `Bn128Pairing`/`Modexp` — this degrades reliable execution of "public underpriced work" near the block/extrinsic weight boundary and can be used to grief callers or force wasted transaction fees, since the fee is charged for the extrinsic base + length even when the dispatched call runs out of assigned weight and fails. It does not (from what could be verified here) allow theft of funds or unauthorized origin escalation, but it violates the invariant that gas estimation should return an amount sufficient for successful, reproducible execution.

### Likelihood Explanation
This is triggerable by any unprivileged EOA using the public `eth_estimateGas` / eth-transaction submission flow with a contract call whose declared dispatch weight sits close to `evm_max_extrinsic_weight()` — no malicious peer, validator, collator, relayer, or governance action is required. The precise magnitude of the discrepancy (`overweight_by`) depends on how close `info.total_weight()` is to `evm_max_extrinsic_weight()`, which is realistically reachable using unbounded-cost precompiles such as `Bn128Pairing` given its steep linear weight scaling shown in the benchmark output.

### Recommendation
Apply the same `overweight_by` capping logic in `into_call` for both the dry-run and real-dispatch code paths (or, alternatively, always compute and return the capped weight from `eth_estimate_gas`'s dry runs so subsequent binary-search iterations and the final estimate reflect the same weight ceiling that will be enforced during actual dispatch). This removes the asymmetry between the estimation context and the execution context so that a successful `eth_estimate_gas` result guarantees a successful on-chain execution with that gas.

### Proof of Concept
1. Deploy a contract whose `fallback`/`call` function invokes the `Bn128Pairing` precompile (address `0x8`) with a large multiple-of-192-byte input, chosen so that `info.total_weight()` for the resulting `eth_call`/`eth_instantiate_with_code` dispatchable sits just below `evm_max_extrinsic_weight()`.
2. Call `eth_estimateGas` for a transaction invoking this contract. Because `into_call` is invoked with `is_dry_run = true` in `dry_run_eth_transact`, no `overweight_by` correction is applied, and the binary search in `eth_estimate_gas` [4](#0-3)  converges to a `gas` value that succeeds under the uncapped weight.
3. Submit the actual transaction using the returned `gas` value. `into_call` is now invoked with `is_dry_run = false`, applying `overweight_by = info.total_weight().saturating_sub(evm_max_extrinsic_weight())` and reducing `weight_limit` [5](#0-4) .
4. Observe that the contract call, which succeeded during the `eth_estimate_gas` dry run, now runs out of weight and fails during real dispatch, despite the transaction having supplied the exact gas amount returned by estimation.

Note: full end-to-end confirmation would require running this scenario against a live `pallet-revive` runtime instance with `Bn128Pairing` benchmarked weights configured, which was not executed here; the analysis above is based on direct reading of the `into_call`, `eth_estimate_gas`, and `Bn128Pairing`/weight-benchmark source in this repository.

### Citations

**File:** substrate/frame/revive/src/evm/call.rs (L203-239)
```rust
		let weight_limit = {
			let fixed_fee = <T as Config>::FeeInfo::fixed_fee(encoded_len as u32);
			let info = <T as Config>::FeeInfo::dispatch_info(&call);

			let remaining_fee = {
				let adjusted = eth_fee.checked_sub(fixed_fee.into()).ok_or_else(|| {
				log::debug!(target: LOG_TARGET, "Not enough gas supplied to cover base and len fee. eth_fee={eth_fee:?} fixed_fee={fixed_fee:?}");
				InvalidTransaction::Payment
			})?;

				let unadjusted = compute_max_integer_quotient(
					<T as Config>::FeeInfo::next_fee_multiplier(),
					<BalanceOf<T>>::saturated_from(adjusted),
				);

				unadjusted
			};
			let remaining_fee_weight = <T as Config>::FeeInfo::fee_to_weight(remaining_fee);
			let weight_limit = remaining_fee_weight
			.checked_sub(&info.total_weight()).ok_or_else(|| {
			log::debug!(target: LOG_TARGET, "Not enough gas supplied to cover the weight ({:?}) of the extrinsic. remaining_fee_weight: {remaining_fee_weight:?}", info.total_weight(),);
			InvalidTransaction::Payment
		})?;

			call.set_weight_limit(weight_limit);

			if !is_dry_run {
				let max_weight = <Pallet<T>>::evm_max_extrinsic_weight();
				let info = <T as Config>::FeeInfo::dispatch_info(&call);
				let overweight_by = info.total_weight().saturating_sub(max_weight);
				let capped_weight = weight_limit.saturating_sub(overweight_by);
				call.set_weight_limit(capped_weight);
				capped_weight
			} else {
				weight_limit
			}
		};
```

**File:** substrate/frame/revive/src/lib.rs (L1946-2104)
```rust
	pub fn eth_estimate_gas(
		tx: GenericTransaction,
		timestamp_override: Option<MomentOf<T>>,
		state_overrides: Option<StateOverrideSet>,
	) -> Result<U256, EthTransactError>
	where
		T::Nonce: Into<U256> + TryFrom<U256>,
		CallOf<T>: SetWeightLimit,
	{
		log::debug!(target: LOG_TARGET, "eth_estimate_gas: {tx:?}");

		let mut low = U256::zero();
		let mut high = Self::evm_block_gas_limit();

		log::trace!(target: LOG_TARGET, "eth_estimate_gas starting with low={low}, high={high}");

		// If the user has specified a gas limit then this is the limit we use as the high bound for
		// the binary search. Also, if the user didn't specify a gas limit then we need to skip the
		// balance checks.
		let perform_balance_checks = if let Some(gas_limit) = tx.gas {
			high = gas_limit;
			log::trace!(target: LOG_TARGET, "eth_estimate_gas high limited by the gas limit high={high}");
			true
		} else {
			false
		};

		// Cap the high bound of the binary search based on the account's balance if it can be done.
		let fee_cap = tx.max_fee_per_gas.or(tx.gas_price);
		if let (Some(fee_cap), Some(from), true) = (fee_cap, tx.from, perform_balance_checks) {
			let mut available_balance = Self::evm_balance(&from);
			if let Some(value) = tx.value {
				available_balance = available_balance.checked_sub(value).ok_or_else(|| {
					EthTransactError::Message("insufficient funds for value transfer".into())
				})?;
			}
			if let Some(allowance) = available_balance.checked_div(fee_cap) {
				if high > allowance && allowance != U256::zero() {
					log::trace!(target: LOG_TARGET, "eth_estimate_gas high limited by the user's allowance high={high} allowance={allowance}");
					high = allowance
				}
			}
		}

		// Run one gas probe in a rolled-back transaction. Overrides are passed along so that
		// `dry_run_eth_transact` applies them *after* `prepare_dry_run` bumps the nonce, keeping a
		// nonce override at the exact value it sets.
		let dry_run_at = |gas: U256| {
			let mut transaction = tx.clone();
			transaction.gas = Some(gas);
			with_transaction(|| {
				TransactionOutcome::Rollback(Ok::<_, DispatchError>(Self::dry_run_eth_transact(
					transaction,
					timestamp_override,
					perform_balance_checks,
					state_overrides.clone(),
				)))
			})
			.expect("Rollback shouldn't error out")
		};

		// Classify against post-override state (a code override can make the destination a
		// contract) in a rolled-back probe, so the overrides don't leak into the dry runs.
		let is_simple_transfer = with_transaction(|| {
			let probe = state_overrides
				.clone()
				.map_or(Ok(()), state_overrides::apply_state_overrides::<T>)
				.map(|()| Self::is_simple_transfer(&tx));
			TransactionOutcome::Rollback(Ok::<_, DispatchError>(probe))
		})
		.expect("Rollback shouldn't error out")?;

		if is_simple_transfer {
			let dry_run_result = dry_run_at(high)?;
			log::trace!(
				target: LOG_TARGET,
				"eth_estimate_gas short-circuited simple transfer to {:?} with eth_gas={}",
				tx.to,
				dry_run_result.eth_gas,
			);
			return Ok(dry_run_result.eth_gas);
		}

		// Perform the first dry run with the gas limit of the binary search's high bound. If it
		// fails then we attempt again with the max extrinsic weight in gas which we do since some
		// transactions fail the dry run with the highest gas limit. If both of these fail then we
		// return early as it means that the transaction simply can't succeed.
		let dry_run_results = [high, Self::evm_max_extrinsic_weight_in_gas()]
			.map(|gas_limit| (gas_limit, dry_run_at(gas_limit)));
		let (gas_limit, first_dry_run_result) = match dry_run_results {
			[(gas_limit1, Ok(dry_run_result1)), (gas_limit2, Ok(dry_run_result2))] => {
				if dry_run_result2.eth_gas >= gas_limit2 {
					(gas_limit1, dry_run_result1)
				} else {
					(gas_limit2, dry_run_result2)
				}
			},
			[(gas_limit, Ok(dry_run_result)), (_, Err(_))] |
			[(_, Err(_)), (gas_limit, Ok(dry_run_result))] => (gas_limit, dry_run_result),
			[(_, Err(err)), (_, Err(..))] => return Err(err),
		};
		log::trace!(
			target: LOG_TARGET,
			"eth_estimate_gas first dry run succeeded with gas_limit={} consumed={}",
			gas_limit,
			first_dry_run_result.eth_gas
		);
		low = first_dry_run_result.eth_gas;
		high = gas_limit;

		while low + U256::one() < high {
			log::trace!(target: LOG_TARGET, "eth_estimate_gas estimation iteration with low={low} high={high}");
			let error_ratio = high
				.checked_sub(low)
				.and_then(|value| value.checked_mul(U256::from(1000)))
				.and_then(|value| value.checked_div(high))
				.ok_or_else(|| {
					EthTransactError::Message(
						"failed to calculate error ratio in gas estimation".into(),
					)
				})?;
			if error_ratio <= U256::from(15) {
				log::trace!(
					target: LOG_TARGET,
					"eth_estimate_gas finished due to error ratio being less than 1.5% high={}",
					high
				);
				break;
			}

			let mut midpoint = high
				.checked_sub(low)
				.and_then(|value| value.checked_div(U256::from(2)))
				.and_then(|value| value.checked_add(low))
				.ok_or_else(|| {
					EthTransactError::Message(
						"failed to calculate midpoint in gas estimation".into(),
					)
				})?;

			if let Some(other_midpoint) = low.checked_mul(U256::from(2)) {
				if other_midpoint != U256::zero() {
					midpoint = midpoint.min(other_midpoint)
				}
			};

			let dry_run_result = dry_run_at(midpoint);
			log::trace!(target: LOG_TARGET, "eth_estimate_gas dry run result with midpoint={midpoint} is dry_run_result={dry_run_result:?}");
			match dry_run_result {
				Ok(..) => {
					log::trace!(target: LOG_TARGET, "eth_estimate_gas dry run succeeded, new high={midpoint}");
					high = midpoint
				},
				Err(..) => {
					log::trace!(target: LOG_TARGET, "eth_estimate_gas dry run failed, new low={midpoint}");
					low = midpoint
				},
			}
		}
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/weights/pallet_revive.rs (L1499-1508)
```rust
	fn bn128_pairing(n: u32, ) -> Weight {
		// Proof Size summary in bytes:
		//  Measured:  `0`
		//  Estimated: `0`
		// Minimum execution time: 1_494_000 picoseconds.
		Weight::from_parts(4_968_864_584, 0)
			.saturating_add(Weight::from_parts(0, 0))
			// Standard Error: 10_525_515
			.saturating_add(Weight::from_parts(6_023_077_711, 0).saturating_mul(n.into()))
	}
```
