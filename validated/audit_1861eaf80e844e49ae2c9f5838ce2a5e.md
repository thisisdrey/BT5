### Title
`ProxyType::NonTransfer` fails to filter `pallet_asset_conversion` calls, allowing a non-transfer proxy delegate to move the delegator's funds to an arbitrary account - (File: `cumulus/parachains/runtimes/assets/asset-hub-rococo/src/lib.rs`, `cumulus/parachains/runtimes/assets/asset-hub-westend/src/lib.rs`, `substrate/frame/staking-async/runtimes/parachain/src/lib.rs`)

### Summary
`remove_liquidity`, `swap_exact_tokens_for_tokens`, and `swap_tokens_for_exact_tokens` in `pallet_asset_conversion` each take a caller-supplied beneficiary account (`withdraw_to` / `send_to`) that receives real value (native currency or fungible assets) and is only constrained by `ensure_signed(origin)` — there is no requirement that the beneficiary equal the signer. When these calls are dispatched through a `pallet_proxy` delegate authorized only with `ProxyType::NonTransfer`, the runtime's `InstanceFilter` implementation does not exclude `RuntimeCall::AssetConversion { .. }`, only `Balances`, `Assets`, `NftFractionalization`, `Nfts`, and `Uniques`. This lets a delegate that was explicitly restricted from moving the delegator's funds instead do so via the AssetConversion pallet: add/already-held liquidity can be pulled out to an attacker address via `remove_liquidity`, or the delegator's native/asset balance can be swapped and paid out to an attacker-controlled `send_to`.

### Finding Description
`do_remove_liquidity` burns LP tokens from `who` (the signer/proxied account) and unconditionally transfers the underlying reserves to the caller-supplied `withdraw_to` account: [1](#0-0) 

The dispatchable itself performs no restriction linking `withdraw_to` to the signer: [2](#0-1) 

Likewise, the swap extrinsics accept an arbitrary `send_to` destination for the swap proceeds while debiting the signer's holdings: [3](#0-2) [4](#0-3) 

The asset-hub proxy `InstanceFilter` for `ProxyType::NonTransfer` is designed to block a delegate from moving the delegator's value, but it enumerates only `Balances`, `Assets`, `NftFractionalization`, `Nfts`, and `Uniques` — `AssetConversion` is absent from the exclusion list, so it is allowed under `NonTransfer`: [5](#0-4) 

The same pattern (missing `AssetConversion` exclusion) is present in `cumulus/parachains/runtimes/assets/asset-hub-westend/src/lib.rs` and `substrate/frame/staking-async/runtimes/parachain/src/lib.rs`, whose `NonTransfer` definitions are structurally identical. [6](#0-5) 

Because `pallet_proxy` executes the wrapped call with the delegator's own `AccountId` as the dispatch origin (not the delegate's), any call that AssetConversion allows through unchanged reaches `do_remove_liquidity`/`do_swap_*` with `who = delegator`. If the delegator holds LP tokens, a pool asset balance, or native currency, a delegate restricted to `NonTransfer` can:
- Call `AssetConversion::remove_liquidity(asset1, asset2, lp_token_burn, 0, 0, attacker)` to redeem the delegator's LP position straight to an attacker account, or
- Call `AssetConversion::swap_exact_tokens_for_tokens([native_or_asset, other_asset], amount_in, 0, attacker, false)` to convert and drain the delegator's native/asset balance to an attacker account.

This is exactly the composition pattern flagged by the review's pivot: a public wrapper (`proxy`) must not widen origin or bypass filters for value-moving calls, and here the filter's intended invariant ("NonTransfer cannot move my funds") is broken by an unlisted pallet that internally performs equivalent transfers with an attacker-chosen beneficiary.

### Impact Explanation
Any account that granted a `NonTransfer` proxy (a common low-trust delegation, e.g., for automation/bots) to another account is exposed to theft of native currency, other fungible assets, and LP positions held in pools registered with `pallet_asset_conversion`, via calls the proxy filter was supposed to block. This is direct theft of user funds through an authorization/filter bypass, matching the "unauthorized execution / wrongful asset movement" impact category.

### Likelihood Explanation
Exploitation requires only that the victim has granted a `NonTransfer` proxy and holds LP tokens or a balance in an asset paired in an AssetConversion pool — no privileged action, race, or governance interaction needed. The delegate simply submits a normal signed `Proxy::proxy` call wrapping `AssetConversion::remove_liquidity` or a swap call with itself (or a third party) as the beneficiary. This is trivially reproducible and requires no timing or infrastructure access.

### Recommendation
Add `RuntimeCall::AssetConversion { .. }` (or more precisely, exclude the calls that accept an arbitrary beneficiary/destination: `remove_liquidity`, `swap_exact_tokens_for_tokens`, `swap_tokens_for_exact_tokens`, and possibly `add_liquidity`'s `mint_to`) to the `ProxyType::NonTransfer` filter's exclusion list in all affected runtimes (`asset-hub-rococo`, `asset-hub-westend`, `staking-async` parachain runtime, and any other runtime including `pallet_asset_conversion` with a similarly defined `NonTransfer`). Alternatively, restrict AssetConversion to only allow beneficiaries equal to the signer under `NonTransfer`-equivalent restrictions, or grant AssetConversion-touching calls only to `ProxyType::Any`/dedicated proxy types.

### Proof of Concept
1. Victim account `V` holds LP tokens for pool `(Native, Asset(2))` and grants `Proxy::add_proxy(delegate: D, proxy_type: NonTransfer, delay: 0)`.
2. Attacker controls `D` and submits:
```
Proxy::proxy(
  real: V,
  force_proxy_type: Some(NonTransfer),
  call: AssetConversion::remove_liquidity(
      asset1: Native, asset2: Asset(2),
      lp_token_burn: <V's LP balance>,
      amount1_min_receive: 0, amount2_min_receive: 0,
      withdraw_to: Attacker,
  ),
)
```
3. `ProxyType::NonTransfer::filter` returns `true` for this call because `RuntimeCall::AssetConversion` is not in its exclusion list (see cited match arms), so `pallet_proxy` dispatches `do_remove_liquidity` with origin `V`.
4. `do_remove_liquidity` burns `V`'s LP tokens and transfers both underlying assets directly to `Attacker`, confirmed by the unconditional `T::Assets::transfer(asset1/2, &pool_account, withdraw_to, amount, Expendable)` calls — despite `V` having authorized only a `NonTransfer` proxy.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L497-517)
```rust
		pub fn remove_liquidity(
			origin: OriginFor<T>,
			asset1: Box<T::AssetKind>,
			asset2: Box<T::AssetKind>,
			lp_token_burn: T::Balance,
			amount1_min_receive: T::Balance,
			amount2_min_receive: T::Balance,
			withdraw_to: T::AccountId,
		) -> DispatchResult {
			let sender = ensure_signed(origin)?;
			Self::do_remove_liquidity(
				&sender,
				*asset1,
				*asset2,
				lp_token_burn,
				amount1_min_receive,
				amount2_min_receive,
				&withdraw_to,
			)?;
			Ok(())
		}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L527-545)
```rust
		pub fn swap_exact_tokens_for_tokens(
			origin: OriginFor<T>,
			path: Vec<Box<T::AssetKind>>,
			amount_in: T::Balance,
			amount_out_min: T::Balance,
			send_to: T::AccountId,
			keep_alive: bool,
		) -> DispatchResult {
			let sender = ensure_signed(origin)?;
			Self::do_swap_exact_tokens_for_tokens(
				sender,
				path.into_iter().map(|a| *a).collect(),
				amount_in,
				Some(amount_out_min),
				send_to,
				keep_alive,
			)?;
			Ok(())
		}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L555-573)
```rust
		pub fn swap_tokens_for_exact_tokens(
			origin: OriginFor<T>,
			path: Vec<Box<T::AssetKind>>,
			amount_out: T::Balance,
			amount_in_max: T::Balance,
			send_to: T::AccountId,
			keep_alive: bool,
		) -> DispatchResult {
			let sender = ensure_signed(origin)?;
			Self::do_swap_tokens_for_exact_tokens(
				sender,
				path.into_iter().map(|a| *a).collect(),
				amount_out,
				Some(amount_in_max),
				send_to,
				keep_alive,
			)?;
			Ok(())
		}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L941-952)
```rust
			// burn the provided lp token amount that includes the fee
			T::PoolAssets::burn_from(
				pool.lp_token.clone(),
				who,
				lp_token_burn,
				Expendable,
				Exact,
				Polite,
			)?;

			T::Assets::transfer(asset1, &pool_account, withdraw_to, amount1, Expendable)?;
			T::Assets::transfer(asset2, &pool_account, withdraw_to, amount2, Expendable)?;
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-rococo/src/lib.rs (L612-623)
```rust
impl InstanceFilter<RuntimeCall> for ProxyType {
	fn filter(&self, c: &RuntimeCall) -> bool {
		match self {
			ProxyType::Any => true,
			ProxyType::NonTransfer => !matches!(
				c,
				RuntimeCall::Balances { .. } |
					RuntimeCall::Assets { .. } |
					RuntimeCall::NftFractionalization { .. } |
					RuntimeCall::Nfts { .. } |
					RuntimeCall::Uniques { .. }
			),
```

**File:** substrate/frame/staking-async/runtimes/parachain/src/lib.rs (L680-687)
```rust
			ProxyType::NonTransfer => !matches!(
				c,
				RuntimeCall::Balances { .. } |
					RuntimeCall::Assets { .. } |
					RuntimeCall::NftFractionalization { .. } |
					RuntimeCall::Nfts { .. } |
					RuntimeCall::Uniques { .. }
			),
```
