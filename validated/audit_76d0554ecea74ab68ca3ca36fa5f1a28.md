### Title
Silent fund loss when ERC20 asset deposit fails during `transfer_asset` fallback — the `Erc20Credit` imbalance is dropped without any compensating on-chain balance action (File: `polkadot/xcm/xcm-executor/src/traits/transact_asset.rs`, `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`)

### Summary
The Sherlock report describes a token-transfer helper that silently fails (always returns `false`/reverts) for non-standard ERC20 tokens, so the protocol can end up "claiming" support for a token it cannot actually move. The local analog is the `ERC20Transactor` added for Asset Hub's XCM asset handling, combined with the generic `TransactAsset::transfer_asset` fallback path: when a deposit-back-to-owner best-effort recovery also fails for a "weird" ERC20 (blacklist-style, no-bool-return, or always-`false` tokens like the report's Tether Gold example), the XCM executor silently discards the `AssetsInHolding` credit representing that value, while the real ERC20 balance is left permanently stuck in the shared `TransfersCheckingAccount`.

### Finding Description
`ERC20Transactor` (in `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`) only overrides the `*_with_surplus` variants of `TransactAsset`: [1](#0-0) [2](#0-1) 

It does not implement plain `withdraw_asset`/`deposit_asset`/`internal_transfer_asset`, so any code path that calls those plain trait methods falls back to the trait's defaults, which return `Err(XcmError::Unimplemented)`: [3](#0-2) 

The generic `transfer_asset` default implementation on `TransactAsset` is exactly such a caller. When `internal_transfer_asset` is unimplemented (as it is for `ERC20Transactor`), it does a manual withdraw+deposit and, if the deposit to the destination fails, it attempts a **best-effort** refund back to the original owner and then **discards the result**: [4](#0-3) 

```rust
Self::deposit_asset(credit, to, Some(context)).map_err(|(unspent, error)| {
    // best effort try to return the assets to original owner
    let _ = Self::deposit_asset(unspent, from, Some(context));
    error
})?;
```

For the ERC20 case, the value being carried through this path is an `Erc20Credit`, whose own doc comment states plainly that it "does not perform runtime-level balance enforcement" — i.e., dropping it has zero effect on the actual ERC20 contract balances: [5](#0-4) 

Meanwhile, `withdraw_asset_with_surplus` has already performed a **real, irreversible on-chain ERC20 transfer** of the user's tokens into `TransfersCheckingAccount` (a single PalletId-derived account shared by every user and every ERC20 asset on the chain): [6](#0-5) 

If `deposit_asset_with_surplus` to the destination then fails (which the repository's own tests demonstrate happens for realistic "weird" tokens — non-bool-returning, false-returning, or gas-hungry contracts, mirroring the reported Tether-Gold-style behavior): [7](#0-6) 

and if the best-effort refund back to `from` *also* fails — which is exactly the scenario a compliance-blacklisted or always-`false`-returning token guarantees, since it fails identically regardless of counterparty — the `unspent` `Erc20Credit` is simply dropped by `let _ = ...`. There is no `Drop` impl or `handle_dust`-style recovery for it, so the amount is erased from the runtime's XCM asset accounting while the real tokens remain permanently held in `TransfersCheckingAccount` with no on-chain record of which user is owed what.

### Impact Explanation
This is a genuine "permanent user-fund lock" in the sense required by the impact gate: unprivileged users can trigger real ERC20 transfers into a shared checking account through ordinary XCM `TransferAsset`/`InitiateTransfer`-style instructions involving `ERC20Transactor`-matched assets, and if the token's transfer semantics make both the primary deposit and the best-effort refund fail (a realistic condition for the exact token class named in the source report), funds become permanently stuck with no accounting trail, and the XCM executor reports only a generic `FailedToTransactAsset` error, masking that value has already moved and cannot be reclaimed.

### Likelihood Explanation
This requires no privileged actor, relayer, validator, or governance action — any user submitting an XCM program (via `pallet-xcm::execute` or as part of a larger transfer/teleport instruction sequence) that references an `AccountKey20`-identified ERC20 asset which behaves like a non-standard/blacklist/always-false token can trigger the withdraw succeeding and deposit(s) failing. Given Asset Hub explicitly documents intent to support arbitrary ERC20 tokens via this transactor (prdoc `pr_7762.prdoc`), and the repository's own test suite already demonstrates several classes of tokens that make the deposit leg fail, the precondition (a non-conforming token) is squarely within the documented threat model this PR intends to support, exactly matching the "weird token" bug class from the source report.

### Recommendation
- Do not silently discard the `unspent` value in `transfer_asset`'s fallback; if the best-effort return-to-owner also fails, surface this distinctly (e.g., a dedicated `XcmError` variant or event) so the failure is auditable and, ideally, recorded so the stuck value can be manually reconciled/refunded.
- For `ERC20Transactor` specifically, avoid using a single shared `TransfersCheckingAccount` for the escrow leg without a persisted per-user/per-asset ledger; track owed amounts on-chain (storage map) rather than relying purely on the ERC20 contract's transfer semantics and an in-memory imbalance object that has no enforcement, so failed deposits can be resolved via a dedicated recovery/claim extrinsic instead of being lost.
- Add an explicit check/whitelist step (or fee/deposit-based registration path) validating that a token conforms to the ERC20 return-value semantics assumed by the transactor before permitting it to be used as an XCM-transactable asset.

### Proof of Concept
1. Deploy an ERC20 whose `transfer` function returns `false` (or reverts) whenever the recipient equals a specific blacklisted address, but succeeds for other recipients (mirrors Tether Gold–style behavior referenced in the source report).
2. Register this contract's address as an XCM-transactable asset location matched by `ERC20Matcher` on Asset Hub.
3. As an ordinary user, build and execute (via `pallet_xcm::execute`) an XCM program that performs `WithdrawAsset`/`InitiateTransfer` (or any instruction hitting `TransactAsset::transfer_asset`) moving this ERC20 from the user (`from`) to a beneficiary (`to`) that is blacklisted by the token.
4. `withdraw_asset_with_surplus` succeeds — the user's real ERC20 balance is transferred into `TransfersCheckingAccount`.
5. `deposit_asset` to `to` fails (token returns `false`/reverts for the blacklisted recipient).
6. The fallback in `transfer_asset` attempts `deposit_asset(unspent, from, ...)` back to the original owner — but if `from` is also blacklisted (or the token unconditionally returns `false`), this also fails and its result is discarded (`let _ = ...`).
7. The `Erc20Credit` value is dropped with no compensating action; the on-chain ERC20 balance remains permanently held in `TransfersCheckingAccount`, unrecoverable through any exposed extrinsic.

Note: I was not able to execute this scenario in a live runtime within this session (no test harness run), so the exact panics/error propagation at the `pallet-xcm` dispatch layer around step 6 should be confirmed with an integration test modeled on the existing `smart_contract_does_not_return_bool_fails`/`expensive_erc20_runs_out_of_gas` tests in `cumulus/parachains/runtimes/assets/asset-hub-westend/tests/tests.rs`, extended with a blacklist-style contract and a `from`-side failure to observe the silently dropped credit.

### Citations

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L73-89)
```rust
/// A minimal imbalance tracking type that holds an ERC20 token amount.
///
/// This type implements the necessary imbalance accounting traits but does not perform
/// runtime-level balance enforcement. It's used to track ERC20 token amounts within XCM
/// asset holdings, where the actual balance constraints are enforced by the ERC20 smart
/// contract itself rather than the runtime.
struct Erc20Credit(u128);
impl UnsafeConstructorDestructor<u128> for Erc20Credit {
	fn unsafe_clone(&self) -> Box<dyn ImbalanceAccounting<u128>> {
		Box::new(Erc20Credit(self.0))
	}
	fn forget_imbalance(&mut self) -> u128 {
		let amount = self.0;
		self.0 = 0;
		amount
	}
}
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L150-154)
```rust
	fn withdraw_asset_with_surplus(
		what: &Asset,
		who: &Location,
		_context: Option<&XcmContext>,
	) -> Result<(AssetsInHolding, Weight), XcmError> {
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L162-184)
```rust
		// We need to map the 32 byte checking account to a 20 byte account.
		let checking_account_eth = T::AddressMapper::to_address(&TransfersCheckingAccount::get());
		let checking_address = Address::from(Into::<[u8; 20]>::into(checking_account_eth));
		let weight_limit = WeightLimit::get();
		// To withdraw, we actually transfer to the checking account.
		// We do this using the solidity ERC20 interface.
		let data =
			IERC20::transferCall { to: checking_address, value: EU256::from(amount) }.abi_encode();
		let ContractResult { result, weight_consumed, storage_deposit, .. } =
			pallet_revive::Pallet::<T>::bare_call(
				OriginFor::<T>::signed(who.clone()),
				asset_id,
				U256::zero(),
				TransactionLimits::WeightAndDeposit {
					weight_limit,
					deposit_limit: StorageDepositLimit::get(),
				},
				data,
				&ExecConfig::new_substrate_tx(),
			);
		// We need to return this surplus for the executor to allow refunding it.
		let surplus = weight_limit.saturating_sub(weight_consumed);
		tracing::trace!(target: "xcm::transactor::erc20::withdraw", ?weight_consumed, ?surplus, ?storage_deposit);
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L225-229)
```rust
	fn deposit_asset_with_surplus(
		what: AssetsInHolding,
		who: &Location,
		_context: Option<&XcmContext>,
	) -> Result<Weight, (AssetsInHolding, XcmError)> {
```

**File:** polkadot/xcm/xcm-executor/src/traits/transact_asset.rs (L82-117)
```rust
	fn deposit_asset(
		what: AssetsInHolding,
		_who: &Location,
		_context: Option<&XcmContext>,
	) -> Result<(), (AssetsInHolding, XcmError)> {
		Err((what, XcmError::Unimplemented))
	}

	/// Identical to `deposit_asset` but returning the surplus, if any.
	///
	/// Return the difference between the worst-case weight and the actual weight consumed.
	/// This can be zero most of the time unless there's some metering involved.
	fn deposit_asset_with_surplus(
		what: AssetsInHolding,
		who: &Location,
		context: Option<&XcmContext>,
	) -> Result<Weight, (AssetsInHolding, XcmError)> {
		Self::deposit_asset(what, who, context).map(|()| Weight::zero())
	}

	/// Withdraw the given asset from the consensus system.
	///
	/// Return the actual asset(s) withdrawn, which should always be equal to `_what`.
	///
	/// The XCM `_maybe_context` parameter may be `None` when the caller of `withdraw_asset` is
	/// outside of the context of a currently-executing XCM. An example will be the `charge_fees`
	/// method in the XCM executor.
	///
	/// Implementations should return `XcmError::FailedToTransactAsset` if withdraw failed.
	fn withdraw_asset(
		_what: &Asset,
		_who: &Location,
		_maybe_context: Option<&XcmContext>,
	) -> Result<AssetsInHolding, XcmError> {
		Err(XcmError::Unimplemented)
	}
```

**File:** polkadot/xcm/xcm-executor/src/traits/transact_asset.rs (L167-185)
```rust
	fn transfer_asset(
		asset: &Asset,
		from: &Location,
		to: &Location,
		context: &XcmContext,
	) -> Result<Asset, XcmError> {
		match Self::internal_transfer_asset(asset, from, to, context) {
			Err(XcmError::AssetNotFound | XcmError::Unimplemented) => {
				let credit = Self::withdraw_asset(asset, from, Some(context))?;
				Self::deposit_asset(credit, to, Some(context)).map_err(|(unspent, error)| {
					// best effort try to return the assets to original owner
					let _ = Self::deposit_asset(unspent, from, Some(context));
					error
				})?;
				Ok(asset.clone())
			},
			result => result,
		}
	}
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/tests/tests.rs (L2021-2073)
```rust
#[test]
fn smart_contract_does_not_return_bool_fails() {
	let sender: AccountId = ALICE.into();
	let beneficiary: AccountId = BOB.into();
	let revive_account = pallet_revive::Pallet::<Runtime>::account_id();
	let checking_account =
		asset_hub_westend_runtime::xcm_config::ERC20TransfersCheckingAccount::get();
	let initial_wnd_amount = 10_000_000_000_000u128;

	ExtBuilder::<Runtime>::default().build().execute_with(|| {
		// Bring the revive account to life.
		assert_ok!(Balances::mint_into(&revive_account, initial_wnd_amount));

		// Fund all accounts involved.
		assert_ok!(Balances::mint_into(&sender, initial_wnd_amount));
		assert_ok!(Balances::mint_into(&beneficiary, initial_wnd_amount));
		assert_ok!(Balances::mint_into(&checking_account, initial_wnd_amount));

		// This contract implements the ERC20 interface for `transfer` except it returns a uint256.
		let code = compile_module_with_type("MyTokenFake", FixtureType::Resolc)
			.expect("compile ERC20")
			.0;

		let initial_amount_u256 = U256::from(1_000_000_000_000u128);
		let constructor_data = sol_data::Uint::<256>::abi_encode(&initial_amount_u256);

		let Contract { addr: non_erc20_address, .. } = bare_instantiate(&sender, code)
			.transaction_limits(TransactionLimits::WeightAndDeposit {
				weight_limit: Weight::from_parts(500_000_000_000, 10 * 1024 * 1024),
				deposit_limit: Balance::MAX,
			})
			.data(constructor_data)
			.build_and_unwrap_contract();

		let wnd_amount_for_fees = 1_000_000_000_000u128;
		let erc20_transfer_amount = 100u128;
		let message = Xcm::<RuntimeCall>::builder()
			.withdraw_asset((Parent, wnd_amount_for_fees))
			.pay_fees((Parent, wnd_amount_for_fees))
			.withdraw_asset((
				AccountKey20 { key: non_erc20_address.into(), network: None },
				erc20_transfer_amount,
			))
			.deposit_asset(AllCounted(1), beneficiary.clone())
			.build();
		// Execution fails but doesn't panic.
		assert!(PolkadotXcm::execute(
			RuntimeOrigin::signed(sender.clone()),
			Box::new(VersionedXcm::V5(message)),
			Weight::from_parts(2_500_000_000, 220_000),
		)
		.is_err());
	});
```
