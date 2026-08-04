### Title
Ratio-based back-conversion of Ethereum-gas budget into weight/deposit limits in `pallet-revive`'s nested-frame metering can starve legitimate nested calls of weight - ([File: substrate/frame/revive/src/metering/math.rs])

### Summary
`pallet-revive`'s `substrate_execution::new_nested_meter` computes a nested contract call's `weight_limit`/`deposit_limit` by (1) converting the parent's remaining weight and remaining deposit into an Ethereum-gas *estimate* via `FeeInfo::weight_to_fee_average` / `to_adjusted_deposit_charge`, (2) capping that combined estimate against the caller-requested `gas`, and (3) splitting the resulting `gas_limit` back into weight and deposit using a single scalar `ratio`. This mirrors the GoodEntry `addDust` flaw: a value needed by an *actual* consumption model (weight metering / storage metering) is derived from a *different*, lossy pricing model (a linear ethereum-gas exchange rate), and the two models diverge when one component of the estimate dominates.

### Finding Description
In `substrate/frame/revive/src/metering/math.rs::substrate_execution::new_nested_meter` (`CallResources::Ethereum` branch, lines 111-163):

```rust
let weight_gas_left = SignedGas::<T>::from_weight_fee(T::FeeInfo::weight_to_fee_average(&weight_left));
let deposit_gas_left = SignedGas::<T>::from_adjusted_deposit_charge(&StorageDeposit::Charge(deposit_left));
let remaining_gas = (weight_gas_left.saturating_add(&deposit_gas_left)).to_ethereum_gas()...;
let remaining_gas = remaining_gas.min(u64::MAX.saturated_into());
let gas_limit = remaining_gas.min(*gas);
let ratio = FixedU128::from_rational(gas_limit, remaining_gas);
let weight_limit = Weight::from_parts(
    ratio.saturating_mul_int(weight_left.ref_time()),
    ratio.saturating_mul_int(weight_left.proof_size()),
);
let deposit_limit = ratio.saturating_mul_int(deposit_left);
``` [1](#0-0) 

The corrupted value is `ratio`: it is derived from the *combined* gas-equivalent of weight and deposit, then applied uniformly to both weight and deposit. When `deposit_left` is disproportionately large relative to `weight_left` (a perfectly legitimate configuration — a caller with a generous `deposit_limit` but a modest `weight_limit`), the deposit term dominates `remaining_gas`, so `ratio = gas_limit / remaining_gas` collapses toward the fraction of gas the deposit represents, and `ratio.saturating_mul_int(weight_left.ref_time())` produces a `weight_limit` far below the weight the nested call actually needs — even though the parent still has abundant weight budget. The nested `WeightMeter` then rejects legitimate weight charges with `Error::OutOfGas`, exactly like `getTokenAmountsExcludingFees` reverting because the tick-based consumption model diverged from the oracle-based dust estimate.

`paritytech/polkadot-sdk` PR #10924 ("revive: cap remaining_gas to u64::MAX in substrate_execution") already confirms this exact failure mode occurred with `deposit_left = u128::MAX`, causing `ratio ≈ 0` and nested calls immediately failing with `OutOfGas`: [2](#0-1) 

The applied fix only caps `remaining_gas` at `u64::MAX` — it does not fix the underlying single-ratio proportional split. Any deposit_left value that is large-but-below-the-cap (a normal, non-degenerate value for contracts with sizeable storage budgets) still skews `ratio` away from the weight side, because the split assumes weight and deposit convert to gas at rates that make a uniform ratio meaningful, which is not guaranteed once one term dominates the sum, exactly as the GoodEntry dust calculation assumed spot-price ⇔ tick-price equivalence that didn't hold at real tick ranges.

### Impact Explanation
A legitimate, unprivileged contract caller (any EOA/contract issuing an `eth_call`/`eth_transact` with a `CALL` requesting Ethereum-style gas to a sub-contract) can have their nested call spuriously reverted with `OutOfGas` even though both weight and deposit budgets at the transaction level are sufficient. This degrades correctness of contract execution on Asset-Hub-style runtimes that enable `pallet-revive`, causing unnecessary transaction failures / wasted fees (a form of "public underpriced work" causing wasted block space and stalled contract execution flows), and can be weaponized to make composable multi-call contracts (e.g., proxies, routers) unreliable or force griefing reverts against specific counter-parties by tuning the storage-vs-weight ratio of a call.

### Likelihood Explanation
This triggers under normal usage whenever a transaction's remaining `deposit_left` gas-equivalent is much larger than its remaining `weight_left` gas-equivalent (e.g., contracts pre-funded with a generous deposit limit but modest weight budget, or after multiple nested frames have already consumed weight while deposit remains largely unconsumed). No malicious peer, validator, or governance action is required — only the caller structuring an ordinary transaction and nested `CALL` with an Ethereum-style gas request. The exact `u128::MAX` case was already observed and fixed in production-adjacent testing (contract-issues#256), demonstrating the pattern is realistically reachable, not merely theoretical.

### Recommendation
Do not back-convert a single combined gas estimate into independent weight and deposit limits via one scalar ratio. Instead, bound weight and deposit against the requested gas by verifying that both dimensions individually satisfy the requested budget (e.g., cap `nested_weight_limit` to `weight_left` unless it is confirmed sufficient to cover the *actual* weight required to spend the requested gas, and only apply proportional scaling within bounds that are provably safe, such as when neither term dominates by more than a fixed factor), or track the two resources independently rather than forcing a single linear exchange rate across heterogeneous units. This mirrors GoodEntry's fix of abandoning derived "dust" estimates in favor of checking actual required amounts directly (`depositExactly`).

### Proof of Concept
Conceptual PoC (mirrors the pattern already validated by the `pallet-revive` test suite and PR #10924's reproduction):
1. Configure a root `TransactionMeter` with `weight_limit` set to a modest amount (e.g. representative of a typical extrinsic weight budget) and `deposit_limit` set very large (e.g., `u128::MAX / 2`, deliberately below the `u64::MAX` gas cap so the PR #10924 fix does not engage).
2. Invoke a contract that performs a nested `CALL` (`CallResources::Ethereum { gas, add_stipend }`) requesting a `gas` amount that would legitimately be satisfiable from `weight_left` alone.
3. Observe `new_nested_meter` compute `ratio` skewed toward the deposit term (since `deposit_gas_left ≫ weight_gas_left`), producing `nested_weight_limit ≪ weight_left`.
4. The nested call fails with `Error::<T>::OutOfGas` even though `weight_left` was ample — reproducing the same "should have plenty of the required resource but computed proxy value causes revert" behavior as the GoodEntry `addDust`/`getTokenAmountsExcludingFees` PoC.

This can be adapted directly from the existing test harness in `substrate/frame/revive/src/tests/pvm.rs::gas_estimation_for_subcalls` and `substrate/frame/revive/src/metering/tests.rs`, by substituting a `TransactionLimits::EthereumGas`/`CallResources::Ethereum` scenario with a skewed weight-vs-deposit ratio instead of the currently-tested u128::MAX edge case. [3](#0-2) [4](#0-3)

### Citations

**File:** substrate/frame/revive/src/metering/math.rs (L111-147)
```rust
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

**File:** substrate/frame/revive/src/tests/pvm.rs (L1926-2000)
```rust
#[test]
fn gas_estimation_for_subcalls() {
	let (caller_code, _caller_hash) = compile_module("call_with_limit").unwrap();
	let (dummy_code, _callee_hash) = compile_module("dummy").unwrap();
	ExtBuilder::default().existential_deposit(50).build().execute_with(|| {
		let min_balance = Contracts::min_balance();
		let _ = <Test as Config>::Currency::set_balance(&ALICE, 2_000 * min_balance);

		let Contract { addr: addr_caller, .. } =
			builder::bare_instantiate(Code::Upload(caller_code))
				.native_value(min_balance * 100)
				.build_and_unwrap_contract();

		let Contract { addr: addr_dummy, .. } = builder::bare_instantiate(Code::Upload(dummy_code))
			.native_value(min_balance * 100)
			.build_and_unwrap_contract();

		// Run the test for all of those weight limits for the subcall
		let weights = [
			Weight::MAX,
			WEIGHT_LIMIT,
			WEIGHT_LIMIT * 2,
			WEIGHT_LIMIT / 5,
			Weight::from_parts(u64::MAX, WEIGHT_LIMIT.proof_size()),
			Weight::from_parts(WEIGHT_LIMIT.ref_time(), u64::MAX),
		];

		let (sub_addr, sub_input) = (addr_dummy.as_ref(), vec![]);

		for weight in weights {
			let input: Vec<u8> = sub_addr
				.iter()
				.cloned()
				.chain(weight.ref_time().to_le_bytes())
				.chain(weight.proof_size().to_le_bytes())
				.chain(sub_input.clone())
				.collect();

			// Call in order to determine the gas that is required for this call
			let result_orig = builder::bare_call(addr_caller).data(input.clone()).build();
			assert_ok!(&result_orig.result);
			assert_eq!(result_orig.weight_required, result_orig.weight_consumed);

			// Make the same call using the estimated gas. Should succeed.
			let result = builder::bare_call(addr_caller)
				.transaction_limits(TransactionLimits::WeightAndDeposit {
					weight_limit: result_orig.weight_required,
					deposit_limit: result_orig.storage_deposit.charge_or_zero().into(),
				})
				.data(input.clone())
				.build();
			assert_ok!(&result.result);

			// Check that it fails with too little ref_time
			let result = builder::bare_call(addr_caller)
				.transaction_limits(TransactionLimits::WeightAndDeposit {
					weight_limit: result_orig.weight_required.sub_ref_time(1),
					deposit_limit: result_orig.storage_deposit.charge_or_zero().into(),
				})
				.data(input.clone())
				.build();
			assert_err!(result.result, <Error<Test>>::OutOfGas);

			// Check that it fails with too little proof_size
			let result = builder::bare_call(addr_caller)
				.transaction_limits(TransactionLimits::WeightAndDeposit {
					weight_limit: result_orig.weight_required.sub_proof_size(1),
					deposit_limit: result_orig.storage_deposit.charge_or_zero().into(),
				})
				.data(input.clone())
				.build();
			assert_err!(result.result, <Error<Test>>::OutOfGas);
		}
	});
}
```

**File:** substrate/frame/revive/src/metering/tests.rs (L297-361)
```rust
/// A dry-run from an unfunded account should still report the `max_storage_deposit`
/// that a successful run would need, so that the caller can size the allowance
/// required to cover the storage deposit before submitting the real transaction.
#[test_case(FixtureType::Solc   , "DepositPrecompile" ; "solc precompiles")]
#[test_case(FixtureType::Resolc , "DepositPrecompile" ; "resolc precompiles")]
#[test_case(FixtureType::Solc   , "DepositDirect" ; "solc direct")]
#[test_case(FixtureType::Resolc , "DepositDirect" ; "resolc direct")]
fn max_storage_deposit_reported_for_unfunded_dry_run(
	fixture_type: FixtureType,
	fixture_name: &str,
) {
	let (code, _) = compile_module_with_type(fixture_name, fixture_type).unwrap();

	ExtBuilder::default().build().execute_with(|| {
		let _ = <Test as Config>::Currency::set_balance(&ALICE, 100_000_000_000);

		let Contract { addr: caller_addr, .. } =
			builder::bare_instantiate(Code::Upload(code)).build_and_unwrap_contract();

		// Wrap each call in a rolled-back storage layer so state doesn't leak
		// between them. Mirrors how a runtime API dispatches the dry-run.
		let run_in_rollback = |build: &dyn Fn() -> _| {
			with_transaction(|| {
				TransactionOutcome::Rollback(Ok::<_, sp_runtime::DispatchError>(build()))
			})
			.unwrap()
		};

		// Reference run from a funded account.
		let funded = run_in_rollback(&|| {
			builder::bare_call(caller_addr)
				.data(DepositPrecompile::setAndClearCall {}.abi_encode())
				.build()
		});
		assert!(funded.result.is_ok(), "reference run must succeed, got {:?}", funded.result);
		assert!(
			funded.max_storage_deposit.charge_or_zero() > 0,
			"expected the funded reference run to require some storage deposit, got {:?}",
			funded.max_storage_deposit,
		);

		// Same call from CHARLIE, who has no balance, using the runtime-api dry-run
		// `ExecConfig`. Collecting the deposit fails because CHARLIE cannot fund it, but
		// the reported `max_storage_deposit` must still match the funded run so the
		// caller can size the allowance needed to cover the deposit.
		let unfunded = run_in_rollback(&|| {
			crate::Pallet::<Test>::prepare_dry_run(&CHARLIE);
			builder::bare_call(caller_addr)
				.origin(RawOrigin::Signed(CHARLIE).into())
				.data(DepositPrecompile::setAndClearCall {}.abi_encode())
				.transaction_limits(TransactionLimits::WeightAndDeposit {
					weight_limit: <Test as frame_system::Config>::BlockWeights::get().max_block,
					deposit_limit: u128::MAX,
				})
				.exec_config(ExecConfig::new_substrate_tx().with_dry_run(None))
				.build()
		});

		assert_eq!(
			unfunded.result.unwrap_err(),
			Error::<Test>::StorageDepositNotEnoughFunds.into()
		);
		assert_eq!(unfunded.max_storage_deposit, funded.max_storage_deposit);
	});
}
```
