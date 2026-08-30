### Title
Depositors can front-run bad-debt write-down and exit at an inflated share price, forcing losses (and potential insolvency) onto remaining vault depositors - (File: `mainnet/contracts/vault/v0-vault-usdc.clar`, `mainnet/contracts/market/v0-4-market.clar`)

### Summary
The reported bug class is a timing race between an unprivileged withdrawal path and an asynchronous "loss/slash" accounting event: a staker can pull principal out before a slash is finalized, so the slash lands on funds that are no longer there, corrupting accounting and potentially reverting the last withdrawer. The equivalent unprivileged-principal race exists in the Zest vault/market pair: vault share pricing (`total-assets`) values a borrower's outstanding debt at full face value + accrued interest until the market explicitly calls `socialize-debt`, and that write-down only happens atomically inside `liquidate()` once someone actually liquidates the position. Any depositor can call `redeem` at the stale, inflated share price for as long as the underwater position sits unliquidated, extracting real underlying assets before the loss is recognized.

### Finding Description
Vault share value is derived from `total-assets`, which adds back all accrued (unpaid) interest on the vault's scaled debt regardless of whether that debt is actually collectible: [1](#0-0) 

`redeem` only gates on share balance, pause state, slippage, and `available-assets` (raw liquidity) — it performs no check on the health of the market positions backing that debt: [2](#0-1) 

The only mechanism that reduces this overstated valuation is `socialize-debt`, which writes down `lindex` (the per-share value) and is restricted to an authorized caller: [3](#0-2) 

That authorized caller is `market.clar`, and it only invokes `socialize-debt` from inside `liquidate()`, at the very end of the flow, and only when a borrower's collateral has been fully exhausted: [4](#0-3) [5](#0-4) 

Between the moment a borrower's position becomes hopelessly underwater (price crash, LTV far past `LTV-LIQ-FULL`) and the moment some third party actually submits `liquidate()` for that borrower, `total-assets`/`lindex` for the vault holding that debt still reflects the position as if it were fully solvent. Any depositor watching the mempool or price feeds can call `redeem` during this window and receive underlying assets at the pre-write-down exchange rate — functionally identical to Alice withdrawing her staked ETH before her challenged batch is proven in the original report. Once `socialize-debt` eventually fires, the loss is spread only across whatever principal remains in the vault, disproportionately punishing depositors who did not exit in time, and — if enough depositors exit first — the pool of `available-assets` may be depleted such that later, legitimate redeemers revert with `ERR-INSUFFICIENT-LIQUIDITY`/`ERR-INSUFFICIENT-ASSETS`, mirroring the "last transaction reverts" failure mode in the source report.

### Impact Explanation
This does not require any privileged action, DAO compromise, or oracle manipulation caused elsewhere — it is a pure ordering/timing race available to any unprivileged depositor who can observe on-chain state (price feeds, position health) before a liquidator acts. The result is a shift of a bad-debt loss away from the party who should bear it and onto remaining depositors, and in the worst case an inability for the last depositors to redeem at all — landing on the in-scope impact classes of protocol insolvency and/or temporary freezing of funds for the remaining depositors.

### Likelihood Explanation
Underwater positions are not instantaneous events — liquidation requires a separate transaction from an external liquidator, health can be checked by anyone via `is-healthy`/oracle prices, and `redeem` remains fully open with no cooldown or health-based restriction during that window. Any sophisticated depositor (or bot) monitoring prices has a straightforward, gas-cheap way to front-run the liquidation/`socialize-debt` call. Likelihood is moderate-to-high, gated primarily on there being sufficient `available-assets` liquidity to redeem against and a large-enough single position going bad relative to vault size.

### Recommendation
Ensure loss recognition is not deferrable relative to withdrawal: either (a) make `redeem`'s pricing sensitive to detectably unhealthy/underwater positions still counted at face value (e.g., mark-to-market or haircut debt whose LTV exceeds `LTV-LIQ-FULL` before allowing full-price redemption), or (b) require any position past `LTV-LIQ-FULL` to be forcibly and immediately liquidated/written down (e.g., via a permissionless "mark bad debt" step usable independently of a full liquidation) so `total-assets`/`lindex` are updated before further redemptions are permitted, closing the window that lets depositors exit at a stale valuation.

### Proof of Concept
1. Borrower deposits sBTC collateral and borrows USDC near the max LTV, exactly as in the existing test harness scenario (`ATK-LG-05`) that already demonstrates a position going massively underwater: [6](#0-5) 
2. BTC price crashes (as in that test, from full price to $10k), making the position's debt far exceed its collateral value, but no one has called `liquidate()` yet.
3. During this window, `total-assets`/`lindex` for the USDC vault still counts the borrower's full scaled debt (principal + accrued interest) as good, per `total-assets`: [1](#0-0) 
4. Any other USDC-vault depositor calls `redeem`, which only checks share balance/slippage/liquidity — not position health — and withdraws underlying USDC at the still-inflated exchange rate: [2](#0-1) 
5. Later, `liquidate()` is finally called, triggers `socialize-debt-asset` → `vault-socialize-debt`, and only now writes down `lindex`, spreading the entire loss over whatever principal remains in the vault (i.e., over the depositors who did not exit early): [3](#0-2) 
6. If enough depositors race out beforehand, the vault's `available-assets` can be insufficient for the remaining/last redeemers, causing their `redeem` calls to revert with `ERR-INSUFFICIENT-LIQUIDITY`.

### Citations

**File:** local-testing/contracts/vault/vault-ststxbtc.clar (L338-343)
```text
(define-private (total-assets)
  (let ((current-assets (var-get assets))
        (debt (total-debt))
        (borrowed (var-get total-borrowed))
        (interest (if (> debt borrowed) (- debt borrowed) u0)))
    (+ current-assets interest)))
```

**File:** local-testing/contracts/vault/vault-ststx.clar (L801-821)
```text
(define-public (redeem (amount uint) (min-out uint) (recipient principal))
  (let (
    (states (var-get pause-states))
    (u (try! (accrue)))
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
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L942-964)
```text
(define-public (socialize-debt (scaled-amount uint))
  (let ((scaled-principal (var-get principal-scaled))
        (borrowed (var-get total-borrowed))
        (idx (var-get index))
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
```

**File:** mainnet/contracts/market/v0-4-market.clar (L879-903)
```text
(define-private (socialize-debt-asset
                (debt-entry { aid: uint, scaled: uint })
                (acc { borrower: principal, success: bool }))
  ;; Early return if previous socialization failed
  (if (not (get success acc))
      acc
      (let ((borrower (get borrower acc))
            (failed-status { borrower: borrower, success: false })
            (asset-id (get aid debt-entry))
            (scaled-debt (get scaled debt-entry)))

            ;; Socialize in vault - pass scaled directly to avoid rounding
            (unwrap! (vault-socialize-debt asset-id scaled-debt) failed-status)
            ;; Refresh cache with new indexes post-write-down (lindex decreased)
            (map-set index-cache
                     { timestamp: stacks-block-time, aid: asset-id }
                     (unwrap! (vault-accrue asset-id) failed-status))
            ;; Remove from obligation
            (unwrap! (contract-call? .v0-market-vault
                                      debt-remove-scaled
                                      borrower
                                      scaled-debt
                                      asset-id) failed-status)
          acc)
        ))
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

**File:** local-testing/tests/security/liquidation.test.ts (L153-177)
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
```
