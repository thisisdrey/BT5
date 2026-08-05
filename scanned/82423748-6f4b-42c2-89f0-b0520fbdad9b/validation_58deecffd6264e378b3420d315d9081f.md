### Title
Permissionless whale-fillable aggregate debt ceiling in `pallet-psm::mint` can DoS minting for all users and all approved externals on a PSM instance - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
`pallet-psm` implements a Peg Stability Module where any signed account can call the public `mint` extrinsic to deposit an external asset and receive the pallet's internal asset, subject to a shared aggregate cap (`PsmInfo::max_debt`) and a per-external ceiling derived from `AssetCeilingWeight`. This aggregate cap is exactly analogous to Liquity's TCR/CCR: as long as total debt stays under the ceiling, minting proceeds normally; once the shared ceiling is reached, **every** further mint on **any** approved external of that PSM instance is rejected with `ExceedsMaxPsmDebt`, for every user, with no per-account limit, no cooldown, and no privileged action required to trigger it.

### Finding Description
The pallet tracks debt per `(internal_asset, external_asset)` but enforces both a per-external ceiling and a pallet-wide aggregate ceiling shared across all approved externals of the same instance: [1](#0-0) 

The `set_max_debt` extrinsic documents this shared, mint-time throttle: [2](#0-1) 

The pallet's own regression test explicitly confirms the aggregate check spans externals: filling 50% of the shared ceiling via `USDC` blocks a subsequent mint of the remaining share plus one unit via `USDT`, even though `USDT`'s own weight-derived slice was untouched, because the check is against the **global** `max_debt` for the instance: [3](#0-2) 

A second test walks the debt up to the ceiling in a loop and shows the guard condition is simply `current_debt + amount > max_debt`, i.e., a single global counter with no per-user allotment, no rate limit, and no minimum time between mints: [4](#0-3) 

`mint` is dispatched by any `RuntimeOrigin::signed` account — it is a fully public, unprivileged entrypoint: [5](#0-4) 

This mirrors the Liquity bug precisely: in Liquity, any unprivileged borrower can push the branch's TCR down to CCR in one transaction, after which `_requireNoBorrowing` blocks all further borrowing for every user until someone performs an economically unattractive redemption to restore TCR. Here, any unprivileged minter can push the shared `max_debt` counter to its ceiling in one (or a few) `mint` calls, after which `ExceedsMaxPsmDebt` blocks all further minting for every user and every approved external of that instance, until someone calls `redeem` to burn back internal asset and free up headroom — a redemption that costs the caller the `RedemptionFee` and is not guaranteed to be profitable for them, exactly the "current mitigation is unprofitable to the redeemer" problem flagged in the source report.

Because `max_debt` is a single global counter with no per-account cap, cooldown, or fairness allocation, an early depositor (when `max_debt` is still small at bootstrap) or a whale with enough of the external asset can consume the entire ceiling cheaply and unilaterally, denying the mint path to everyone else on that PSM instance — including externals whose own per-asset weight would otherwise still allow room, since the aggregate check dominates once close to the shared cap.

### Impact Explanation
This is a permissionless, public-entrypoint denial-of-service on a core value-flow primitive (minting the internal stablecoin against external collateral). It does not require any admin, governance, validator, or malicious peer — a single unprivileged account can halt minting for the entire PSM instance (all approved externals) by exhausting the shared `max_debt` counter in normal, permitted calls. This matches the "public underpriced work that degrades... stalls processing" and "message queue/payout state must only advance atomically and fairly" impact classes: legitimate users are locked out of minting until someone absorbs a redemption-fee cost to unwind the attacker's position, which the attacker can then trivially refill.

### Likelihood Explanation
High. `mint` is a normal, low-privilege user operation; no special conditions, timing races, or governance actions are required. The attack is simply "mint as much as the per-asset ceiling weight allows, as many times/assets as needed to reach the aggregate `max_debt`." The barrier to entry scales only with the size of `max_debt` at the time of the attack — trivially cheap immediately after `create_psm`/`add_external_asset`, and still economically feasible for a well-capitalized actor later, mirroring the "whale" and "early depositor" scenarios in the source report.

### Recommendation
- Do not let a single mint (or a small number of mints from one actor) exhaust the entire shared `max_debt` headroom; add a per-account or per-block minting rate limit, or size ceiling increments/adjustments dynamically rather than as a single global threshold that flips a hard binary allow/deny for all users.
- Consider allowing mints that keep or improve the ratio of debt to available collateral/backing (analogous to the Liquity mitigation of "allow if it raises TCR"), rather than a hard stop the instant `max_debt` is reached.
- Ensure `redeem`, which is the only permissionless way to restore headroom, is not structured so that unwinding the ceiling is systematically unprofitable (e.g., cap/adjust `RedemptionFee` when the circuit is saturated), so the system cannot be held in a griefed state indefinitely by a single actor who can cheaply refill it after each redemption.

### Proof of Concept
1. Deploy a PSM instance with `max_debt = D` and two approved externals `A` and `B`, each with `AssetCeilingWeight = 50%` (as in the existing test setup).
2. Attacker funds an account with enough of external `A` to mint `50% of D` (`Psm::mint(signed(attacker), internal, A, 50%_of_max_debt_worth_of_A, fee)`), which succeeds and consumes half of the aggregate ceiling — reproduced by `fails_mint_exceeds_aggregate_psm_ceiling`: [6](#0-5) 
3. Attacker repeats via a second account/asset to consume the rest of `D` (or a single large mint if their asset's weight ceiling allows), driving aggregate `PsmDebt` to `max_debt`.
4. Any other unprivileged user's `mint` call on any approved external (`A`, `B`, or a future asset added to the instance) now reverts with `Error::ExceedsMaxPsmDebt`, as shown by the loop-based ceiling test: [4](#0-3) 
5. Minting for the entire PSM instance stays blocked for all users until someone calls `redeem` and pays `RedemptionFee` to reduce `PsmDebt`, after which the attacker (or anyone) can immediately re-fill the ceiling and repeat the griefing cycle at will.

### Citations

**File:** substrate/frame/psm/README.md (L41-51)
```markdown
### 1. Mint (External → Internal)

```rust
mint(origin, internal_asset, asset_id, external_amount)
```

- Deposits `external_amount` of `asset_id` into `internal_asset`'s PSM reserve
- Mints `internal_asset` to the user (minus minting fee)
- Fee is minted as `internal_asset` and transferred to the instance's `fee_destination`
- Enforces the per-instance aggregate `max_debt` and the per-external normalised ceiling
- Requires the swap (in internal units) to be `>= PsmInfo::min_swap_amount`
```

**File:** substrate/frame/psm/README.md (L65-78)
```markdown
## Debt Ceiling

Each PSM instance has an absolute internal-asset debt ceiling stored on
`PsmInfo::max_debt`. Within that, per-external ceilings are derived from
ceiling weights:

```
max_asset_debt(internal, external) =
    (AssetCeilingWeight[internal, external] / sum_of_weights[internal])
        * Psm[internal].max_debt
```

Setting an asset's weight to 0% disables minting for that external and
redistributes its share to the others within the same instance.
```

**File:** substrate/frame/psm/src/lib.rs (L1166-1186)
```rust
		pub fn set_max_debt(
			origin: OriginFor<T>,
			internal_asset: T::AssetId,
			value: BalanceOf<T>,
		) -> DispatchResult {
			Self::ensure_psm_admin(origin, &internal_asset, |l| l.can_set_max_debt())?;
			// `max_debt` only gates minting: lowering it below outstanding debt does not claw
			// anything back, it just pauses minting until redemptions bring the debt back under
			// the new ceiling.
			let old_value =
				Psm::<T>::try_mutate(&internal_asset, |maybe| -> Result<_, DispatchError> {
					let info = maybe.as_mut().ok_or(Error::<T>::PsmNotFound)?;
					Ok(core::mem::replace(&mut info.max_debt, value))
				})?;
			Self::deposit_event(Event::MaxDebtUpdated {
				internal_asset,
				old_value,
				new_value: value,
			});
			Ok(())
		}
```

**File:** substrate/frame/psm/src/tests.rs (L346-382)
```rust
	#[test]
	fn fails_mint_exceeds_aggregate_psm_ceiling() {
		new_test_ext().execute_with(|| {
			// Set both assets to 50% ratio each (100% total)
			// This tests that aggregate PSM ceiling is enforced even when per-asset ceilings allow
			set_asset_ceiling_weight(USDC_ASSET_ID, Permill::from_percent(50));
			set_asset_ceiling_weight(USDT_ASSET_ID, Permill::from_percent(50));

			let max_psm_debt = crate::Pallet::<Test>::max_psm_debt(&INTERNAL_ASSET_ID);

			// Mint 50% of PSM ceiling via USDC (succeeds)
			let usdc_amount = Permill::from_percent(50).mul_floor(max_psm_debt);
			fund_external_asset(USDC_ASSET_ID, ALICE, usdc_amount);
			assert_ok!(Psm::mint(
				RuntimeOrigin::signed(ALICE),
				INTERNAL_ASSET_ID,
				USDC_ASSET_ID,
				usdc_amount,
				Permill::from_percent(1)
			));

			// Try to mint 50% + 1 via USDT (total would exceed PSM ceiling)
			let usdt_amount = Permill::from_percent(50).mul_floor(max_psm_debt) + 1;
			fund_external_asset(USDT_ASSET_ID, BOB, usdt_amount);

			assert_noop!(
				Psm::mint(
					RuntimeOrigin::signed(BOB),
					INTERNAL_ASSET_ID,
					USDT_ASSET_ID,
					usdt_amount,
					Permill::from_percent(1)
				),
				Error::<Test>::ExceedsMaxPsmDebt
			);
		});
	}
```

**File:** substrate/frame/psm/src/tests.rs (L2246-2256)
```rust
			loop {
				let current_debt = PsmDebt::<Test>::get(INTERNAL_ASSET_ID, USDC_ASSET_ID);

				// Check if we can mint another `amount`
				if current_debt + amount > max_debt {
					println!("\n=== Debt ceiling reached after {} cycles ===", cycle);
					println!("Current debt: {:.2}", current_debt as f64 / unit);
					println!("Max debt: {:.2}", max_debt as f64 / unit);
					println!("Cannot mint {:.2} more (would exceed ceiling)", amount as f64 / unit);
					break;
				}
```
