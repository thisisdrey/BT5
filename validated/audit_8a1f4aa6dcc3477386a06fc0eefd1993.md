### Title
Rounding-to-zero in `deposit` silently mints zero shares while pulling full underlying, permanently freezing the depositor's principal - (File: `mainnet/contracts/vault/v0-vault-stx.clar` and all `v0-vault-*.clar` vaults)

### Summary
Every Zest vault contract (`v0-vault-stx.clar`, `v0-vault-sbtc.clar`, `v0-vault-usdc.clar`, `v0-vault-ststx.clar`, `v0-vault-usdh.clar`, `v0-vault-ststxbtc.clar`) implements `deposit` with no check that the computed share amount (`inkind`) is non‑zero before transferring the depositor's underlying and minting shares. `redeem` explicitly guards against a zero output with `ERR-OUTPUT-ZERO`, but `deposit` does not have the symmetric guard, so a legitimate deposit can silently mint `0` shares while the full underlying amount is pulled from the caller and folded into vault `assets`.

### Finding Description
`convert-to-shares-preview` rounds down when converting assets to shares: [1](#0-0) 

If total supply `ts` is non-zero and total assets `ta` is non-zero, shares are computed as `mul-div-down amount ts ta`, which truncates to `0` whenever `amount < ta / ts` (i.e., whenever the deposit is smaller than the current price-per-share).

`deposit` uses this preview directly as the amount to mint, but never asserts it is non-zero: [2](#0-1) 

Compare this to `redeem`, in the same contract, which explicitly guards the symmetric case with `ERR-OUTPUT-ZERO`: [3](#0-2) 

The `ERR-OUTPUT-ZERO` constant exists specifically for this purpose but is only wired into `redeem`, not `deposit`: [4](#0-3) 

The vault's price-per-share (`ta/ts`) is not static — it is designed to increase over time as interest accrues. `accrue` grows `total-assets-preview` from accumulated interest on borrowed principal, while `total-supply` only grows from the (much smaller) treasury reserve-factor mint: [5](#0-4) 

Because the vault is bootstrapped with a tiny `MINIMUM-LIQUIDITY` initial share supply: [6](#0-5) [7](#0-6) 

the price-per-share ratio `ta/ts` starts near `1` and only grows upward from there as the vault earns yield, since `ts` increases far slower than `ta`. As this ratio rises, the minimum deposit amount required to receive at least `1` share (in the vault's smallest internal unit) rises with it. Any ordinary user who deposits an amount below that threshold — which can be an entirely normal, "reasonable" deposit expressed in human terms once decimals and price-per-share are considered, and does not require any privileged action or DAO misconfiguration — has their full underlying amount transferred into the vault's `assets` and receives `0` shares in return, with the transaction succeeding (`ft-mint? zft 0 recipient` does not revert). The depositor's principal is absorbed into the shared pool and proportionally benefits all other existing shareholders, but the depositor has no shares and thus no path to reclaim any of the deposited value.

This is the direct analog of the referenced report's root cause: an ERC4626-style share-conversion rounding-to-zero edge case that was not correctly guarded in a deposit path. In the original Sense report the missing guard caused a revert (DoS on `roll`); in Zest's vaults the missing guard is on the opposite side (no revert at all), which is more severe because it silently destroys the depositor's funds instead of merely blocking the call.

### Impact Explanation
This is a Critical severity issue — permanent freezing/loss of user principal. Any unprivileged user calling `deposit` when the price-per-share has grown such that their deposit amount rounds to `0` shares in `convert-to-shares-preview` will lose 100% of the deposited underlying with no recourse (no shares minted, so no `redeem` is possible for that value). The lost value is not destroyed but effectively redistributed to existing shareholders, meaning the vault's other depositors gain at the direct, permanent expense of the affected depositor's principal.

### Likelihood Explanation
The bug is reachable purely through ordinary use of the public `deposit` entry point — no privileged role, DAO action, or flashloan is required. It becomes increasingly likely to be triggered (even accidentally, by normal users making small deposits) the longer a vault operates and accrues interest, since the price-per-share only ever grows relative to the tiny `MINIMUM-LIQUIDITY` bootstrap supply. It is also trivially triggerable deliberately: an attacker can identify the current price-per-share via read-only calls (`get-liquidity-index`, asset/supply getters) and submit a deposit sized just below the rounding threshold to guarantee `0` shares are minted — although in the deliberate case the "loss" is the attacker's own funds, this same code path is exercised by any ordinary user's normal-sized deposit once the ratio is high enough, making unintentional fund loss for genuine users likely over the vault's lifetime.

### Recommendation
Add the same zero-output guard used in `redeem` to `deposit` in every vault contract, e.g.:
```clarity
(asserts! (> inkind u0) ERR-OUTPUT-ZERO)
```
placed alongside the other `asserts!` checks in `deposit`, before `receive-underlying`/`ft-mint?` are called, so that a deposit which would compute to zero shares reverts instead of silently consuming the depositor's funds.

### Proof of Concept
1. Vault is initialized normally; `initialize` mints `MINIMUM-LIQUIDITY` (`u1000`) shares to `NULL-ADDRESS`, so `total-supply = 1000` and `assets = 1000`. [7](#0-6) 
2. Over time, borrowers draw down liquidity and interest accrues; each `accrue` call increases `total-assets-preview` via `debt-preview`, while `total-supply` only increases by the smaller `treasury-lp` mint driven by `fee-reserve`. [5](#0-4) 
3. Once `total-assets-preview()/total-supply()` (price-per-share) exceeds `N` (in the vault's base units), any `deposit` call with `amount < N` computes `inkind = mul-div-down amount ts ta = 0` in `convert-to-shares-preview`. [1](#0-0) 
4. `deposit` proceeds without any check on `inkind`, transfers `amount` of underlying from the caller via `receive-underlying`, mints `0` shares via `ft-mint? zft 0 recipient`, and updates `assets` to include the deposited amount — the call returns `(ok 0)` successfully instead of reverting. [2](#0-1) 
5. The depositor now holds `0` vault shares and has no way to redeem the amount they deposited; that value is permanently retained by the vault and benefits all other existing shareholders proportionally.

### Citations

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L35-35)
```text
(define-constant MINIMUM-LIQUIDITY u1000)
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L55-55)
```text
(define-constant ERR-OUTPUT-ZERO (err u800012))
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

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L492-507)
```text
;; -- Initialization ---------------------------------------------------------

(define-public (initialize)
  (begin
    (asserts! (not (var-get initialized)) ERR-ALREADY-INITIALIZED)
    (var-set initialized true)
    (try! (deposit MINIMUM-LIQUIDITY u0 NULL-ADDRESS))
    
    (print {
      action: "vault-initialize",
      caller: contract-caller,
      data: {
        vault: UNDERLYING,
        minimum-liquidity: MINIMUM-LIQUIDITY
      }
    })
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L763-795)
```text
(define-public (deposit (amount uint) (min-out uint) (recipient principal))
    (let (
      (states (var-get pause-states))
      (u (try! (accrue)))
      (account contract-caller)
      (CAP-SUPPLY (var-get cap-supply))
      (current-assets (var-get assets))
      (inkind (convert-to-shares-preview amount)))

    (asserts! (not (get deposit states)) ERR-PAUSED)
    (asserts! (var-get initialized) ERR-INIT)
    (asserts! (not (var-get in-flashloan)) ERR-REENTRANCY)
    (asserts! (> amount u0) ERR-AMOUNT-ZERO)
    (asserts! (>= inkind min-out) ERR-SLIPPAGE)
    (asserts! (<= (+ current-assets amount) CAP-SUPPLY) ERR-SUPPLY-CAP-EXCEEDED)

    (try! (receive-underlying amount account))
    (try! (ft-mint? zft inkind recipient))
    (var-set assets (+ current-assets amount))
    
    (print {
      action: "deposit",
      caller: contract-caller,
      data: {
        depositor: account,
        recipient: recipient,
        amount: amount,
        shares-minted: inkind,
        assets: (+ current-assets amount)
      }
    })

    (ok inkind)))
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L797-813)
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
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L843-852)
```text
          (let ((next (next-index))
                (nliq (next-liquidity-index))
                (scaled-principal (var-get principal-scaled))
                (old-debt (mul-div-down scaled-principal idx INDEX-PRECISION))
                (new-debt (mul-div-down scaled-principal next INDEX-PRECISION))
                (debt-delta (if (> new-debt old-debt) (- new-debt old-debt) u0))
                (reserve-inc (mul-div-down debt-delta (var-get fee-reserve) BPS))
                (treasury-lp (if (> reserve-inc u0) (mul-div-down reserve-inc (total-supply) (- (total-assets-preview) reserve-inc)) u0)))
            (if (not (is-eq idx next))
                (var-set index next)
```
