### Title
Unprivileged users can permanently lock (silently "burn") vault shares via `deposit`'s unguarded `recipient` parameter, unlike `transfer` - (File: mainnet/contracts/vault/v0-vault-stx.clar)

### Summary
The vault contracts' `transfer` function explicitly forbids sending `zft` shares to the vault's own contract principal, but the `deposit` function's `recipient` parameter — which is the mint destination for newly issued shares — has no equivalent guard. This is the same class of bug as the GNTDeposit report: a function meant to move value to a caller-controlled destination allows an unprivileged/ordinary principal to redirect value to an address from which it can never be recovered, silently destroying it outside of the protocol's normal accounting path (`ft-burn?`).

### Finding Description
In every vault contract (`v0-vault-stx.clar`, `v0-vault-sbtc.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`, `v0-vault-usdc.clar`, `v0-vault-usdh.clar`), `transfer` explicitly checks that the destination is not the vault contract itself: [1](#0-0) 

This guard exists specifically because sending `zft` shares to `current-contract` would make them permanently unrecoverable: no code path lets `contract-caller` become `current-contract` in a subsequent `transfer` or `redeem` call, so shares held at that address can never be moved out or redeemed.

However, `deposit` mints new shares directly to a caller-supplied `recipient` with no such check: [2](#0-1) 

Any ordinary, unprivileged caller can invoke `deposit` with `recipient` set to the vault's own contract principal (`current-contract`). The `ft-mint?` call succeeds, `total-supply` of `zft` increases, and `assets` is incremented as normal — but the newly minted shares are irretrievably stuck at the vault's own address, since `transfer`'s guard prevents anyone from ever moving shares *out* through that path and no other function allows `contract-caller` to equal `current-contract` and call `redeem` on the vault's own behalf.

This mirrors the GNTDeposit `withdraw` bug precisely: a normal-user-callable function that is supposed to move value to any address effectively allows silently destroying that value (locking shares forever) without the value passing through the protocol's designated burn function (`ft-burn?` in `redeem`), and without decrementing `total-supply`/`assets` consistently the way a proper `redeem` would.

### Impact Explanation
Shares minted to `current-contract` are permanently and irrecoverably locked — this is a permanent freezing of funds. Because `total-supply` of `zft` keeps counting these unredeemable shares forever, the share-price math used throughout the protocol (`convert-to-shares-preview`/`convert-to-assets-preview`, and the `accrue` treasury-lp minting calculation) permanently carries "phantom" supply that no legitimate holder can ever redeem, degrading share-price accounting integrity over time as more such deposits accumulate. This falls under the Critical impact bucket ("permanent freezing of funds") in the vault share math / interest accrual area explicitly listed in scope.

### Likelihood Explanation
Medium-to-low. It requires a caller (an ordinary user, or more critically a third-party integrator contract calling `deposit` on a user's behalf with an attacker-influenced `recipient` field) to intentionally or mistakenly pass the vault's own contract principal as `recipient`. Since the protocol developers demonstrably considered and mitigated this exact scenario for `transfer` but not for `deposit`, this is a plausible oversight rather than a purely theoretical edge case, and it is trivially reachable by any unprivileged caller with a single `deposit` call.

### Recommendation
Add the same guard used in `transfer` to `deposit` (and audit `redeem`'s `recipient` for the underlying-asset transfer as well):
```clarity
(asserts! (not (is-eq current-contract recipient)) ERR-TOKENIZED-VAULT-PRECONDITIONS)
```
Apply this fix consistently across all six vault contracts (`v0-vault-stx.clar`, `v0-vault-sbtc.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`, `v0-vault-usdc.clar`, `v0-vault-usdh.clar`).

### Proof of Concept
1. Attacker (an ordinary, unprivileged principal) calls:
   `(contract-call? .v0-vault-stx deposit amount min-out .v0-vault-stx)`
   i.e., `recipient` = the vault contract's own principal.
2. `deposit` executes normally: `receive-underlying` pulls in the attacker's funds, `assets` is incremented, and `(ft-mint? zft inkind recipient)` mints `inkind` shares directly to `.v0-vault-stx` itself. [3](#0-2) 
3. The shares now held at `.v0-vault-stx` can never be moved: any attempt to `transfer` them out is blocked because the sender check requires `tx-sender`/`contract-caller` to equal `from` (the vault contract itself, which no external call can satisfy), and no `redeem` path can be invoked with `contract-caller` equal to the vault's own principal. [4](#0-3) 
4. Result: the deposited value is permanently locked, `total-supply` of `zft` is permanently inflated with unredeemable shares, and this discrepancy persists in every subsequent share-price and treasury-LP-minting calculation.

### Citations

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L752-759)
```text
(define-public (transfer (amount uint) (from principal) (to principal) (memo (optional (buff 34))))
  (begin
    (try! (accrue))
    (asserts! (or (is-eq tx-sender from) (is-eq contract-caller from)) (err u4))
    (asserts! (not (is-eq current-contract to)) ERR-TOKENIZED-VAULT-PRECONDITIONS)
    (try! (ft-transfer? zft amount from to))
    (match memo to-print (print to-print) 0x)
    (ok true)))
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
