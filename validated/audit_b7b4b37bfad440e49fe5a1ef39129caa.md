### Title
`market.repay()` can revert on legitimate calls when `amount-to-repay` reaches zero due to unconditional zero-value token transfer in vault `system-repay` - (File: `mainnet/contracts/market/v0-4-market.clar`, `mainnet/contracts/vault/v0-vault-*.clar`)

### Summary
`market.repay()` computes `amount-to-repay` from a scaled-debt calculation and passes it unconditionally to the vault's `system-repay`, which unconditionally calls `receive-underlying` (an ERC20/SIP-010-style `transfer`). If the computed `amount-to-repay` (or, in the vault, `capped-amount`) resolves to zero while the outer, user-supplied `amount` is still non-zero, a token that reverts on zero-value transfers will cause the whole repay to fail, mirroring the reported "collect module fails on zero-value transfer" bug class.

### Finding Description
In `market.repay` (`mainnet/contracts/market/v0-4-market.clar:1316-1378`), the repay amount actually sent to the vault is derived through several roundings:
```
max-repay-tokens (mul-div-up account-scaled-debt borrow-index INDEX-PRECISION)
safe-amount (min amount max-repay-tokens)
scaled-debt-repayment (mul-div-down safe-amount INDEX-PRECISION borrow-index)
repaid-scaled-debt (min account-scaled-debt scaled-debt-repayment)
amount-to-repay (mul-div-up repaid-scaled-debt borrow-index INDEX-PRECISION)
``` [1](#0-0) 

The only pre-condition checked before calling the vault is `(> amount u0)` and `(> repaid-scaled-debt u0)` — there is no check that `amount-to-repay` itself is non-zero: [2](#0-1) 

`vault-system-repay` then unconditionally calls the vault's `system-repay`: [3](#0-2) 

Inside the vault (e.g. `v0-vault-stx.clar`), `system-repay` recomputes a `capped-amount` from the passed-in `amount` and vault-side `debt`:
```
capped-amount (if (> amount debt) debt amount)
```
and unconditionally transfers it:
```
(try! (receive-underlying capped-amount tx-sender))
``` [4](#0-3) 

`receive-underlying` performs an unconditional SIP-010/ERC20-style `transfer` call with no zero-amount guard: [5](#0-4) 

Because `capped-amount` is only guarded by `(> amount u0)` (the market's `amount-to-repay`, not the vault-internal recomputed `capped-amount`), if the vault-cached `debt` for that asset (via the market's per-block index cache) rounds down to `u0` — e.g. extremely small dust residual positions, or rounding edge cases between the market's scaled-debt bookkeeping and the vault's own `total-debt()` computation using its independently tracked `principal-scaled`/`index` — `capped-amount` can be `u0` while the outer `amount` parameter passed by the market is `> 0`. The call then attempts a zero-value `transfer` on the underlying SIP-010 token. All vault contracts (`v0-vault-stx`, `v0-vault-sbtc`, `v0-vault-ststx`, `v0-vault-usdc`, `v0-vault-usdh`, `v0-vault-ststxbtc`) share this exact pattern, so any underlying token contract that reverts on zero-value transfers (a documented weird-ERC20/SIP-010 behavior class) will cause `system-repay` — and therefore the user-facing `market.repay()` — to revert entirely, denying the ordinary user the ability to repay their debt.

This is the direct structural analog of the referenced Aave-Lens finding: a derived, rounded/capped sub-amount is transferred without a `> 0` guard, even though the top-level caller-supplied amount was checked to be non-zero.

### Impact Explanation
This lands in the **High** impact bucket — temporary freezing of funds: if the underlying token used by a vault reverts on zero-value transfers, users with debt whose vault-side `capped-amount` resolves to zero (due to rounding divergence between market and vault debt bookkeeping) would be unable to call `repay()` for that asset, temporarily freezing their ability to close out debt positions and potentially preventing them from avoiding liquidation on that market. It does not directly cause fund loss but denies core repay functionality, an unprivileged-principal-reachable market entry point.

### Likelihood Explanation
Likelihood is low-to-moderate: it requires (a) the underlying token used for the affected vault to revert on zero-value transfers, and (b) a rounding/timing scenario where the vault's independently-tracked `total-debt()` (using vault `principal-scaled`/`index`) diverges to zero for a wei/µunit amount while the market's own scaled-debt bookkeeping still reports non-zero. This can realistically occur on the final repay of dust-level residual debt, or through timing differences between market-side and vault-side accrual if a user's debt is repaid down to sub-unit remainder amounts across two systems that separately round. None of the currently deployed underlying tokens (STX/wSTX, sBTC, stSTX, USDC, USDH, stSTXbtc) are confirmed here to revert on zero-value transfers, so the practical likelihood depends on future underlying token integrations or edge-case dust conditions, but the code path itself provides no such guard today.

### Recommendation
Add an explicit zero-amount guard immediately before the unconditional token transfer inside each vault's `system-repay` (and symmetrically in `market.repay` before invoking `vault-system-repay`), e.g.:
```clarity
(if (> capped-amount u0)
    (try! (receive-underlying capped-amount tx-sender))
    true)
```
and skip the corresponding bookkeeping deltas cleanly when `capped-amount` is `u0`, rather than reverting the whole repay transaction. Apply the same defensive check to any other unconditional `send-underlying`/`receive-underlying` call sites in the vault contracts that are fed by internally re-derived amounts rather than the already-validated top-level `amount`.

### Proof of Concept
1. A user has an outstanding debt scaled-balance that, when converted via the vault's own `total-debt()` (using vault-local `principal-scaled` and `index`, which can diverge slightly from the market's scaled-debt bookkeeping due to independent rounding/accrual timing), computes to `u0` micro-units of underlying debt, while the market-side `account-scaled-debt` for that asset is still `> 0`.
2. The user calls `market.repay(ft, amount, none)` with `amount > 0`. The market computes `amount-to-repay > 0` based on market-side scaled debt and calls `vault-system-repay`.
3. Inside the target vault's `system-repay`, `debt` (vault's own `total-debt()`) is computed as `u0` for this edge case, so `capped-amount = (if (> amount debt) debt amount) = u0`.
4. `(try! (receive-underlying u0 tx-sender))` executes a zero-value `transfer` call on the underlying SIP-010 token contract.
5. If that token contract reverts on transfers of amount `0` (a known behavior class for some tokens), the entire `system-repay` call reverts, and consequently `market.repay()` reverts — even though the user supplied a valid non-zero `amount` and holds real debt from the market's perspective.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L1332-1345)
```text
        ;; Step 3: Get account debt FIRST to enable safe amount capping
        (account-scaled-debt (get-account-scaled-debt account asset-id))
        
        ;; Step 4: Calculate max repayable amount (actual debt in token), mul-div-up for safe upper bound
        (max-repay-tokens (mul-div-up account-scaled-debt borrow-index INDEX-PRECISION))
        
        ;; Step 5: Cap input amount at actual debt - prevents overflow in scaled calculation
        (safe-amount (min amount max-repay-tokens))
        
        ;; Step 6: Convert to scaled debt (amount is bounded)
        (scaled-debt-repayment (mul-div-down safe-amount INDEX-PRECISION borrow-index))

        (repaid-scaled-debt (min account-scaled-debt scaled-debt-repayment))
        (amount-to-repay (mul-div-up repaid-scaled-debt borrow-index INDEX-PRECISION))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1350-1355)
```text
    ;; preconditions
    (asserts! (is-eq contract-caller tx-sender) ERR-AUTHORIZATION)
    (asserts! (> amount u0) ERR-AMOUNT-ZERO)
    (asserts! (> repaid-scaled-debt u0) ERR-INSUFFICIENT-SCALED-DEBT)

    (try! (vault-system-repay asset-id amount-to-repay ft address))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1381-1378)
```text

```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L291-294)
```text
(define-private (receive-underlying (amount uint) (account principal))
  (begin
    (try! (contract-call? .wstx transfer amount account current-contract none))
    (ok true)))
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L908-924)
```text
        (debt (total-debt))
        (total-borrowed-amount (var-get total-borrowed))
        (capped-amount (if (> amount debt) debt amount))
        (principal-reduction (calc-principal-ratio-reduction capped-amount scaled-principal debt))
        (capped-reduction (if (> principal-reduction scaled-principal) scaled-principal principal-reduction))
        (updated-scaled-principal (- scaled-principal capped-reduction))
        (principal-repaid (mul-div-down capped-amount total-borrowed-amount debt))
        (interest-paid (- capped-amount principal-repaid))
        (total-borrowed-new (if (> total-borrowed-amount principal-repaid) (- total-borrowed-amount principal-repaid) u0)))

    (try! (check-caller-auth))
    (asserts! (not (get repay states)) ERR-PAUSED)
    (asserts! (> amount u0) ERR-AMOUNT-ZERO)

    (try! (receive-underlying capped-amount tx-sender))
    (var-set principal-scaled updated-scaled-principal)
    (var-set total-borrowed total-borrowed-new)
```
