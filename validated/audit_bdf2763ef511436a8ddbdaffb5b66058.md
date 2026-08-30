### Title
Interest-rate utilization curve does not account for the absolute `cap-debt` limit, making high-utilization segments of the rate curve permanently unreachable - (File: `mainnet/contracts/vault/v0-vault-stx.clar` and identical `v0-vault-*.clar` vaults)

### Summary
Each Zest vault (`v0-vault-stx.clar`, `v0-vault-sbtc.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`, `v0-vault-usdc.clar`, `v0-vault-usdh.clar`) computes the utilization used to price interest as `debt / (debt + available-liquidity)` (i.e. `debt / total-assets`), while `borrow`/`system-borrow` independently enforces a separate, absolute `cap-debt` ceiling. Because `cap-debt` is not factored into the utilization formula, whenever `cap-debt < total-assets` the vault's actual maximum achievable utilization is capped at `cap-debt / total-assets`, permanently below 100% (`BPS`). Any interest-rate curve segments configured above that ratio (e.g., the kink/high-utilization slope meant to protect LPs and throttle borrowing near depletion) can never be reached, exactly mirroring the Teller `getPoolUtilizationRatio` bug where `liquidityThresholdPercent` was omitted from the utilization math.

### Finding Description
`calc-utilization` computes utilization purely from actual outstanding debt versus total vault assets: [1](#0-0) 

This is used directly to look up the borrow rate via `interpolate-rate`/`resolve-and-interpolate` against the DAO-configured utilization/rate curve points (`points-ir`): [2](#0-1) 

Separately, each vault enforces a hard, absolute debt ceiling `cap-debt` (a `uint` token amount, not a percentage) in `system-borrow`: [3](#0-2) 

`cap-debt` is a plain data-var, set by governance independently of `cap-supply`/total assets: [4](#0-3) 

The read-only data layer (`v0-1-data.clar`) even acknowledges that `cap-debt` restricts real borrowable liquidity below what raw available liquidity would suggest, computing a separate "cap-aware" `available-to-borrow`: [5](#0-4) 

However, this cap-awareness exists only in the display/data helper — it is never fed back into `calc-utilization`/`interest-rate` inside the vault contracts themselves. Consequently, once `debt` reaches `cap-debt` (the true maximum borrowable amount), the vault is "fully utilized" in the economically meaningful sense (no more borrowing is possible), yet `calc-utilization` still reports `cap-debt / (cap-debt + available-liquidity)`, which is strictly less than `BPS` (100%) whenever `cap-debt < total-assets`. Any curve points configured for utilization above that ratio (typically the steep "kink" segment designed to sharply raise rates and reward LPs as the pool nears exhaustion) become permanently unreachable — identical in nature to the Teller finding where utilization above `liquidityThresholdPercent` was unreachable because the threshold was excluded from the ratio calculation.

### Impact Explanation
Because the borrow-rate curve's high-utilization segment is unreachable whenever `cap-debt` is below total vault assets, LPs never receive the higher yield the curve was designed to pay once the pool is effectively maxed out (borrowing is blocked by `ERR-DEBT-CAP-EXCEEDED` well before the priced-in high-utilization APY kicks in). Supply APY is derived directly from this same capped utilization value in the data layer (`calc-supply-apy`): [6](#0-5) 

This results in LPs permanently under-earning relative to the intended rate model whenever a debt cap is configured below the supply cap — a persistent under-realization of yield that LPs are otherwise entitled to under the protocol's own interest-rate design (temporary/permanent freezing of unclaimed yield for LPs), matching the in-scope "interest accrual" vault math category.

### Likelihood Explanation
This triggers automatically, with no attacker action required, any time governance sets `cap-debt` strictly less than the vault's total assets/`cap-supply` (a normal, expected configuration used to control per-asset risk exposure) and utilization organically rises to the cap through ordinary borrowing by unprivileged users. No DAO compromise is needed — only a legitimately configured, conservative `cap-debt`, which is a common and expected operational parameter choice.

### Recommendation
Include `cap-debt` in the utilization computation used for interest-rate pricing, analogous to the Teller fix. For example, cap the denominator (or scale utilization) by `min(total-assets, cap-debt-implied ceiling)` so that `debt == cap-debt` maps to 100% utilization:
```
utilization = debt * BPS / max-effective-liquidity
```
where `max-effective-liquidity = min(total-assets, cap-debt + (total-assets - debt))`-equivalent logic ensures `debt == cap-debt` yields `BPS` (100%), letting the full configured rate curve (including its high-utilization/kink segment) be reachable.

### Proof of Concept
1. Governance deploys `v0-vault-usdc` with `cap-supply = 1,000,000 USDC` and `cap-debt = 600,000 USDC` (a common conservative risk parameter, set via `set-cap-debt`/DAO proposal as in `v0-init.clar`).
2. LPs deposit up to `cap-supply` (1,000,000 USDC) via `deposit`.
3. Borrowers call `borrow`/`system-borrow` until `total-borrowed == cap-debt == 600,000`; further borrows revert with `ERR-DEBT-CAP-EXCEEDED` (`mainnet/contracts/vault/v0-vault-usdc.clar:879`-ish, mirrored across vaults at the `system-borrow` `asserts!` shown above).
4. At this point the vault is at its true maximum utilization (no more debt can ever be taken), but `calc-utilization` reports `600,000 * 10000 / 1,000,000 = 6000` (60%), not `10000` (100%).
5. If the DAO configured `points-ir` with a kink at, say, 80% utilization to sharply raise the borrow rate (and thus supply APY) as the pool nears exhaustion, that segment is permanently unreachable — LPs are stuck earning the pre-kink rate indefinitely despite the pool being fully committed, verifiable by comparing `get-utilization`/`get-interest-rate` outputs against the configured `points-ir` in `v0-vault-usdc.clar`.

### Citations

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L164-168)
```text
(define-private (calc-utilization (available-liquidity uint) (debt-amount uint))
  (let ((total (+ debt-amount available-liquidity)))
    (if (is-eq total u0)
        u0
        (mul-div-down debt-amount BPS total))))
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L82-83)
```text
(define-data-var cap-debt uint u0)
(define-data-var cap-supply uint u0)
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L368-377)
```text
(define-private (utilization)
  (calc-utilization (get-available-assets) (total-debt)))

(define-private (interest-rate)
  (let ((points-data (var-get points-ir))
        (uword (get util points-data))
        (rword (get rate points-data))
        (utils (unpack-u16 uword))
        (rates (unpack-u16 rword)))
    (interpolate-rate (utilization) utils rates)))
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L865-881)
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
```

**File:** mainnet/contracts/utility/v0-1-data.clar (L303-307)
```text
;; Supply APY = borrow_rate x utilization x (1 - reserve_fee / BPS)
(define-private (calc-supply-apy (borrow-rate uint) (utilization uint) (reserve-fee uint))
  (let ((util-applied (mul-bps-down borrow-rate utilization))
        (fee-factor (- BPS reserve-fee)))
    (mul-bps-down util-applied fee-factor)))
```

**File:** mainnet/contracts/utility/v0-1-data.clar (L320-333)
```text
;; -- Build single reserve data ----------------------------------------------

(define-private (build-reserve-data (vid uint))
  (let ((borrow-apy (get-vault-interest-rate vid))
        (utilization (get-vault-utilization vid))
        (fee-reserve (get-vault-fee-reserve vid))
        (supply-apy (calc-supply-apy borrow-apy utilization fee-reserve))
        (total-borrowed (get-vault-debt vid))
        (cap-debt (get-vault-cap-debt vid))
        (available-liquidity (get-vault-available-liquidity vid))
        ;; Cap-aware borrowable: min of liquidity and remaining debt cap
        (remaining-cap (if (> cap-debt total-borrowed) (- cap-debt total-borrowed) u0))
        (available-to-borrow (min available-liquidity remaining-cap)))
    {
```
