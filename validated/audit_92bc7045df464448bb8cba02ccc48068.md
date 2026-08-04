### Title
`pallet-psm` mint/redeem tracks debt from nominal transfer amounts instead of the actual asset delta received, breaking the 1:1 reserve-to-debt invariant for non-standard `Fungibles` assets — ([File: substrate/frame/psm/src/lib.rs])

### Summary
`pallet-psm` implements a Peg Stability Module that assumes a strict 1:1 backing between internal-asset debt (`PsmDebt`) and the external-asset balance actually held in the PSM's reserve account. This is the same broken invariant flagged in the Nibiru finding: the code computes the amount to mint/burn from the *nominal* transfer amount rather than the *actual* balance delta observed in the reserve/user account, and never re-checks that assumption before advancing debt state.

### Finding Description
In `mint` (`substrate/frame/psm/src/lib.rs`, `pallet::Pallet::mint`), the flow is:
1. Compute `effective_external` and `internal_equivalent` purely from `external_amount` and decimal conversion.
2. Call `T::Fungibles::transfer(external_asset, &who, &psm_account, effective_external, Preservation::Expendable)?;` — the `Balance` actually returned by this call (which for `pallet_assets`/wrapped ERC20 fungibles is nominally equal to the input, but is **not required to be** for tokens with balance changes outside of transfers — rebasing tokens, fee-on-transfer tokens, deflationary supply-adjusting tokens) is discarded, not compared against `effective_external`.
3. `PsmDebt` is then incremented unconditionally by `internal_equivalent` — the *nominal* value — regardless of what the reserve account actually received:
```rust
T::Fungibles::transfer(external_asset.clone(), &who, &psm_account, effective_external, Preservation::Expendable)?;
T::Fungibles::mint_into(internal_asset.clone(), &who, internal_to_user)?;
...
PsmDebt::<T>::insert(&internal_asset, &external_asset, new_debt);
```
This is architecturally identical to Nibiru's `convertCoinToEvmBornERC20`/`convertEvmToCoin`, which assumed a static escrow-to-coin relationship; Nibiru's own later mitigation was to capture the *actual* transferred amount and use that for burn/debt accounting rather than the nominal request — a pattern absent here.

In `redeem`, the pallet does perform a defensive `get_reserve` check before transferring out (`if reserve < external_out { defensive!(...); return Err(Error::<T>::Unexpected.into()) }`), which mitigates outright insolvency-caused panics, but this is a reactive check, not a fix to the underlying accounting: `PsmDebt` was already inflated (or could be, depending on the external asset's real-world behavior) at mint time based on nominal, not actual, reserve balance. `Config::Fungibles` is generic (`FungiblesMutate` + metadata + roles `Inspect`) and is explicitly designed to plug in arbitrary asset backends — including, per `cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs`, assets whose balance is a live proxy for an underlying ERC20 contract accessed via `pallet_revive::Pallet::<T>::bare_call` `IERC20::transferCall`. Any ERC20 exhibiting "balance changes outside of transfers" (rebasing, fee-on-transfer) that is wired in as a PSM external asset reproduces exactly the Nibiru bug class: the PSM's bookkeeping (`PsmDebt`) diverges from the real reserve balance because the code never verifies the actual amount credited/debited against the nominal request.

### Impact Explanation
- If the external asset's actual transfer-in amount is *less* than `effective_external` (fee-on-transfer, negative rebase before settlement), `PsmDebt` and the internal asset minted to the user are based on the larger nominal figure, i.e. the protocol mints internal-asset supply not fully backed by reserve — an unbacked mint, directly increasing insolvency risk of the whole internal-asset peg (all holders of the internal asset are diluted, not just holders of the weird external asset).
- If the external asset's balance in the reserve subsequently decreases outside of a transfer (rebase down) after being escrowed, later redeemers hit the defensive `Error::Unexpected` revert path and are permanently unable to redeem their share — a fund-lock condition, matching the "insufficient balance/permanent lock" scenario from the source report.
- If the external asset balance increases (positive rebase) inside the reserve, the surplus is untracked and effectively stuck/unclaimable by anyone (protocol cannot account for value beyond `PsmDebt`), representing value leakage from user perspective though not privileged access.
- This maps to the required impact categories: theft/unbacked mint, and permanent user-fund lock, without requiring any privileged/malicious actor — a normal user simply mints/redeems against an external asset whose real-world balance semantics are non-transfer-conservative, and PSM's own bookkeeping breaks.

### Likelihood Explanation
Likelihood depends entirely on which `T::AssetId`/`T::Fungibles` implementations governance approves as PSM externals (`add_external_asset`). The pallet's `Config` places no restriction preventing approval of assets backed by arbitrary ERC20 contracts via the `erc20_transactor` bridge, and the PSM README explicitly frames externals as "third-party assets (e.g. USDC, USDT)" without excluding assets with non-standard transfer semantics. Given that the general codebase already integrates ERC20-backed asset wrapping (`erc20_transactor.rs`) and Snowbridge/AssetHub pipelines register arbitrary foreign/Ethereum-origin tokens, it is plausible for an admin to unknowingly approve a rebasing/fee-on-transfer token as a PSM external, at which point every subsequent unprivileged `mint`/`redeem` call by ordinary users triggers the divergence. No attacker action beyond normal usage is required once such an asset is listed.

### Recommendation
- In `mint`, capture the actual balance delta of the reserve account before/after `T::Fungibles::transfer` (or use the `Balance` returned by `transfer`, if the trait guarantees it reflects the real amount received) and base `internal_equivalent`/`PsmDebt` updates on that actual amount, mirroring Nibiru's own recommended fix of tracking `actualSentAmount`.
- In `redeem`, similarly verify actual amount transferred out of the reserve, and fail atomically (not proceed with `PsmDebt` mutation) if it diverges from `external_out`.
- Alternatively, explicitly document/enforce (e.g., via a `Config` trait bound or asset registration check) that `add_external_asset` only accepts assets with strictly transfer-conservative balance semantics, rejecting rebasing/fee-on-transfer tokens, particularly any asset instance whose underlying implementation is `erc20_transactor`-style ERC20-backed.

### Proof of Concept
Conceptual sequence assuming a PSM instance is configured with an external asset `X` whose `Fungibles` backend is ERC20-bridged (via `erc20_transactor.rs`) to a real-world fee-on-transfer/rebasing ERC20:
1. Admin calls `create_psm` for `internal_asset = pUSD` and `add_external_asset` approving `external_asset = X`.
2. User A calls `mint(pUSD, X, 1000, max_fee)`. Code computes `effective_external = 1000` (minus fee logic aside), calls `T::Fungibles::transfer(X, A, psm_account, 1000, ...)`. Because `X` deducts a transfer fee (e.g. 2%) at the ERC20 level, `psm_account` actually receives only `980` of `X`, but `PsmDebt[pUSD][X]` is incremented by `internal_equivalent` computed from the full `1000`.
3. Repeat across many mints: `PsmDebt` grows faster than the real reserve balance of `X` in `psm_account`.
4. User B, an early redeemer, calls `redeem` and succeeds because `get_reserve` still shows enough balance (front-of-queue).
5. A later redeemer C calls `redeem` for the remaining nominal debt share; `get_reserve` returns less than `external_out` (since real balance was inflated relative to debt), the call reverts with `Error::Unexpected`, and C's `pUSD` cannot be redeemed for `X` — permanent lock of that user's swap right, while the protocol's `PsmDebt` bookkeeping never reconciles the shortfall.

Note: I could not fully inspect `get_reserve`'s exact implementation (grep only located its declaration signature reference, not its full body, due to the final-iteration cutoff) — its precise semantics (whether it queries the live `Fungibles` balance vs. a cached figure) should be confirmed directly in `substrate/frame/psm/src/lib.rs` before remediation, but the core issue — `PsmDebt` accounting from nominal rather than actual transferred amounts at `mint` time — is confirmed directly from the cited code. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** substrate/frame/psm/src/lib.rs (L700-767)
```rust
		#[pallet::call_index(0)]
		#[pallet::weight(T::WeightInfo::mint(T::MaxExternals::get()))]
		pub fn mint(
			origin: OriginFor<T>,
			internal_asset: T::AssetId,
			external_asset: T::AssetId,
			external_amount: BalanceOf<T>,
			max_fee: Permill,
		) -> DispatchResult {
			let who = ensure_signed(origin)?;
			let info = Psm::<T>::get(&internal_asset).ok_or(Error::<T>::PsmNotFound)?;

			let external = ExternalAssets::<T>::get(&internal_asset, &external_asset)
				.ok_or(Error::<T>::UnsupportedAsset)?;
			ensure!(external.status.allows_minting(), Error::<T>::MintingStopped);

			let (ext_decimals, internal_decimals) =
				Self::ensure_decimals_match(&info, &internal_asset, &external_asset, &external)?;

			let internal_equivalent =
				Self::external_to_internal(external_amount, ext_decimals, internal_decimals)?;
			ensure!(!internal_equivalent.is_zero(), Error::<T>::AmountTooSmallAfterConversion);
			ensure!(internal_equivalent >= info.min_swap_amount, Error::<T>::BelowMinimumSwap);

			let effective_external =
				Self::internal_to_external(internal_equivalent, ext_decimals, internal_decimals)?;

			let fee_rate = MintingFee::<T>::get(&internal_asset, &external_asset);
			ensure!(fee_rate <= max_fee, Error::<T>::FeeTooHigh);
			let fee = fee_rate.mul_ceil(internal_equivalent);
			let internal_to_user = internal_equivalent.saturating_sub(fee);

			let current_total_psm_debt = Self::total_psm_debt(&internal_asset);
			ensure!(
				current_total_psm_debt.saturating_add(internal_equivalent) <= info.max_debt,
				Error::<T>::ExceedsMaxPsmDebt
			);

			let current_debt = PsmDebt::<T>::get(&internal_asset, &external_asset);
			let max_debt = Self::max_asset_debt(&internal_asset, &external_asset, &info);
			let new_debt = current_debt.saturating_add(internal_equivalent);
			ensure!(new_debt <= max_debt, Error::<T>::ExceedsMaxPsmDebt);

			let psm_account = Self::psm_account(&internal_asset);
			T::Fungibles::transfer(
				external_asset.clone(),
				&who,
				&psm_account,
				effective_external,
				Preservation::Expendable,
			)?;
			T::Fungibles::mint_into(internal_asset.clone(), &who, internal_to_user)?;
			if !fee.is_zero() {
				T::Fungibles::mint_into(internal_asset.clone(), &info.fee_destination, fee)?;
			}

			PsmDebt::<T>::insert(&internal_asset, &external_asset, new_debt);

			Self::deposit_event(Event::Minted {
				who,
				internal_asset,
				external_asset,
				external_consumed: effective_external,
				internal_received: internal_to_user,
				internal_fee: fee,
			});
			Ok(())
		}
```

**File:** substrate/frame/psm/src/lib.rs (L809-902)
```rust
		#[pallet::call_index(1)]
		#[pallet::weight(T::WeightInfo::redeem())]
		pub fn redeem(
			origin: OriginFor<T>,
			internal_asset: T::AssetId,
			external_asset: T::AssetId,
			internal_amount: BalanceOf<T>,
			max_fee: Permill,
		) -> DispatchResult {
			let who = ensure_signed(origin)?;
			let info = Psm::<T>::get(&internal_asset).ok_or(Error::<T>::PsmNotFound)?;

			let external = ExternalAssets::<T>::get(&internal_asset, &external_asset)
				.ok_or(Error::<T>::UnsupportedAsset)?;
			ensure!(external.status.allows_redemption(), Error::<T>::AllSwapsStopped);

			let ext_decimals = external.decimals;
			let internal_decimals = info.internal_decimals;

			ensure!(internal_amount >= info.min_swap_amount, Error::<T>::BelowMinimumSwap);

			let fee_rate = RedemptionFee::<T>::get(&internal_asset, &external_asset);
			ensure!(fee_rate <= max_fee, Error::<T>::FeeTooHigh);
			let fee = fee_rate.mul_ceil(internal_amount);
			let internal_net = internal_amount.saturating_sub(fee);

			let external_out =
				Self::internal_to_external(internal_net, ext_decimals, internal_decimals)?;
			ensure!(
				internal_net.is_zero() || !external_out.is_zero(),
				Error::<T>::AmountTooSmallAfterConversion
			);
			// `effective_internal_net` is the internal value that round-trips to `external_out`;
			// it is what we actually burn and what the tracked debt decreases by. Any truncation
			// dust stays in the caller's internal balance, symmetric with `mint`, which takes
			// only the round-tripped share of the external amount.
			let effective_internal_net =
				Self::external_to_internal(external_out, ext_decimals, internal_decimals)?;

			let current_debt = PsmDebt::<T>::get(&internal_asset, &external_asset);
			ensure!(current_debt >= effective_internal_net, Error::<T>::InsufficientReserve);

			let reserve = Self::get_reserve(&internal_asset, &external_asset);
			if reserve < external_out {
				defensive!("PSM reserve is less than expected output amount");
				return Err(Error::<T>::Unexpected.into());
			}

			if !fee.is_zero() {
				T::Fungibles::transfer(
					internal_asset.clone(),
					&who,
					&info.fee_destination,
					fee,
					Preservation::Expendable,
				)?;
			}

			if !effective_internal_net.is_zero() {
				T::Fungibles::burn_from(
					internal_asset.clone(),
					&who,
					effective_internal_net,
					Preservation::Expendable,
					Precision::Exact,
					Fortitude::Polite,
				)?;
			}

			let psm_account = Self::psm_account(&internal_asset);
			if !external_out.is_zero() {
				T::Fungibles::transfer(
					external_asset.clone(),
					&psm_account,
					&who,
					external_out,
					Preservation::Expendable,
				)?;
			}

			PsmDebt::<T>::mutate(&internal_asset, &external_asset, |debt| {
				*debt = debt.saturating_sub(effective_internal_net);
			});

			Self::deposit_event(Event::Redeemed {
				who,
				internal_asset,
				external_asset,
				internal_consumed: effective_internal_net.saturating_add(fee),
				external_received: external_out,
				internal_fee: fee,
			});
			Ok(())
		}
```

**File:** cumulus/parachains/runtimes/assets/common/src/erc20_transactor.rs (L150-216)
```rust
	fn withdraw_asset_with_surplus(
		what: &Asset,
		who: &Location,
		_context: Option<&XcmContext>,
	) -> Result<(AssetsInHolding, Weight), XcmError> {
		tracing::trace!(
			target: "xcm::transactor::erc20::withdraw",
			?what, ?who,
		);
		let (asset_id, amount) = Matcher::matches_fungibles(what)?;
		let who = AccountIdConverter::convert_location(who)
			.ok_or(MatchError::AccountIdConversionFailed)?;
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
		if let Ok(return_value) = result {
			tracing::trace!(target: "xcm::transactor::erc20::withdraw", ?return_value, "Return value by withdraw_asset");
			if return_value.did_revert() {
				tracing::debug!(target: "xcm::transactor::erc20::withdraw", "ERC20 contract reverted");
				Err(XcmError::FailedToTransactAsset("ERC20 contract reverted"))
			} else {
				let is_success = IERC20::transferCall::abi_decode_returns_validate(&return_value.data).map_err(|error| {
					tracing::debug!(target: "xcm::transactor::erc20::withdraw", ?error, "ERC20 contract result couldn't decode");
					XcmError::FailedToTransactAsset("ERC20 contract result couldn't decode")
				})?;
				if is_success {
					tracing::trace!(target: "xcm::transactor::erc20::withdraw", "ERC20 contract was successful");
					Ok((
						AssetsInHolding::new_from_fungible_credit(
							what.id.clone(),
							Box::new(Erc20Credit(amount)),
						),
						surplus,
					))
				} else {
					tracing::debug!(target: "xcm::transactor::erc20::withdraw", "contract transfer failed");
					Err(XcmError::FailedToTransactAsset("ERC20 contract transfer failed"))
				}
			}
		} else {
			tracing::debug!(target: "xcm::transactor::erc20::withdraw", ?result, "Error");
			// This error could've been duplicate smart contract, out of gas, etc.
			// If the issue is gas, there's nothing the user can change in the XCM
			// that will make this work since there's a hardcoded gas limit.
			Err(XcmError::FailedToTransactAsset("ERC20 contract execution errored"))
		}
	}
```
