### Title
Self-liquidation lets an underwater borrower cap their own loss and shift the shortfall to LPs via bad-debt socialization - ([File: mainnet/contracts/market/v0-4-market.clar])

### Summary
`liquidate` in `v0-4-market.clar` has no restriction preventing `borrower == liquidator`, and no restriction on who receives the seized collateral (`collateral-receiver` defaults to `liquidator`). Once a position is unhealthy (`current-ltv >= ltv-liq-partial`), the borrower can call `liquidate` on their own position, pay only the debt amount that their remaining collateral can cover (capped and recalculated proportionally), take that collateral back for themselves, and let any un-covered debt be written off via `socialize-debt-asset`, which is absorbed by the LP vault instead of by the borrower.

### Finding Description
`liquidate` computes an expected collateral seizure with a liquidator bonus, then caps it to what the borrower actually has, and *recalculates the debt actually repaid downward* to match the capped collateral: [1](#0-0) .

Key mechanics:
- `process-collateral-asset` computes `coll-expected` from the requested debt (with the liquidator bonus baked in) and caps `coll-actual` at the borrower's actual balance when insufficient: [2](#0-1) .
- `calc-final-liquidation-amounts` then recomputes `debt-final` proportionally downward when collateral was capped, i.e. the caller repays *less* than they requested/owe when their collateral cannot cover the full penalty-adjusted amount: [3](#0-2) .
- The only authorization checks are that `liquidate` is not paused and that `contract-caller == tx-sender`; nothing prevents `liquidator == borrower`, and `collateral-receiver` defaults to the liquidator (i.e., the borrower can direct the seized collateral back to themselves): [4](#0-3) .
- When the position's collateral is fully exhausted this way, the outstanding debt remainder is written off via `socialize-debt-asset`, which calls into the vault's `socialize-debt`, permanently reducing the vault's recorded assets (`lindex` write-down) — i.e., LPs absorb the loss: [5](#0-4)  and [6](#0-5) .

Compare this to a normal `repay`: the borrower must supply the *entire* debt amount 1:1 in debt tokens to reduce their obligation — there is no proportional discount and no path to unilaterally walk away from a shortfall. Self-liquidation, by contrast, lets the borrower "close" their own underwater position by paying only what their collateral (at the penalty-adjusted rate) can cover, reclaim that collateral for themselves, and force the protocol/LPs to socialize the rest as bad debt — this is a direct analog of the reported behavior ("if collateral is worth 100 but full debt repayment requires collateral worth 110, the user self-liquidates to reduce their loss").

### Impact Explanation
This shifts a borrower's personal shortfall onto liquidity providers of the debt vault. Every self-liquidation of this kind permanently reduces the vault's `lindex`/`total-assets`, socializing a loss that would otherwise have been the borrower's sole responsibility (either through full repayment or through a third-party liquidator absorbing/keeping the discount instead of the borrower). This constitutes a permanent, protocol-absorbed loss transferred to LPs — i.e., protocol insolvency / permanent freezing (dilution) of LP funds, rather than a mechanism failure limited to the borrower alone.

### Likelihood Explanation
Likelihood is low-to-moderate: it requires the borrower's position to already be unhealthy (`current-ltv >= ltv-liq-partial`) with collateral value insufficient to cover the full penalty-adjusted debt (e.g., after a sharp price drop). This is the same precondition window the original report describes, and it is entirely triggerable by an ordinary borrower calling `liquidate` on their own account with no special privileges needed — no oracle manipulation or third-party compromise required.

### Recommendation
Add an explicit check in `liquidate` (and `liquidate-multi`/`liquidate-redeem`) disallowing `liquidator == borrower`, or restrict this self-liquidation path (where the position ends with `no-collateral-left`/bad-debt socialization and `collateral-receiver` resolves back to the borrower) to privileged/keeper addresses, consistent with the original report's recommendation.

### Proof of Concept
1. Borrower deposits sBTC collateral and borrows USDC up to near max LTV.
2. sBTC price crashes such that `current-ltv >= ltv-liq-partial` and the collateral value, once the liquidation-penalty is applied, is less than the outstanding debt (e.g., collateral now worth $100 in liquidation terms vs. $110 required to fully clear the debt with penalty) — same setup used in the codebase's own bad-debt test at [7](#0-6) .
3. Borrower (as `contract-caller`/`tx-sender`) calls `liquidate` on themselves as `borrower`, with `debt-ft`/`collateral-ft` matching their own position and `collateral-receiver` left as `none` (defaults to themselves).
4. `debt-final` is capped/recalculated down to what their remaining collateral can cover per `calc-final-liquidation-amounts`, they receive their own `coll-final` collateral back, and the uncovered remainder of their debt is socialized into the vault via `socialize-debt-asset`/`socialize-debt`, permanently reducing vault assets instead of remaining as the borrower's obligation.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L811-829)
```text
                           found found
                           ;; Not found (disabled): resolve price on demand
                           (let ((oracle-data (get oracle coll-asset))
                                 (price (unwrap-panic (price-resolve oracle-data))))
                             (merge coll-asset { price: price }))))
        (coll-price (get price coll-asset-info))
        (coll-decimals (get decimals coll-asset-info))
        (coll-expected (mul-div-down coll-usd-expected (pow u10 coll-decimals) coll-price))
        
        ;; cap at available collateral (user may not have enough)
        (coll-actual (if (> coll-expected user-coll-balance)
                         user-coll-balance
                         coll-expected)))
    {
      coll-actual: coll-actual,
      coll-expected: coll-expected,
      coll-price: coll-price,
      coll-decimals: coll-decimals
    }))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L834-853)
```text
(define-private (calc-final-liquidation-amounts
  (debt-actual-usd uint)
  (coll-actual uint)
  (coll-expected uint)
  (coll-price uint)
  (coll-decimals uint)
  (debt-price uint)
  (debt-decimals uint)
  (liq-penalty uint))
  
  (let ((coll-actual-usd (normalize (* coll-actual coll-price) coll-decimals false))
        ;; If collateral was capped, recalculate debt proportionally
        (debt-final-usd (if (< coll-actual coll-expected)
                           (calc-liq-debt-repay-real coll-actual-usd liq-penalty)
                           debt-actual-usd))
        (debt-final (mul-div-down debt-final-usd (pow u10 debt-decimals) debt-price)))
    {
      debt-final-usd: debt-final-usd,
      debt-final: debt-final
    }))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1453-1512)
```text
    ;; collateral processing
    (user-coll-balance (find-collateral-amount (get collateral pos-full) coll-aid))
    (coll-info (process-collateral-asset coll-aid debt-actual-usd liq-penalty 
                                         user-coll-balance assets coll-asset))
    (coll-actual (get coll-actual coll-info))
    (coll-expected (get coll-expected coll-info))
    (coll-price (get coll-price coll-info))
    (coll-decimals (get coll-decimals coll-info))

    ;; final liquidation amounts (with proportional adjustment if needed)
    (final-amounts (calc-final-liquidation-amounts
                     debt-actual-usd coll-actual coll-expected
                     coll-price coll-decimals
                     debt-price debt-decimals liq-penalty))
    (debt-final-usd (get debt-final-usd final-amounts))
    (debt-final (get debt-final final-amounts))

    ;; debt scaling for storage
    (curr-scaled (get-account-scaled-debt borrower debt-aid))
    (scaled-info (scale-debt-for-liquidation debt-final coll-actual curr-scaled debt-aid))
    (scaled-to-remove (get scaled-to-remove scaled-info))
    (debt-to-repay (get debt-to-repay scaled-info))
    (coll-final-raw (get coll-final scaled-info))
    (coll-remaining (- user-coll-balance coll-final-raw))
    (remaining-debt-to-repay
      (if (> coll-remaining u0)
        (let ((rem-coll-usd (normalize (* coll-remaining coll-price) coll-decimals false))
              (rem-debt-usd (div-bps-down rem-coll-usd (+ BPS liq-penalty-max)))
              (rem-debt-tokens (mul-div-down rem-debt-usd (pow u10 debt-decimals) debt-price))
              (rem-borrow-index (get index (unwrap-panic (get-cached-indexes debt-aid))))
              (rem-scaled (mul-div-down rem-debt-tokens INDEX-PRECISION rem-borrow-index)))
          (mul-div-up rem-scaled rem-borrow-index INDEX-PRECISION))
        u1))
    (coll-final (if (is-eq remaining-debt-to-repay u0) user-coll-balance coll-final-raw)))

    (asserts! (not (is-liquidation-paused debt-aid)) ERR-LIQUIDATION-PAUSED)
    (asserts! (is-eq contract-caller tx-sender) ERR-AUTHORIZATION)
    (asserts! (> debt-amount u0) ERR-AMOUNT-ZERO)
    (asserts! (> debt-to-repay u0) ERR-ZERO-LIQUIDATION-AMOUNTS)
    (asserts! (> coll-final u0) ERR-ZERO-LIQUIDATION-AMOUNTS)
    (asserts! (>= coll-final min-collateral-expected) ERR-SLIPPAGE)

    ;; execute liquidation
    (try! (vault-system-repay debt-aid debt-to-repay debt-ft debt-address))

    ;; update obligations and socialize bad debt
    (let ((debt-updated (try! (contract-call? .v0-market-vault
                              debt-remove-scaled
                              borrower
                              scaled-to-remove
                              debt-aid)))
          ;; Collateral receiver defaults to liquidator if not specified
          (actual-receiver (match collateral-receiver recv recv liquidator))
          (coll-removed (try! (contract-call? .v0-market-vault
                              collateral-remove
                              borrower
                              coll-final
                              collateral-ft
                              coll-aid
                              actual-receiver)))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1534-1560)
```text
      ;; Handle bad debt socialization if no collateral left
      (let ((bad-debt-socialized 
              (if no-collateral-left
                  (let ((stripped-debt-list (filter-out-debt-asset (get debt pos-full) debt-aid))
                        (fresh-debt-list (if (is-eq debt-updated u0)
                                             stripped-debt-list
                                             (unwrap-panic (as-max-len?
                                               (append stripped-debt-list
                                                       { aid: debt-aid, scaled: debt-updated })
                                               u64)))))
                    (if (> (len fresh-debt-list) u0) ;; if still has debt
                      (let ((socialization-result (fold socialize-debt-asset 
                                                        fresh-debt-list 
                                                        { borrower: borrower, success: true })))
                        (asserts! (get success socialization-result) ERR-BAD-DEBT-SOCIALIZATION-FAILED)
                        ;; emit bad-debt-socialized event
                        (print {
                          action: "bad-debt-socialized",
                          caller: contract-caller,
                          data: {
                            borrower: borrower,
                            debt-list: fresh-debt-list
                          }
                        })
                        true)
                      false))
                  false)))
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L946-968)
```text
        (current-assets (var-get assets))
        (current-lindex (var-get lindex))
        (old-total-assets (total-assets))
        (debt-reduction (mul-div-down scaled-amount idx INDEX-PRECISION))
        (principal-reduction (if (> scaled-principal u0)
                                (mul-div-down scaled-amount borrowed scaled-principal)
                                u0))
        ;; Write down lindex proportionally to loss in total-assets
        (new-lindex (if (and (> old-total-assets u0) (> old-total-assets debt-reduction))
                       (mul-div-down current-lindex (- old-total-assets debt-reduction) old-total-assets)
                       u0)))

    (try! (check-caller-auth))
    (asserts! (> scaled-amount u0) ERR-AMOUNT-ZERO)

    (var-set lindex new-lindex)
    (var-set principal-scaled (if (> scaled-principal scaled-amount) (- scaled-principal scaled-amount) u0))
    (var-set total-borrowed (if (> borrowed principal-reduction) (- borrowed principal-reduction) u0))
    (var-set assets (if (> current-assets principal-reduction) (- current-assets principal-reduction) u0))

    (print {
      action: "socialize-debt",
      caller: contract-caller,
```

**File:** local-testing/tests/security/liquidation.test.ts (L153-188)
```typescript
  describe("ATK-LG-05: Bad debt cannot be artificially created", () => {
    it("should socialize bad debt when collateral is exhausted", async () => {
      // Setup: Alice has small collateral, large debt
      txOk(market.collateralAdd(sbtcToken.identifier, 100000000n, null), alice); // 1 sBTC
      txOk(market.borrow(usdcToken.identifier, 42000000000n, null, null), alice); // $42k
      
      // Crash price severely to create bad debt scenario
      // At $10k per BTC: collateral = $10k, debt = $42k (massive underwater)
      await set_price(PythFeedIds.BTC, scalePriceForPyth(10000, -8), -8, deployer);
      
      const charlieSbtcBefore = rov(sbtcToken.getBalance(charlie)).value!;
      
      // Charlie tries to liquidate - will seize all collateral but not cover all debt
      txOk(
        market.liquidate(
          alice,
          sbtcToken.identifier,
          usdcToken.identifier,
          50000000000n, // Try to liquidate $50k (more than debt)
          0n,
          null,
          null
        ),
        charlie
      );
      
      const charlieSbtcAfter = rov(sbtcToken.getBalance(charlie)).value!;
      const collateralSeized = charlieSbtcAfter - charlieSbtcBefore;
      
      // Should have seized all of Alice's collateral (1 BTC)
      expect(collateralSeized).toBeLessThanOrEqual(100000000n);
      
      // Bad debt should be socialized (verified by liquidation succeeding)
      // The protocol handled the bad debt rather than allowing it to corrupt the system
      
      console.log("✓ Bad debt properly socialized when collateral exhausted");
```
