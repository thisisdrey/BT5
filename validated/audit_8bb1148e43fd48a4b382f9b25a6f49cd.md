### Title
Calls from `pallet-revive` contracts to token addresses without deployed code silently succeed as no-op balance transfers, enabling an ERC-20-honeypot-style fund theft - (File: `substrate/frame/revive/src/exec.rs`)

### Summary
`pallet-revive`'s call-dispatch deliberately treats any call to an address that has no contract code as a plain native-balance transfer that returns success with empty output, mirroring EVM EOA-transfer semantics [1](#0-0) . This is the same broken invariant as the Solmate `SafeTransferLib` honeypot in the external report: a "token transfer" call to a not-yet-deployed contract address does not fail, it silently succeeds without moving any ERC-20 balance, while the caller's higher-level logic (e.g. an escrow/auction contract) believes the transfer happened because `success == true` was returned.

### Finding Description
`Stack::new` (and the underlying `Stack::run_call` entry point) returns `Ok(None)` — not an error — whenever the destination account has no associated contract code, explicitly documenting that "this will result in a value transfer" [2](#0-1) . In `run_call`, when `Stack::new` yields `None`, the code falls straight into `Self::transfer_from_origin(...)`, which moves the *native* balance and unconditionally returns `Ok(...)` — it never even attempts to interpret the call as an ERC-20 `transfer`/`transferFrom` invocation, and it discards the ABI-encoded `input_data` entirely [3](#0-2) . This behavior was intentionally introduced by pr_5664 ("Calling an address without associated code is a balance transfer") and reinforced by pr_6741, which explicitly removed the previous `NotCallable` error because "it will just be a balance transfer" [4](#0-3) [5](#0-4) .

The unit test `recursive_call_during_constructor_is_balance_transfer` confirms this precisely: a call to an address with no contract deployed succeeds even when arbitrary call data (`vec![1, 2, 3, 4]`) is supplied, because "call data set... is ignored when no contract is deployed" [6](#0-5) . The same fallback applies to `delegate_call`, where calls to non-contract accounts are unconditionally "considered success" [7](#0-6) .

Contrast this with the XCM ERC-20 withdrawal path, which correctly guards against the exact honeypot primitive: `PolkadotXcm::execute` withdrawing from a non-existent ERC-20 contract address properly errors out rather than silently succeeding [8](#0-7) . No equivalent guard exists at the `pallet-revive` call layer itself — a Solidity contract on Revive that does `bool ok = token.call(abi.encodeWithSignature("transferFrom(address,address,uint256)", ...))` and checks only `ok` (the common Solmate-style low-level call pattern from the original report) will get `ok == true` and empty returndata for any `token` address with no code, exactly like Solmate's `SafeTransferLib`.

### Impact Explanation
Any Solidity/PVM contract deployed via `pallet-revive` that uses low-level `.call()` (rather than a full ABI decode-and-validate pattern) to interact with an ERC-20-like token address inherits the honeypot primitive from the external report. An attacker can:
1. Reference a token address that has not yet been deployed as the "quote"/"bid" token in an escrow, auction, or vault contract.
2. Have a victim call the deposit/bid function, pulling "tokens" via a low-level call to the undeployed address — the call succeeds, transferring zero ERC-20 balance (it silently transfers native balance instead, or nothing if value is zero), while the victim-facing contract logic records the deposit as successful.
3. Cancel/withdraw and recover the victim's actual funds (native currency or other legitimately deposited assets) once the token is later deployed at that same address (a common cross-chain deployment pattern), duplicating the Qubit Finance/Solmate honeypot exactly.

This is a chain-level (not off-repo) issue because the vulnerable behavior is baked into the core `pallet-revive` execution semantics used by every EVM-compatible contract on any Substrate chain that includes this pallet, not a third-party bug.

### Likelihood Explanation
Likelihood is contract-pattern dependent: it requires a contract author to use raw/low-level `call()` for token interactions and check only the boolean success flag without also validating return-data length or an `extcodesize` check — a pattern common enough that Solmate's `SafeTransferLib` (cited in the very report this analog is derived from) is widely used for this reason. Since the behavior is a deliberate, documented pallet-revive design choice (matching EVM EOA semantics) rather than an accidental regression, it is a stable, reproducible attack surface across all chains using `pallet-revive`, not a one-off implementation slip.

### Recommendation
Since matching EVM semantics for plain-address calls is an intentional design goal, mitigation should be scoped to Revive-hosted contract security guidance/tooling rather than pallet internals: document prominently (in `pallet-revive` docs and Solidity compatibility notes) that `address.call(...)` to code-less accounts returns `true` with empty data, and recommend/promote a `SafeERC20`-equivalent library for Revive-targeted Solidity that checks `extcodesize(token) > 0` before treating a low-level call's success as a genuine token transfer, mirroring OpenZeppelin's `SafeERC20` recommendation from the original report.

### Proof of Concept
Reduced from `recursive_call_during_constructor_is_balance_transfer` [6](#0-5) :
1. Deploy Contract V ("victim escrow") that expects an ERC-20 token contract at address `T` (not yet deployed on this chain, but known to be deployed with this address on other chains, as in the 1INCH/GEL cross-chain deployment pattern cited in the report).
2. Attacker calls `V.bid(T, amount)`. `V` internally does `bool ok = T.call(abi.encodeWithSignature("transferFrom(address,address,uint256)", attacker, address(this), amount))`.
3. Because `T` has no code, `pallet-revive`'s `Stack::new`/`run_call` returns the no-code branch and executes `transfer_from_origin`, returning `Ok(...)` with empty output regardless of the encoded call data [3](#0-2) .
4. `V` observes `ok == true`, credits the attacker's deposit as `amount` of token `T`, without any of `T`'s ERC-20 balance actually moving.
5. Attacker cancels the bid/auction and recovers whatever real value (native currency, other assets) a victim later deposited under the assumption that `T`'s balance backing was legitimate — reproducing the Solmate honeypot exploit inside a `pallet-revive`-hosted contract.

### Citations

**File:** substrate/frame/revive/src/exec.rs (L858-921)
```rust
	pub fn run_call(
		origin: Origin<T>,
		dest: H160,
		transaction_meter: &'a mut TransactionMeter<T>,
		value: U256,
		input_data: Vec<u8>,
		exec_config: &ExecConfig<T>,
	) -> ExecResult {
		let dest = T::AddressMapper::to_account_id(&dest);
		if let Some((mut stack, executable)) = Stack::<'_, T, E>::new(
			FrameArgs::Call { dest: dest.clone(), cached_info: None, delegated_call: None },
			origin.clone(),
			transaction_meter,
			value,
			exec_config,
			&input_data,
		)? {
			stack.run(executable, input_data).map(|_| stack.first_frame.last_frame_output)
		} else {
			if_tracing(|t| {
				t.enter_child_span(
					origin.account_id().map(T::AddressMapper::to_address).unwrap_or_default(),
					T::AddressMapper::to_address(&dest),
					None,
					false,
					value,
					&input_data,
					Default::default(),
				);
			});

			let result = if let Some(mock_answer) =
				exec_config.mock_handler.as_ref().and_then(|handler| {
					handler.mock_call(T::AddressMapper::to_address(&dest), &input_data, value)
				}) {
				Ok(mock_answer)
			} else {
				Self::transfer_from_origin(
					&origin,
					&origin,
					&dest,
					value,
					transaction_meter,
					exec_config,
				)
			};

			if_tracing(|t| {
				let gas_used =
					transaction_meter.total_consumed_gas().try_into().unwrap_or(u64::MAX);
				let weight_consumed = transaction_meter.weight_consumed();
				match result {
					Ok(ref output) => t.exit_child_span(&output, gas_used, weight_consumed),
					Err(e) => {
						t.exit_child_span_with_error(e.error.into(), gas_used, weight_consumed)
					},
				}
			});

			log::trace!(target: LOG_TARGET, "call finished with: {result:?}");

			result
		}
	}
```

**File:** substrate/frame/revive/src/exec.rs (L1003-1027)
```rust
	/// Create a new call stack.
	///
	/// Returns `None` when calling a non existent contract. This is not an error case
	/// since this will result in a value transfer.
	fn new(
		args: FrameArgs<T, E>,
		origin: Origin<T>,
		transaction_meter: &'a mut TransactionMeter<T>,
		value: U256,
		exec_config: &'a ExecConfig<T>,
		input_data: &Vec<u8>,
	) -> Result<Option<(Self, ExecutableOrPrecompile<T, E, Self>)>, ExecError> {
		origin.ensure_mapped()?;
		let Some((first_frame, executable)) = Self::new_frame(
			args,
			value,
			transaction_meter,
			&CallResources::NoLimits,
			false,
			true,
			input_data,
			exec_config,
		)?
		else {
			return Ok(None);
```

**File:** substrate/frame/revive/src/exec.rs (L2003-2009)
```rust
		)? {
			self.run(executable, input_data)
		} else {
			// Delegate-calls to non-contract accounts are considered success.
			Ok(())
		}
	}
```

**File:** prdoc/stable2412/pr_5664.prdoc (L1-11)
```text
title: Calling an address without associated code is a balance transfer

doc:
  - audience: Runtime Dev
    description: |
     This makes pallet_revive behave like EVM where a balance transfer
     is just a call to a plain wallet.

crates:
  - name: pallet-revive
    bump: patch
```

**File:** prdoc/stable2503/pr_6741.prdoc (L1-16)
```text
title: 'pallet-revive: Adjust error handling of sub calls'
doc:
- audience: Runtime Dev
  description: |-
    We were trapping the host context in case a sub call was exhausting the storage deposit limit set for this sub call. This prevents the caller from handling this error. In this PR we added a new error code that is returned when either gas or storage deposit limit is exhausted by the sub call.

    We also remove the longer used `NotCallable` error. No longer used because this is no longer an error: It will just be a balance transfer.

    We also make `set_code_hash` infallible to be consistent with other host functions which just trap on any error condition.
crates:
- name: pallet-revive
  bump: major
- name: pallet-revive-uapi
  bump: major
- name: pallet-revive-fixtures
  bump: major
```

**File:** substrate/frame/revive/src/exec/tests.rs (L1564-1594)
```rust
#[test]
fn recursive_call_during_constructor_is_balance_transfer() {
	let code = MockLoader::insert(Constructor, |ctx, _| {
		let account_id = ctx.ext.account_id().clone();
		let addr =
			<<Test as Config>::AddressMapper as AddressMapper<Test>>::to_address(&account_id);
		let balance = ctx.ext.balance();

		// Calling ourselves during the constructor will trigger a balance
		// transfer since no contract exist yet.
		assert_ok!(ctx.ext.call(
			&Default::default(),
			&addr,
			(balance - 1).into(),
			vec![],
			ReentrancyProtection::AllowReentry,
			false
		));

		// Should also work with call data set as it is ignored when no
		// contract is deployed.
		assert_ok!(ctx.ext.call(
			&Default::default(),
			&addr,
			1u32.into(),
			vec![1, 2, 3, 4],
			ReentrancyProtection::AllowReentry,
			false
		));
		exec_success()
	});
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/tests/tests.rs (L1931-1968)
```rust
#[test]
fn non_existent_erc20_will_error() {
	let sender: AccountId = ALICE.into();
	let beneficiary: AccountId = BOB.into();
	let revive_account = pallet_revive::Pallet::<Runtime>::account_id();
	let checking_account =
		asset_hub_westend_runtime::xcm_config::ERC20TransfersCheckingAccount::get();
	let initial_wnd_amount = 10_000_000_000_000u128;
	// We try to withdraw an ERC20 token but the address doesn't exist.
	let non_existent_contract_address = [1u8; 20];

	ExtBuilder::<Runtime>::default().build().execute_with(|| {
		// Bring the revive account to life.
		assert_ok!(Balances::mint_into(&revive_account, initial_wnd_amount));
		// Fund all accounts involved.
		assert_ok!(Balances::mint_into(&sender, initial_wnd_amount));
		assert_ok!(Balances::mint_into(&beneficiary, initial_wnd_amount));
		assert_ok!(Balances::mint_into(&checking_account, initial_wnd_amount));

		let wnd_amount_for_fees = 1_000_000_000_000u128;
		let erc20_transfer_amount = 100u128;
		let message = Xcm::<RuntimeCall>::builder()
			.withdraw_asset((Parent, wnd_amount_for_fees))
			.pay_fees((Parent, wnd_amount_for_fees))
			.withdraw_asset((
				AccountKey20 { key: non_existent_contract_address, network: None },
				erc20_transfer_amount,
			))
			.deposit_asset(AllCounted(1), beneficiary.clone())
			.build();
		// Execution fails but doesn't panic.
		assert!(PolkadotXcm::execute(
			RuntimeOrigin::signed(sender.clone()),
			Box::new(VersionedXcm::V5(message)),
			Weight::from_parts(2_500_000_000, 120_000),
		)
		.is_err());
	});
```
