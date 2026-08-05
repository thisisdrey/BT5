### Title
Permissionless self-priced AMM pool lets an attacker pay near-zero transaction fees on a worthless self-minted asset, causing underpriced work on-chain - ([File: substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs])

### Summary
The Vader bug relies on an attacker who: (1) creates a worthless custom token, (2) mints it freely, (3) creates a thin, self-controlled AMM pool for that token, and (4) uses the pool's manipulated exchange rate as a "value oracle" that a reward/accounting system trusts at face value with no depth check. The direct on-chain analog is `pallet-asset-conversion-tx-payment`'s `SwapAssetAdapter`, which uses `pallet-asset-conversion`'s own permissionless AMM (`quote_price_tokens_for_exact_tokens` / `swap_tokens_for_exact_tokens`) as the sole price oracle for converting a transaction's native fee into "asset" units. Because pool creation, minting, and liquidity provisioning in `pallet-asset-conversion` are permissionless and unauthenticated by any curation/whitelist, an attacker can create a custom asset, seed a pool with an extreme (self-chosen) reserve ratio, and pay for transaction fees at an arbitrarily cheap price in that asset, while the chain performs full-price weight/length work.

### Finding Description
`pallet-asset-conversion` allows any signed account to `create_pool` for an arbitrary `AssetKind` pair and to `add_liquidity` in any ratio it likes (subject only to `MintMinLiquidity`), since asset creation via `pallet-assets` is also permissionless (`force_create`/normal `create`, `mint_into`, etc. are user-controlled for user-owned asset ids). There is no "curation" gate (unlike Vader's post-mortem fix requiring `isCurated(token)` with a liquidity-depth check).

`SwapAssetAdapter::withdraw_fee` (and `can_withdraw_fee`) in `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs:119-146` computes the amount of the user-chosen `asset_id` required to cover the native `fee` purely via: [1](#0-0) 
This call is answered by `AssetConversion::quote_price_tokens_for_exact_tokens`, which is a plain constant-product (`x*y=k`) quote derived only from the two reserve balances of whatever pool the attacker created and funded: [2](#0-1) 
There is no minimum-liquidity-depth guard, no slippage-based "swap value" computation analogous to Vader's fix (`calcSwapValueInBase`), and no restriction on which `AssetKind`s may be used to pay fees — any asset registered via `pallet_assets` with `is_sufficient = true` (or the sufficient-asset path) and paired into a pool qualifies.

By seeding the pool with an extremely skewed ratio (e.g. 1 unit of a self-minted "worthless" asset paired against a large amount of native currency he supplies himself, mirroring the Vader "single wei of TOKEN + 10^18 BASE" trick), the attacker inflates the *quoted price* of his own asset relative to native currency. `quote_price_tokens_for_exact_tokens` will then report that only a minuscule amount of his asset is required to cover the transaction's native-denominated fee, and `withdraw_fee`/`can_withdraw_fee` will accept that quote without question, exactly as `calcReward`/`_deposit` in the Vault accepted `calcValueInBase` at face value regardless of how the price was produced.

### Impact Explanation
This matches the required "public underpriced work that degrades block production or stalls bridge processing" impact: an attacker can submit computationally/storage-heavy transactions (subject to normal weight limits, but paid at negligible real cost) repeatedly, because the fee charged in the chosen asset is effectively decoupled from real economic value. Unlike a legitimate market-priced asset, the "price" here is entirely attacker-controlled at pool-creation time and requires no meaningful locked value (analogous to Vader's single-wei manipulation), so this is not merely paying fees in a volatile-but-real asset — it is fee payment against a self-manufactured, valueless oracle. Because transaction fees exist specifically to rate-limit and monetize block space/weight consumption, this directly enables spam/DoS-style underpricing of block resources at effectively zero attacker cost (aside from creating the asset and one addition-of-liquidity), which can degrade block production for legitimate users.

### Likelihood Explanation
Every step is achievable by an unprivileged, ordinary signed account with no special permissions, governance, or validator/relayer role: creating an asset via `pallet-assets`, creating a pool and adding liquidity via `pallet-asset-conversion`, and then dispatching transactions with `ChargeAssetTxPayment` referencing that `asset_id`. No malicious peer, prover, or admin is required — this is a pure "public entrypoint" attack path against `withdraw_fee`/`can_withdraw_fee`, precisely mirroring the unprivileged nature of the original Vader `_deposit`/`harvest` exploit. The main constraint is that the runtime must actually enable `pallet-asset-conversion-tx-payment` with `SwapAssetAdapter` and allow user-created assets/pools (true for e.g. Kitchensink `node-runtime`, which wires exactly this configuration).

### Recommendation
- Do not trust raw AMM spot/quote prices from permissionless, user-created pools as the sole basis for fee conversion. Apply a Vader-style mitigation: require a minimum liquidity depth (analogous to `isCurated`) before an asset/pool is eligible for `ChargeAssetTxPayment`, and/or compute the conversion using a slippage-aware "execution value" rather than a pure spot quote, so a thin pool cannot make an arbitrary asset artificially cheap.
- Alternatively/additionally, maintain an allow-list of `AssetId`s (governance-curated, with minimum-liquidity or price-deviation-from-reference checks) eligible for `ChargeAssetTxPayment`, instead of accepting any `AssetKind` with an existing pool.
- Consider bounding the acceptable price deviation between the AMM quote and a trusted reference price (e.g., `pallet-asset-rate`'s governance-set conversion) before allowing fee payment in a given asset.

### Proof of Concept
1. Attacker creates asset `X` via `pallet_assets::force_create`/`create`, mints a large supply to themselves.
2. Attacker calls `AssetConversion::create_pool(Native, X)`.
3. Attacker calls `AssetConversion::add_liquidity(Native, X, huge_native_amount, 1 /* 1 unit of X */, ..., attacker)` — mirroring the Vader "1 wei TOKEN + 10^18 BASE" liquidity-skew trick — establishing a reserve ratio where 1 unit of `X` quotes as worth `huge_native_amount` of native currency.
4. Attacker submits a heavy (high `weight`/`len`) extrinsic using `ChargeAssetTxPayment::from(tip, Some(X))`.
5. `SwapAssetAdapter::withdraw_fee` calls `quote_price_tokens_for_exact_tokens(X, Native, fee, true)`, which — due to the skewed reserves the attacker fully controls — returns a near-zero amount of `X` required.
6. The transaction is accepted for a fee paid almost entirely in a self-minted, valueless asset, while consuming full weight/length resources of the block — repeatable at negligible real cost, underpricing block-space work exactly as the Vault member in the original report underpriced their vault-weight deposit with a self-created synth.

### Citations

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L142-146)
```rust
		// Quote the amount of the `asset_id` needed to pay the fee in the asset `A`.
		let asset_fee =
			S::quote_price_tokens_for_exact_tokens(asset_id.clone(), A::get(), fee, true)
				.filter(|asset_fee| !asset_fee.is_zero())
				.ok_or(InvalidTransaction::Payment)?;
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1523-1547)
```rust
		pub fn quote_price_exact_tokens_for_tokens(
			asset1: T::AssetKind,
			asset2: T::AssetKind,
			amount: T::Balance,
			include_fee: bool,
		) -> Option<T::Balance> {
			// Swaps reject zero amounts, match that behavior.
			if amount.is_zero() {
				return None;
			}

			let pool_account = T::PoolLocator::pool_address(&asset1, &asset2).ok()?;

			let (balance1, balance2) = Self::get_reserves(asset1.clone(), asset2.clone()).ok()?;

			if balance1.is_zero() {
				return None;
			}

			let amount_out = if include_fee {
				let fee = Self::pool_fee_for(&asset1, &asset2).ok()?;
				Self::get_amount_out(fee, &amount, &balance1, &balance2).ok()?
			} else {
				Self::quote(&amount, &balance1, &balance2).ok()?
			};
```
