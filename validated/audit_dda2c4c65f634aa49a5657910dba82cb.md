### Title
Missing Zero-Shares Validation in Vault `deposit` Allows Silent Loss of Deposited Principal - ([File: mainnet/contracts/vault/v0-vault-stx.clar])

### Summary
The `deposit` function in the Zest vault contracts (`v0-vault-stx.clar`, `v0-vault-sbtc.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`, `v0-vault-usdc.clar`, `v0-vault-usdh.clar`) computes the shares to mint via `convert-to-shares-preview` and only checks the result against a caller-supplied `min-out` slippage bound, never against zero. If the computed share amount rounds down to `0` and the caller passes `min-out u0` (the natural default for an "any amount is fine" call), the function transfers the underlying asset into the vault and increments `assets`, but mints `0` zTokens to the depositor — permanently losing the depositor's principal with no shares to redeem it.

### Finding Description
`deposit` in `v0-vault-stx.clar` (representative of all vault instances) is: [1](#0-0) 

The asserts performed are: not paused, initialized, not in a flashloan, `amount > 0`, `inkind >= min-out` (slippage), and supply cap not exceeded. There is **no** `(asserts! (> inkind u0) ERR-OUTPUT-ZERO)` check. Compare this to `redeem` in the same file, which explicitly guards against a zero output: [2](#0-1) 

`redeem` asserts `(> inkind u0) ERR-OUTPUT-ZERO` before burning shares and sending underlying assets back — but the symmetric protection is absent from `deposit`. Because `inkind` (the shares to mint) is only checked against the caller-supplied `min-out`, a caller who passes `min-out u0` (or the default flow through `market.clar`'s helper functions, which forward a user-supplied `min-shares`/`min-out` that a naive integrator or UI might leave at `0`) will have `(>= 0 0)` succeed, so the deposit proceeds:

- `receive-underlying amount account` pulls the depositor's tokens into the vault.
- `(var-set assets (+ current-assets amount))` credits the vault's internal accounting.
- `(try! (ft-mint? zft inkind recipient))` mints `0` zTokens — the depositor receives nothing representing their deposit.

This directly mirrors the reported bug class ("Missing Validation of deposit to targetVault Return Value"): the code assumes a non-zero share/asset conversion always occurs and doesn't validate it. Here, the vulnerable call is the vault's own share-conversion (`convert-to-shares-preview`) rather than an external contract's return value, but the failure mode and missing guard pattern are identical, and this is reachable directly by any unprivileged principal calling `deposit` on the vault, or indirectly via `market.clar`'s `supply-collateral-add`: [3](#0-2) 

which passes through a caller-supplied `min-shares` to `vault-deposit`: [4](#0-3) 

### Impact Explanation
A depositor whose amount rounds down to `0` shares (e.g., very small deposit relative to the current share price, or a share price skewed by prior activity) loses their entire deposited principal with no zToken minted to redeem it — the funds remain locked in the vault's `assets` accounting, permanently inaccessible to that depositor while silently benefiting all other existing shareholders. This falls into the in-scope impact class of **permanent freezing of funds** (the depositor's own principal).

### Likelihood Explanation
This requires no privileged access — it is triggerable by any ordinary principal calling `deposit` directly on a vault, or through `market.clar`'s `supply-collateral-add`, simply by depositing a small enough amount (or an amount that computes to `0` shares at the prevailing share price) while supplying `min-out`/`min-shares` of `0`. Because `min-out u0` is a legitimate and common default for callers who don't want slippage protection, this is easily triggered accidentally as well as intentionally.

### Recommendation
Add an explicit zero-output check in `deposit`, mirroring the existing `redeem` guard:
```clarity
(asserts! (> inkind u0) ERR-OUTPUT-ZERO)
```
placed alongside the existing `ERR-SLIPPAGE` check in every vault's `deposit` function.

### Proof of Concept
1. Attacker or unaware user calls `vault-stx.deposit(amount, min-out: u0, recipient: tx-sender)` where `amount` is small enough that `convert-to-shares-preview(amount)` rounds to `0` (or the vault's share price has drifted such that any given small `amount` computes to `0` shares).
2. `(asserts! (>= inkind min-out) ERR-SLIPPAGE)` passes because `(>= u0 u0)` is `true`.
3. `receive-underlying amount account` executes, pulling `amount` from the depositor.
4. `(var-set assets (+ current-assets amount))` credits the vault.
5. `(try! (ft-mint? zft inkind recipient))` mints `0` zTokens to the depositor.
6. The depositor's `amount` is now part of vault `assets` with zero corresponding zToken balance — permanently unredeemable by that depositor.

### Citations

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

**File:** mainnet/contracts/vault/v0-vault-sbtc.clar (L799-816)
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

```

**File:** mainnet/contracts/market/v0-4-market.clar (L225-232)
```text
(define-private (vault-deposit (aid uint) (amount uint) (min-out uint) (recipient principal))
  (if (is-eq aid STX) (contract-call? .v0-vault-stx deposit amount min-out recipient)
  (if (is-eq aid sBTC) (contract-call? .v0-vault-sbtc deposit amount min-out recipient)
  (if (is-eq aid stSTX) (contract-call? .v0-vault-ststx deposit amount min-out recipient)
  (if (is-eq aid USDC) (contract-call? .v0-vault-usdc deposit amount min-out recipient)
  (if (is-eq aid USDH) (contract-call? .v0-vault-usdh deposit amount min-out recipient)
  (if (is-eq aid stSTXbtc) (contract-call? .v0-vault-ststxbtc deposit amount min-out recipient)
  ERR-UNKNOWN-VAULT)))))))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1188-1197)
```text
    ;; Step 2: Deposit to vault to get zTokens (minted to user)
    ;; Now the market has the underlying tokens and can call vault-deposit
    (let ((shares-minted 
            (try! (if (is-eq ft-address ZEST-STX-WRAPPER-CONTRACT)
              ;; For wSTX: use as-contract with-stx pattern
              (as-contract? ((with-stx amount))
                (try! (vault-deposit asset-id amount min-shares account)))
              ;; For other tokens: use as-contract with-ft pattern
              (as-contract? ((with-ft ft-address "*" amount))
                (try! (vault-deposit asset-id amount min-shares account)))))))
```
