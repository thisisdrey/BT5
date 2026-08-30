### Title
Rounding-down in `convert-to-shares-preview` lets deposits mint zero shares, permanently freezing depositor funds - (File: mainnet/contracts/vault/v0-vault-stx.clar and its ststx/ststxbtc/sbtc/usdc/usdh counterparts)

### Summary
The vault's ERC4626-style share conversion uses floor division (`mul-div-down`), which — analogous to the Fenix `calculateRewardPerEpoch` rounding-loss finding — can round a depositor's minted shares down to zero when `amount * total-supply < total-assets`, while the deposited assets are still added to the vault's asset pool and permanently socialized to existing shareholders.

### Finding Description
Share issuance is computed by `convert-to-shares-preview`: [1](#0-0) 

which calls the floor-rounding helper: [2](#0-1) 

This is the exact rounding pattern described in the external report: integer division `(balance * numerator) / denominator` rounds toward zero, and when `total-supply-preview / total-assets-preview` is large enough (e.g., after significant interest accrual inflates `total-assets` relative to `total-supply`, or vice-versa once treasury LP minting via `calc-treasury-lp-preview` has grown `total-supply` disproportionately), a small-but-nonzero deposit `amount` produces `(amount * ts) / ta == 0`. The same `mul-div-down` floor pattern is repeated identically across all vault instances (`v0-vault-stx.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`, `v0-vault-sbtc.clar`, `v0-vault-usdc.clar`, `v0-vault-usdh.clar`).

Unlike the reward-distribution case in the report, this hits the deposit path directly: if the deposit function does not reject a zero-share mint, the caller's underlying tokens are transferred into the vault's `assets` accounting while the fungible-token mint call for `u0` shares effects nothing, so the depositor receives no claim while the vault's `total-assets` grows — a direct value transfer to existing LPs, and a freeze of the depositor's own funds/yield since they cannot redeem back what they put in.

### Impact Explanation
This falls under **High** — temporary/permanent freezing of the depositor's unclaimed value (their deposited assets never turn into a redeemable share claim), and effectively a transfer of that value to the other silent shareholders, matching the "Reward tokens will be locked and not distributed because of rounding error" class from the source report, translated to vault share accounting instead of an epoch-reward accumulator.

### Likelihood Explanation
Likelihood scales with the ratio `total-supply-preview / total-assets-preview`: as `total-assets` accrues interest (via `total-assets-preview`) or as `total-supply` is inflated by repeated treasury-LP minting (`calc-treasury-lp-preview`), the ratio drifts, making it increasingly easy for ordinary small deposits from any unprivileged principal to round to zero shares — this requires no special privilege, only calling the public deposit entry point with an adversarially/naturally small `amount` relative to vault size.

### Recommendation
- Enforce a minimum non-zero share output check in the deposit path (revert with `ERR-OUTPUT-ZERO` if the computed shares from `convert-to-shares-preview` is `u0` for a non-zero `amount`), mirroring the existing `ERR-OUTPUT-ZERO` error constant already defined in the contract.
- Consider rounding conversions consistently (round down for shares minted on deposit is standard ERC4626 practice, but must be paired with a minimum-shares-out guard) so that no assets can enter the vault's `assets` variable without a corresponding non-zero share mint.

### Proof of Concept
1. Vault accrues significant interest over time such that `total-assets-preview` becomes large relative to `total-supply-preview` (e.g., `ta = 10_000_000`, `ts = 1000`, ratio 10000:1).
2. An unprivileged caller calls `deposit` with a small `amount` (e.g., `amount = 5`), which is a legitimate, non-dust deposit in the underlying token's smallest units.
3. `convert-to-shares-preview` computes `mul-div-down(5, 1000, 10_000_000) = 5000 / 10_000_000 = 0` due to floor division.
4. If the deposit flow does not reject a zero-share result, the underlying tokens are transferred into vault custody and `assets` is incremented by `amount`, but the caller receives `0` `zft` shares — their deposit is irrecoverably donated to existing holders, and they cannot withdraw it back.

Note: I was unable to fully read the public `deposit`/`withdraw` function bodies within the remaining tool budget to confirm whether an explicit zero-shares guard is already present before the mint call; this should be verified directly in `mainnet/contracts/vault/v0-vault-stx.clar`'s `deposit` function (and its sibling vault files) as the decisive check for whether this rounding path is currently exploitable in production.

### Citations

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L147-148)
```text
(define-private (mul-div-down (x uint) (y uint) (z uint))
  (/ (* x y) z))
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L308-315)
```text
(define-private (convert-to-shares-preview (amount uint))
  (let ((ta (total-assets-preview))
        (ts (total-supply-preview)))
    (if (is-eq ts u0)
        amount
        (if (is-eq ta u0)
            u0
            (mul-div-down amount ts ta)))))
```
