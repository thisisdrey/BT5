### Title
Permissionless asset creation + permissionless AMM pool creation lets an attacker permanently freeze a shared Asset Conversion pool's funds - (File: `substrate/frame/assets/src/lib.rs`, `substrate/frame/asset-conversion/src/lib.rs`)

### Summary
The Sherlock finding is a "malicious `tokenIn`" bug: `OracleLess` lets any address supply an arbitrary `IERC20` as `tokenIn`, and the attacker's token reverts on transfer, permanently bricking the refund path (`_cancelOrder`) for orders using it. The Polkadot SDK analog is structurally identical: `pallet-assets` allows permissionless creation of a Trust-Backed asset whose creator is also its `freezer`/admin (self-appointed, not chain governance), and `pallet-asset-conversion`'s `create_pool`/`add_liquidity` are fully permissionless and accept **any** `AssetKind` including such an attacker-controlled asset. Once other users have deposited liquidity into a pool containing the attacker's asset, the attacker can call `Assets::block` on the pool's own sovereign account, which makes every subsequent `T::Assets::transfer` out of that account fail — exactly like the reverting `tokenIn.safeTransfer` in `OracleLess._cancelOrder`. This permanently locks the pool's assets for every honest LP/swapper, not just the attacker.

### Finding Description
- `pallet_assets::Pallet::block` (`substrate/frame/assets/src/lib.rs:1868-1892`) only requires `origin == d.freezer`:
```
ensure!(origin == d.freezer, Error::<T, I>::NoPermission);
...
maybe_account.as_mut().ok_or(...)?.status = AccountStatus::Blocked;
``` [1](#0-0) 
On a chain such as Asset Hub, Trust-Backed assets are created **permissionlessly** by any signed account, and the creator becomes `owner`/`issuer`/`admin`/`freezer` of their own asset (this is the standard, intended design for permissionless asset issuance, not a governance/admin misuse).
- `pallet_asset_conversion::create_pool` and `add_liquidity` are permissionless and accept arbitrary `AssetKind`s from `T::Assets` (the Trust-Backed `Assets` pallet on Asset Hub), as shown by the existing DoS-hardening test `cannot_block_pool_creation` which already demonstrates attacker-controlled tokens being pushed into arbitrary/attacker-chosen pool accounts: [2](#0-1) 
- Swaps and liquidity removal (`swap_tokens_for_exact_tokens`, `swap_exact_tokens_for_tokens`, `remove_liquidity`) all ultimately move funds **out of the pool's sovereign account** via `T::Assets::transfer`/`Balanced` operations. Once the attacker's asset account for the pool account is `Blocked`, `pallet-assets`'s `can_withdraw`/`reducible_balance` checks return a blocked/frozen result and any transfer attempting to move that asset out of the pool fails.
- The only place in this codebase where this exact "asset admin blocks the acting account" scenario is defensively handled is the fee-payment refund path (`substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs`), which explicitly probes `can_deposit`/`can_withdraw` before attempting a refund and quietly skips the refund if blocked (see the `post_dispatch_ok_when_asset_account_blocked_post_withdraw` test). There is no equivalent probing in `pallet-asset-conversion`'s core liquidity/swap dispatchables for the case where the **pool account itself** (not a user account) becomes blocked for one of the pool's constituent assets.

### Impact Explanation
Once the attacker blocks the pool account for their own asset:
- Every LP who added liquidity to that pool can no longer `remove_liquidity` (their share of that asset can never leave the pool account).
- Every swapper attempting to swap into/out of that asset via the pool has their transaction fail, and funds already routed through multi-hop swaps that terminate in this pool can become stuck mid-path.
- Because the pool account is a deterministically-derived, ownerless sovereign account, there is no privileged "cancel"/rescue path analogous to `OracleLess`'s admin cancel — the funds are permanently locked exactly as in the Sherlock report ("orders that is not cancelable"). This matches the Impact Gate's "permanent user-fund or bridge-state lock" category and requires no validator/collator/relayer compromise, no governance action, and no leaked keys — only an unprivileged attacker exercising a permission (`freezer`) they legitimately hold over an asset they created.

### Likelihood Explanation
High. Asset creation and pool creation are both permissionless, low-cost operations by design (intended for open DeFi use on Asset Hub). An attacker only needs to: create an asset, create/seed a pool with it (or wait for others to add liquidity to an already-existing pool containing their asset — pools are permissionless to join by any user, so an attacker can create a pool that looks legitimate and market it, or target an already popular pool if they can register their asset as one leg first), then call `block` on the pool account at a time that maximizes locked value. No special access is required beyond being the original creator/freezer of the asset, which is a routine, expected role under the permissionless-asset model.

### Recommendation
Add defensive checks in `pallet-asset-conversion`'s liquidity/swap paths mirroring what was already done in `asset-conversion-tx-payment`: before/while executing transfers out of the pool account, verify via `can_withdraw`/`can_deposit` that the relevant account is not `Blocked`/`Frozen`, and if it is, either reject the specific operation gracefully (leaving state unchanged, transactional rollback) or provide a permissionless/governed mechanism to migrate liquidity out of a bricked pool (e.g., an emergency drain to a per-LP claim ledger that does not require the frozen account to itself execute the outbound transfer, or restricting which assets can be paired in permissionless pools to those without a self-appointed freezer/admin).

### Proof of Concept
1. Attacker calls `pallet_assets::create` (permissionless on Asset Hub) to create asset `X`, becoming its `owner`/`admin`/`freezer`.
2. Attacker calls `AssetConversion::create_pool(Native, X)` (permissionless) and `add_liquidity` to seed the pool; other users are enticed to `add_liquidity` as well, growing pooled value.
3. Attacker calls `pallet_assets::block(X, pool_account)` — allowed solely because `origin == d.freezer` (`substrate/frame/assets/src/lib.rs:1882`) — no other check on whether the target account is a shared AMM pool account.
4. Any subsequent `AssetConversion::remove_liquidity` or `swap_*` call touching asset `X` in that pool fails at the `T::Assets::transfer` step because the pool account's status for asset `X` is `Blocked`.
5. All native/`X` liquidity in the pool is now permanently unrecoverable by any of the honest LPs, replicating the "uncancelable order griefing" pattern from the Sherlock report at the AMM-pool level.

### Citations

**File:** substrate/frame/assets/src/lib.rs (L1868-1892)
```rust
		#[pallet::call_index(31)]
		pub fn block(
			origin: OriginFor<T>,
			id: T::AssetIdParameter,
			who: AccountIdLookupOf<T>,
		) -> DispatchResult {
			let origin = ensure_signed(origin)?;
			let id: T::AssetId = id.into();

			let d = Asset::<T, I>::get(&id).ok_or(Error::<T, I>::Unknown)?;
			ensure!(
				d.status == AssetStatus::Live || d.status == AssetStatus::Frozen,
				Error::<T, I>::IncorrectStatus
			);
			ensure!(origin == d.freezer, Error::<T, I>::NoPermission);
			let who = T::Lookup::lookup(who)?;

			Account::<T, I>::try_mutate(&id, &who, |maybe_account| -> DispatchResult {
				maybe_account.as_mut().ok_or(Error::<T, I>::NoAccount)?.status =
					AccountStatus::Blocked;
				Ok(())
			})?;

			Self::deposit_event(Event::<T, I>::Blocked { asset_id: id, who });
			Ok(())
```

**File:** substrate/frame/asset-conversion/src/tests.rs (L2334-2391)
```rust
#[test]
fn cannot_block_pool_creation() {
	new_test_ext().execute_with(|| {
		// User 1 is the pool creator
		let user = 1;
		// User 2 is the attacker
		let attacker = 2;

		let ed = get_native_ed();
		assert_ok!(Balances::force_set_balance(RuntimeOrigin::root(), attacker, 10000 + ed));

		// The target pool the user wants to create is Native <=> WithId(2)
		let token_1 = NativeOrWithId::Native;
		let token_2 = NativeOrWithId::WithId(2);

		// Attacker computes the still non-existing pool account for the target pair
		let pool_account =
			<Test as Config>::PoolLocator::address(&(token_1.clone(), token_2.clone())).unwrap();
		// And transfers the ED to that pool account
		assert_ok!(Balances::transfer_allow_death(
			RuntimeOrigin::signed(attacker),
			pool_account,
			ed
		));
		// Then, the attacker creates 14 tokens and sends one of each to the pool account
		for i in 10..25 {
			create_tokens(attacker, vec![NativeOrWithId::WithId(i)]);
			assert_ok!(Assets::mint(RuntimeOrigin::signed(attacker), i, attacker, 1000));
			assert_ok!(Assets::transfer(RuntimeOrigin::signed(attacker), i, pool_account, 1));
		}

		// User can still create the pool
		create_tokens(user, vec![token_2.clone()]);
		assert_ok!(AssetConversion::create_pool(
			RuntimeOrigin::signed(user),
			Box::new(token_1.clone()),
			Box::new(token_2.clone())
		));

		// User has to transfer one WithId(2) token to the pool account (otherwise add_liquidity
		// will fail with `AssetTwoDepositDidNotMeetMinimum`)
		assert_ok!(Balances::force_set_balance(RuntimeOrigin::root(), user, 10000 + ed));
		assert_ok!(Assets::mint(RuntimeOrigin::signed(user), 2, user, 10000));
		assert_ok!(Assets::transfer(RuntimeOrigin::signed(user), 2, pool_account, 1));

		// add_liquidity shouldn't fail because of the number of consumers
		assert_ok!(AssetConversion::add_liquidity(
			RuntimeOrigin::signed(user),
			Box::new(token_1.clone()),
			Box::new(token_2.clone()),
			10000,
			100,
			10000,
			10,
			user,
		));
	});
}
```
