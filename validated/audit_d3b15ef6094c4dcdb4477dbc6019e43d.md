### Title
Hardcoded `STSTX-RATIO-DECIMALS` scaling factor is assumed for the external stSTX ratio oracle, mirroring the Compound exchange-rate decimal assumption bug - ([File: mainnet/contracts/market/v0-4-market.clar])

### Summary
`v0-4-market.clar` prices stSTX (and zstSTX) by calling an external, DAO-independent contract (`block-info-nakamoto-ststx-ratio-v2`) for the "STX per stSTX" ratio and then dividing that raw integer by a hardcoded constant `STSTX-RATIO-DECIMALS` (`u1000000`, i.e. 6 decimals) to normalize it into the price-math domain, exactly the same class of bug flagged in the referenced report: assuming a fixed decimal scale for a value returned by an external protocol contract instead of deriving/validating the scale from that contract.

### Finding Description
`call-ststx-ratio` forwards directly to the external protocol function: [1](#0-0) 

The returned raw ratio is then combined with the market's own hardcoded constant to compute the stSTX price transformation: [2](#0-1) [3](#0-2) 

`resolve-ststx` performs `mul-div-down p ratio STSTX-RATIO-DECIMALS`, i.e. it treats whatever `get-ststx-ratio-v3` returns as if it is always scaled to exactly `1e6`. There is no on-chain call to query the actual precision/decimals used by `block-info-nakamoto-ststx-ratio-v2`, and no assertion validating the magnitude of the returned ratio against an expected range before using it in collateral/debt price math. This is structurally identical to the Compound `CTokenMultiOracle` bug: a `Source`'s scale (`exchangeRateCurrent`/`exchangeRateStored` decimals in the original report, `get-ststx-ratio-v3` decimals here) is fixed at integration time as a constant rather than derived from or validated against the actual external contract, so if that assumption is wrong (or the external contract is upgraded/redeployed with a different precision, as its own `-v2` naming shows has already happened once), every price computed through this path — used both for standalone stSTX collateral valuation and, doubly, for the `zstSTX` vault-token price (which composes `resolve-ststx` then `resolve-ztoken`) — is silently wrong by orders of magnitude while the oracle machinery still reports it as a fresh, "legal" (`> u0`) price.

This directly affects health checks and liquidation math: `resolve-ststx`/`resolve-ztoken` results feed into `price-resolve`, which feeds into the market's notional/health evaluation used by `collateral-add`, borrow, and liquidation entry points, i.e. exactly the oracle resolution/callcode transform path called out as in-scope.

### Impact Explanation
If the decimal-scale assumption baked into `STSTX-RATIO-DECIMALS` does not match what `block-info-nakamoto-ststx-ratio-v2.get-ststx-ratio-v3` actually returns (now or after any future upgrade of that external ratio contract), the computed USD price of stSTX/zstSTX collateral would be systematically wrong by a fixed power-of-ten multiple. An over-valuation would let ordinary borrowers post stSTX/zstSTX collateral and borrow far more than the true value supports, i.e. direct theft of protocol funds (insolvency), landing on the Critical impact class (protocol insolvency / theft of funds at rest).

### Likelihood Explanation
Likelihood depends entirely on whether the assumed `1e6` scale exactly matches the live external contract's return value at all times, including through any future migration of the ststx-ratio provider (already versioned `-v2`, implying at least one prior change). Because the market contract never independently verifies this scale on-chain, any mismatch — introduced by an upgrade, a different network's ratio contract deployment, or a misconfiguration during the module's `@mainnet` activation — would silently corrupt every stSTX/zstSTX price with no in-protocol safeguard to catch it.

### Recommendation
Do not hardcode `STSTX-RATIO-DECIMALS` as a blind assumption about the external ratio contract's precision. Instead, query the decimals/precision explicitly from `block-info-nakamoto-ststx-ratio-v2` (or a documented, versioned interface guarantee) at call time, add a sanity-bound assertion on the raw ratio value (e.g., reject values wildly outside a plausible STX-per-stSTX range) before using it in `resolve-ststx`, and re-verify the assumption whenever the external ratio provider contract is upgraded.

### Proof of Concept
1. Assume (or simulate via a mock in place of `block-info-nakamoto-ststx-ratio-v2`) that `get-ststx-ratio-v3` returns its ratio scaled to 8 decimals (`1e8`) instead of the `1e6` assumed by `STSTX-RATIO-DECIMALS`, e.g. returns `105000000` for a true ratio of `1.05`.
2. `resolve-ststx` computes `mul-div-down p 105000000 1000000` = `p * 105`, i.e. a ~100x overvaluation of stSTX price versus the true `1.05x` STX price.
3. A user calls `collateral-add` posting stSTX (or zstSTX, which chains through `resolve-ztoken` on top of the same inflated `resolve-ststx` output) as collateral; the market's health/notional check in `v0-4-market.clar` uses this inflated price to authorize borrowing far beyond the real collateral value.
4. The user borrows other assets (e.g. USDC/sBTC) up to the inflated collateral ceiling and withdraws them, leaving the protocol under-collateralized/insolvent once the price discrepancy is later corrected or exploited via liquidation avoidance.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L48-49)
```text
;; -- Oracle ratios
(define-constant STSTX-RATIO-DECIMALS u1000000)
```

**File:** mainnet/contracts/market/v0-4-market.clar (L339-341)
```text
(define-private (resolve-ststx (p uint))
  (let ((ratio (unwrap! (call-ststx-ratio) ERR-ORACLE-CALLCODE)))
    (ok (mul-div-down p ratio STSTX-RATIO-DECIMALS))))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1014-1016)
```text
;; ststx ratio transformation
(define-public (call-ststx-ratio)
  (contract-call? 'SP4SZE494VC2YC5JYG7AYFQ44F5Q4PYV7DVMDPBG.block-info-nakamoto-ststx-ratio-v2 get-ststx-ratio-v3))
```
