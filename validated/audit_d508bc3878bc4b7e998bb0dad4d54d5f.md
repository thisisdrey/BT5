## Title
`pallet-psm::mint` violates checks-effects-interactions: debt-ceiling check reads stale `PsmDebt` before external asset transfers/mints complete, enabling reentrant debt-ceiling bypass analogous to the LybraV2 flashloan issue - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
The reported LybraV2 bug is a classic check-then-act flaw: a limit (`configurator.getEUSDMaxLocked()`) is checked against a value (`EUSD.balanceOf(address(this))`) that can be transiently manipulated by a callback/flash-loan executed *inside* the very call that performs the check, because the state used for enforcement is only persisted *after* the external interaction. The polkadot-sdk repository contains a directly analogous pattern in `pallet-psm`'s `mint` extrinsic [1](#0-0) .

### Finding Description
In `Pallet::<T>::mint` (`substrate/frame/psm/src/lib.rs:702-767`), the debt-ceiling checks are computed from `PsmDebt::<T>` storage *before* any external interaction, but the storage update (`PsmDebt::<T>::insert`) happens only *after* the external asset transfer and internal asset minting: [2](#0-1) 

Specifically:
1. `current_total_psm_debt = Self::total_psm_debt(&internal_asset)` is read from storage.
2. `ensure!(current_total_psm_debt + internal_equivalent <= info.max_debt, ...)` and the analogous per-asset check against `PsmDebt::<T>::get(...)` are both evaluated against the *stale* value.
3. Only afterwards does the pallet call `T::Fungibles::transfer(...)` (external asset in) and `T::Fungibles::mint_into(...)` (internal asset out, twice: once to the user, once for the fee to `fee_destination`).
4. `PsmDebt::<T>::insert(&internal_asset, &external_asset, new_debt)` — the actual bookkeeping update — occurs last, after both external calls have already executed.

`T::Fungibles` is a generic associated type (`fungibles::Mutate`/`Transfer` trait bound), so the concrete backing asset implementation is runtime-configurable. If the configured fungibles backend can invoke any external logic during `transfer`/`mint_into` — e.g. an asset type with `FrozenBalance`/`Holder`/`died` hooks (as seen elsewhere in the assets pallet, `T::Freezer::died`, `T::Holder::died`, cf. `substrate/frame/assets/src/functions.rs`), or an asset bridged into `pallet-revive`/`pallet-contracts` where mint/transfer can trigger a contract call-back — a malicious actor controlling that hook can reenter `Pallet::<T>::mint` before `PsmDebt` is updated. Every nested reentrant call will observe the same stale `current_total_psm_debt`/`PsmDebt` values used by the outer call and pass the `ExceedsMaxPsmDebt` check, letting the attacker mint internal stablecoin far beyond `PsmInfo::max_debt` — the exact "flash-loan bypasses balance-based limit check" primitive from the external report, just applied to a debt-tracking ceiling instead of a raw balance.

This is precisely the anti-pattern the report calls out: the invariant (`total debt <= max_debt`) is enforced using a value that is not yet finalized for the duration of the external interaction, rather than being checked-then-immediately-committed atomically or guarded by reentrancy protection.

### Impact Explanation
If the fungibles backend used for either the internal or external asset in a given PSM instance permits any reentrant callback during `transfer`/`mint_into` (via freezer/holder hooks, XCM-triggered code, or a contract-controlled asset integration), an attacker can mint unbacked internal stablecoin beyond the governance-configured `max_debt`/per-asset ceiling. This directly violates "theft or unbacked mint" and "runtime bugs that compromise intended behavior" in the impact gate: the debt ceiling is a solvency control tying internal-asset supply to deposited external collateral, and bypassing it produces unbacked internal-asset issuance, undermining the PSM's 1:1 peg guarantee and threatening insolvency of the reserve.

### Likelihood Explanation
The likelihood depends on whether any concrete runtime configuration wires `T::Fungibles` to an asset implementation capable of invoking caller-controlled code during `transfer`/`mint_into` within the same call stack (e.g., a custom asset with freezer/holder hooks, or integration with `pallet-revive`/`pallet-contracts` precompiles). The pallet itself provides no reentrancy guard (no `frame_support::storage::with_transaction` reentrancy lock, no `ReentrancyGuard`, no `NonReentrant` trait usage) and no explicit ordering fix (state is not updated before interactions). Given `T::Fungibles` is a generic, runtime-configurable trait bound and the pallet doc explicitly supports multiple PSM instances/backing assets, the precondition for reentrant hook invocation is realistically achievable in a permissively-configured runtime, making this a credible, code-verifiable analog even though full exploitability is runtime-configuration dependent.

### Recommendation
Apply the checks-effects-interactions pattern consistently: compute and persist `PsmDebt::<T>::insert(...)` (and any other ceiling bookkeeping) *before* invoking `T::Fungibles::transfer`/`mint_into`, or wrap the whole extrinsic body in an explicit reentrancy guard so a nested call to `mint`/`redeem` for the same PSM instance cannot execute until the outer call finishes. Additionally, verify the ceiling check again after the external interactions complete (defense in depth), similar to the LybraV2 fix of checking the balance/state *after* the flash loan rather than before.

### Proof of Concept
Conceptual (config-dependent) sequence, mirroring the LybraV2 exploit:
1. Attacker controls (or influences via `T::Fungibles`) an asset backend for `external_asset` or `internal_asset` whose `transfer`/`mint_into` implementation triggers a hook (e.g. a `Freezer`/`Holder`/contract-callback) that can call back into the runtime.
2. Attacker calls `Psm::mint(internal_asset, external_asset, external_amount, max_fee)` with `external_amount` sized to just satisfy the ceiling check (`current_total_psm_debt + internal_equivalent <= info.max_debt` at `substrate/frame/psm/src/lib.rs:732-736`).
3. Inside `T::Fungibles::transfer`/`mint_into` (lines 744-753, before `PsmDebt::insert` at line 756), the hook reenters `Psm::mint` for the same `internal_asset`/`external_asset` pair.
4. The reentrant call reads the same stale `PsmDebt`/`total_psm_debt` values (since the outer call has not yet reached its `insert`), passes the ceiling check again, and mints more internal asset.
5. Repeating this recursively lets debt exceed `info.max_debt` by an arbitrary multiple of the ceiling, each nested call committing its own `PsmDebt::insert` on unwind — resulting in unbacked internal-asset supply beyond the configured ceiling.

**Note on verification limits:** I was unable to retrieve the full `redeem` implementation and the `Config` trait's exact bound on `T::Fungibles` (tool access ended before final `read_file` calls completed), so I cannot confirm from this session whether a concretely-configured runtime (e.g. Asset Hub) actually pairs `pallet-psm` with an asset backend capable of hook-triggered reentrancy. This should be verified in a Devin session with full repository access before treating this as guaranteed-exploitable in production; the finding is a strong, code-supported *analog pattern* (state finalized after external interaction) rather than a confirmed end-to-end exploit in a specific deployed runtime.

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
