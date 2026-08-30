## Analog Found

### Title
LPs can front-run `socialize-debt` bad-debt write-downs by withdrawing vault shares before losses are realized, socializing losses onto passive LPs - (File: mainnet/contracts/vault/v0-vault-usdc.clar, mainnet/contracts/market/v0-4-market.clar)

### Summary
The Buffer report describes LPs gaming a shared pool by withdrawing before predictable losses (ITM option expiry) and depositing before predictable gains (OTM expiry), because the pool has no epoch/lockup buffer separating deposit/withdraw timing from loss-realization events. Zest's vaults (`v0-vault-usdc.clar`, etc.) have the same structural gap: `deposit` and `redeem` are freely callable at any time with no lockup, cooldown, or epoch buffer, while the loss-realization event — `socialize-debt`, triggered from `market/v0-4-market.clar`'s `liquidate` — is fully predictable in advance from public oracle prices and on-chain position data.

### Finding Description
`redeem` in the vault contracts allows any zToken holder to withdraw underlying instantly, subject only to available liquidity, with no minimum holding period: [1](#0-0) 

When a borrower's position has no collateral left after liquidation, `v0-4-market.clar`'s `liquidate` calls `socialize-debt-asset`, which invokes `vault-socialize-debt` to write down the vault's `lindex` (the liquidity index that determines the underlying/zToken exchange rate) proportionally to the loss: [2](#0-1) 

The vault-side `socialize-debt` function that performs this write-down is: [3](#0-2) 

Because a borrower's health factor and the exact price at which their position becomes undercollateralized (with `no-collateral-left`) are derivable from public oracle data and on-chain position state (`get-liquidation-position`, `get-full-position`), any LP monitoring the market can predict, with reasonable confidence, when a `socialize-debt` write-down is imminent (e.g., a large under-margined position with a rapidly worsening price). An LP can then `redeem` their zTokens for underlying before the liquidation transaction lands, avoiding the `lindex` write-down entirely, leaving all remaining zToken holders to absorb the full loss at a lower exchange rate. This is functionally identical to the Buffer report's "race to withdraw capital before any option expires ITM" — there is no epoch-based deposit/withdraw buffer or lockup that the Buffer report recommended, and Zest's implementation is if anything more permissive since Buffer had at least a 10-minute lock.

### Impact Explanation
This lets sophisticated/informed LPs systematically shift bad-debt losses onto passive LPs holding the same zToken, which is a transfer of already-accrued value away from other depositors — i.e., theft of unclaimed yield (the passive LPs' pro-rata share of vault assets is diminished disproportionately to benefit the gaming LP), which falls under the in-scope High severity impact class.

### Likelihood Explanation
Likelihood is moderate-to-high in practice: liquidation and socialization events require oracle price crossing a known LTV threshold on a known position, both fully observable on-chain before the liquidating transaction is mined, and vault liquidity is often sufficient for withdrawal since the report's own test suite (`liquidation.test.ts`, "Liquidation Gaming Attack Tests") shows the team is aware of liquidation-gaming risk in general but the tests only cover front-running collateral top-ups and same-block liquidation, not LP-side withdrawal-before-socialization: [4](#0-3) 

### Recommendation
Introduce a withdrawal cooldown/epoch buffer on vault `redeem` (and correspondingly on `deposit`, to prevent the inverse "deposit right before a beneficial event" gaming), or require zTokens to be non-transferable/non-redeemable for a minimum holding window after mint, so that an LP cannot react to imminent, predictable `socialize-debt` events within the same block/short window used to detect and front-run them.

### Proof of Concept
1. Borrower B opens a position near the liquidation threshold; price of collateral is public via oracle feed.
2. LP monitors position health via `get-liquidation-position`/oracle price feed and detects B's position is about to have `no-collateral-left` on the next adverse price tick (bad debt event imminent).
3. LP calls `redeem` on the vault to cash out zTokens for underlying at the current (pre-loss) `lindex`, exiting before the loss is realized: [5](#0-4) 
4. A liquidator (or the LP itself) then calls `liquidate` on `v0-4-market.clar`, triggering `socialize-debt-asset` → `vault-socialize-debt`, which writes down `lindex` for all remaining zToken holders: [6](#0-5) 
5. Remaining LPs bear the loss at a reduced exchange rate while the gaming LP fully avoided it, having exited beforehand with no lockup preventing the withdrawal.

### Citations

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L795-819)
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

  (print {
    action: "redeem",
    caller: contract-caller,
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

**File:** mainnet/contracts/vault/v0-vault-sbtc.clar (L942-964)
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

**File:** local-testing/tests/security/liquidation.test.ts (L29-55)
```typescript
describe("Liquidation Gaming Attack Tests", () => {
  beforeEach(async () => {
    // Initialize protocol
    initializeProtocol();
    
    // Initialize Pyth oracle
    init_pyth(deployer);
    executeDaoProposal(contracts.proposalSetPriceStaleness);
    
    // Set initial prices
    await set_initial_price(PythFeedIds.BTC, scalePriceForPyth(60000, -8), deployer);
    await set_initial_price(PythFeedIds.USDC, scalePriceForPyth(1, -8), deployer);
    
    // Mint tokens
    txOk(usdcToken.mint(1000000000000n, alice), deployer); // 1M USDC for alice
    txOk(usdcToken.mint(1000000000000n, bob), deployer); // 1M USDC for bob
    txOk(usdcToken.mint(100000000000n, charlie), deployer); // 100k USDC for charlie (liquidator)
    txOk(sbtcToken.mint(1000000000n, alice), deployer); // 10 sBTC for alice
    txOk(sbtcToken.mint(1000000000n, bob), deployer); // 10 sBTC for bob
    
    // Provide vault liquidity
    txOk(vaultUsdc.deposit(500000000000n, 0n, bob), bob); // 500k USDC
    txOk(vaultSbtc.deposit(500000000n, 0n, bob), bob); // 5 sBTC
    
    // Create egroups
    executeDaoProposal(proposalCreateMultipleEgroups);
  });
```
