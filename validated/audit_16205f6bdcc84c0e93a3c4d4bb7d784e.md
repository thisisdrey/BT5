This confirms `borrow` is a normal, unprivileged entry point requiring only `> amount u0`, healthy collateral, and no per-egroup borrow-disable flag — any account with any collateral can call it and pull liquidity out of a shared vault. Combined with the vault's `redeem`/`system-borrow` liquidity checks and the zero-interest-in-same-block accrual behavior, this reproduces the Particle NFT-withdrawal-grief pattern using fungible-liquidity griefing instead of a physical NFT.

### Title
Vault liquidity griefing via sandwiched borrow/repay blocks victim redemptions at near-zero cost - (File: `mainnet/contracts/vault/v0-vault-usdc.clar`, `mainnet/contracts/market/v0-4-market.clar`)

### Summary
Any unprivileged principal can call `market.borrow()` to pull nearly all of a vault's available liquidity into their own control immediately before a victim's `redeem` / `collateral-remove-redeem` transaction executes, causing the victim's transaction to revert with `ERR-INSUFFICIENT-LIQUIDITY`, then call `market.repay()` in the same block to reclaim their funds essentially interest-free. This mirrors the Particle Exchange NFT-withdrawal grief (sandwiching a victim's withdraw with a cheap borrow+return), just applied to Zest's shared fungible liquidity pools instead of a single NFT.

### Finding Description
`redeem` in each vault contract (e.g. `v0-vault-usdc.clar`) enforces a liquidity check before releasing underlying assets: [1](#0-0) 

`available-assets` is the same shared pool that `system-borrow` (invoked from `market.borrow`) depletes: [2](#0-1) 

`market.borrow()` is callable by any principal with sufficient collateral and health — it is not permission-gated the way `flashloan` is (`flashloan` requires whitelisting, `borrow` does not): [3](#0-2) 

Interest accrual is index-based on `stacks-block-time` delta; when `time-delta` is `u0` (same block), the multiplier is exactly `INDEX-PRECISION`, i.e., no interest accrues: [4](#0-3) 

An attacker can therefore, within a single block:
1. `borrow()` the vault's `available-assets` down to just below the amount the victim intends to `redeem` / `collateral-remove-redeem`.
2. Let the victim's transaction hit `ERR-INSUFFICIENT-LIQUIDITY` in `redeem` (line 815 pattern above) and revert.
3. `repay()` the borrowed amount back in the same block, paying zero (or negligible rounding-up) interest, exactly as in the Particle case where `payableInterest` was zero because the loan time was zero.

This is the same root cause as the referenced Particle finding: a shared, mutable resource (there: the NFT's contract residency; here: `available-assets`/vault liquidity) gates another user's withdrawal, and the resource can be cheaply and momentarily monopolized by any third party using ordinary, non-privileged entry points (`borrow`/`repay`), with the temporary lock reversible at ~zero cost.

### Impact Explanation
This is a temporary-freezing-of-funds griefing vector: a victim's legitimate `redeem` or `collateral-remove-redeem` call can be made to revert repeatedly by any attacker willing to pay only gas (plus negligible rounding-up interest from `mul-div-up`), denying the victim timely access to their own liquid funds. It does not by itself cause theft or permanent loss, matching the "temporary freezing of funds" High-impact category rather than Critical.

### Likelihood Explanation
Likelihood is not trivial to weaponize into an automatic profit, similar to the disputed Particle finding, but the attack requires no special privileges, no DAO action, and no oracle manipulation — only calling `borrow` then `repay` on a public vault around the victim's transaction in the same block (mempool-visible), making it feasible for any watching bot, especially against low-liquidity vaults (e.g., `USDH`, `stSTXbtc`) where a victim's redemption size is close to `available-assets`.

### Recommendation
Consider decoupling per-block liquidity headroom reservations from instantaneous `available-assets`, e.g., by not allowing a single-block borrow+repay round-trip to fully saturate `available-assets` against pending redemptions, or by applying a minimum holding-period/fee on borrow-then-repay-in-same-block flows so griefing has a non-negligible cost, mirroring how the original report's suggested mitigation (disallow instant round-trip with zero cost) was intended to close the incentive-free grief window.

### Proof of Concept
1. Alice supplies USDC to `vault-usdc` and holds `zUSDC` as (rehypothecated) collateral; she calls `collateral-remove-redeem` intending to redeem `X` USDC.
2. Attacker observes Alice's pending transaction, front-runs it with `market.borrow(usdcToken, available-assets - X + 1, attacker, none)` — permitted since attacker only needs sufficient collateral of their own [5](#0-4) .
3. Alice's `collateral-remove-redeem` → `vault-redeem` call now fails `(asserts! (>= available-assets inkind) ERR-INSUFFICIENT-LIQUIDITY)` [6](#0-5) .
4. Attacker back-runs with `market.repay(usdcToken, borrowed-amount, none)` in the same block; since `next-index` uses `time-delta = 0` → multiplier `INDEX-PRECISION` [4](#0-3) , the attacker pays back essentially the exact borrowed principal with no interest.
5. Repeat every block Alice retries, at negligible cost to the attacker, denying Alice access to her funds.

### Citations

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L799-817)
```text
    (account contract-caller)
    (current-assets (var-get assets))
    (balance (get-balance-internal account))
    (balance-check (asserts! (>= balance amount) ERR-INSUFFICIENT-BALANCE))
    (available-assets (get-available-assets))
    (inkind (convert-to-assets-preview amount)))

  (asserts! (>= current-assets inkind) ERR-INSUFFICIENT-ASSETS)
  (asserts! (not (get redeem states)) ERR-PAUSED)
  (asserts! (> amount u0) ERR-AMOUNT-ZERO)
  (asserts! (> inkind u0) ERR-OUTPUT-ZERO)
  (asserts! (>= inkind min-out) ERR-SLIPPAGE)
  (asserts! (>= available-assets inkind) ERR-INSUFFICIENT-LIQUIDITY)

  (try! (ft-burn? zft amount account))
  (try! (send-underlying inkind recipient))
  (var-set assets (- current-assets inkind))

  (print {
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L863-884)
```text
(define-public (system-borrow (amount uint) (receiver principal))
  (let (
      (states (var-get pause-states))
      (u (try! (accrue)))
      (CAP-DEBT (var-get cap-debt))
      (available-assets (get-available-assets))
      (scaled-principal (var-get principal-scaled))
      (idx (var-get index))
      (debt (total-debt))
      (scaled-amount (mul-div-up amount INDEX-PRECISION idx))
      (updated-scaled-principal (+ scaled-principal scaled-amount)))

    (try! (check-caller-auth))
    (asserts! (not (get borrow states)) ERR-PAUSED)
    (asserts! (> amount u0) ERR-AMOUNT-ZERO)
    (asserts! (<= amount available-assets) ERR-INSUFFICIENT-VAULT-LIQUIDITY)
    (asserts! (<= (+ debt amount) CAP-DEBT) ERR-DEBT-CAP-EXCEEDED)

    (var-set principal-scaled updated-scaled-principal)
    (var-set total-borrowed (+ (var-get total-borrowed) amount))
    (try! (send-underlying amount receiver))

```

**File:** mainnet/contracts/market/v0-4-market.clar (L1269-1289)
```text
    ;; preconditions
    (asserts! (> amount u0) ERR-AMOUNT-ZERO)
    (asserts! (get debt asset) ERR-BORROW-DISABLED)
    (asserts! (is-healthy collateral-value debt-value current-ltvb) ERR-UNHEALTHY)

    ;; Calculate FUTURE debt (after adding this debt)
    ;; For debt: bit position = asset-id + 64 (DEBT-OFFSET)
    (let ((future-mask (bit-or mask (pow u2 (+ asset-id DEBT-OFFSET))))
          (future-group (try! (get-egroup future-mask)))
          ;; Per-egroup borrow disable check (uses FUTURE egroup, not current)
          ;; Each bit in BORROW-DISABLED-MASK corresponds to a debt asset ID (NOT offset by 64)
          (disabled-borrow-mask (get BORROW-DISABLED-MASK future-group))
          (debt-increase (try! (get-asset-value asset amount true)))
          (debt-post-increased (+ debt-value debt-increase)))

    ;; Check if this specific asset is disabled for borrowing in the FUTURE egroup
    (asserts! (is-eq (bit-and disabled-borrow-mask (pow u2 asset-id)) u0) ERR-EGROUP-ASSET-BORROW-DISABLED)
    ;; postconditions
    (asserts! (try! (is-healthy-with-mask collateral-value debt-post-increased future-mask)) ERR-UNHEALTHY)

    (try! (vault-system-borrow asset-id amount funds-receiver))
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L379-390)
```text
(define-private (next-index)
  (let ((states (var-get pause-states))
        (idx (var-get index)))
    (if (get accrue states)
        idx
        (let (
            (rate (interest-rate))
            (time-delta (- stacks-block-time (var-get last-update)))
            (multiplier (if (is-eq time-delta u0)
                          INDEX-PRECISION
                          (calc-multiplier-delta rate time-delta true))))
          (calc-index-next idx multiplier)))))
```
