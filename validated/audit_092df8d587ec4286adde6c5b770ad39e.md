### Title
`redeem` sends underlying assets back to the vault itself without checking, decoupling internal `assets` accounting from actual token balance - (File: mainnet/contracts/vault/v0-vault-stx.clar and equivalent v0-vault-*.clar / vault-*.clar files)

### Summary
The frxETHMinter analog is a function that pays out held funds to an arbitrary `to`/`recipient` address without verifying that address isn't the contract itself, which, combined with logic that treats "funds received" as newly-deposited/mintable, duplicates accounting. In the Zest vault contracts, `redeem` burns a caller's zShares and calls `send-underlying inkind recipient` without ever checking `recipient != current-contract`, unlike the `transfer` function in the same file which explicitly guards this with `(asserts! (not (is-eq current-contract to)) ERR-TOKENIZED-VAULT-PRECONDITIONS)`.

### Finding Description
`send-underlying` moves the underlying `wstx`/ft token from the vault to `recipient` [1](#0-0) . `redeem` calls it with a caller-supplied `recipient` with no self-address check, then unconditionally decrements the internal `assets` accounting variable: [2](#0-1) .

Critically, `total-assets`/`total-assets-preview` (used for all share-price/redemption math and treasury LP minting) are derived purely from the internal `assets` variable plus tracked interest/borrowed figures - not from the vault's actual on-chain token balance: [3](#0-2) . The `transfer` function in the same contract explicitly guards against sending zShares to the vault contract itself: [4](#0-3) , showing the developers are aware of the self-address hazard class, yet the same check is missing from `redeem` (and from `system-borrow`'s `send-underlying amount receiver` call): [5](#0-4) .

If `recipient` in `redeem` is set to the vault's own contract principal, `send-underlying` performs `(contract-call? .wstx transfer amt tx-sender account none)` where both `tx-sender` (as-contract) and `account` are the vault itself - a self-transfer that leaves the vault's actual `wstx`/STX balance unchanged. Meanwhile `(var-set assets (- current-assets inkind))` still deducts `inkind` from the internal `assets` ledger. This is the direct analog of the frxETHMinter bug: funds nominally "withdrawn" are actually retained by the contract, but the accounting subtracts them anyway, permanently decoupling recorded `assets` from the true underlying balance held by the vault. This same pattern is present in `vault-stx.clar`, `vault-sbtc.clar`, `vault-ststx.clar`, `vault-ststxbtc.clar`, `vault-usdc.clar`, `vault-usdh.clar` and their `v0-*` mainnet counterparts, since they share identical `redeem`/`send-underlying` logic.

### Impact Explanation
This creates a persistent divergence between the vault's actual underlying-token holdings and its accounted `assets` value used for all conversion math (`convert-to-shares-preview`, `convert-to-assets-preview`, treasury-LP minting in `accrue`). The shares that were burned in the self-redirect are permanently lost to the redeemer (they received nothing back, since the tokens returned to the vault instead of to them), while the recorded assets backing all remaining zShare holders is understated relative to the real balance. This is not merely a bookkeeping curiosity: it means real, tangible underlying value sitting in the vault becomes unaccounted for and effectively unclaimable through the normal share-based accounting (`assets` var never reflects it), which constitutes a permanent freezing of those funds - they cannot be withdrawn by anyone via `redeem`/`system-borrow` since the internal ledger no longer matches the real balance backing them.

### Likelihood Explanation
This requires no special privilege - any ordinary account holding zShares can call `redeem` with `recipient` set to the vault contract's own principal, exactly mirroring how the frxETHMinter bug required only that `moveWithheldETH`/`recoverEther`'s `to` parameter be set to the minter itself (there, restricted to owner/DAO; here, `redeem`'s recipient is fully attacker-controlled with no privilege needed at all, making it more directly reachable than the original finding).

### Recommendation
Add the same self-address guard already used in `transfer` to `redeem`'s `recipient` parameter (and to `system-borrow`'s `receiver`): `(asserts! (not (is-eq current-contract recipient)) ERR-TOKENIZED-VAULT-PRECONDITIONS)` before calling `send-underlying`. This prevents underlying assets from being paid back into the vault while the `assets` accounting variable is simultaneously decremented as though they left the contract.

### Proof of Concept
1. Attacker holds `N` zSTX shares in `v0-vault-stx` (or any of the vault-* contracts) worth `inkind` STX at current share price.
2. Attacker calls `redeem(N, 0, <vault-stx-contract-principal>)` - passing the vault's own contract principal as `recipient`.
3. `ft-burn? zft N account` burns the attacker's shares [6](#0-5) .
4. `send-underlying inkind recipient` executes `contract-call? .wstx transfer inkind current-contract current-contract none` - a self-transfer; the vault's real `wstx`/STX balance is unchanged [1](#0-0) .
5. `(var-set assets (- current-assets inkind))` still deducts `inkind` from the internal ledger [7](#0-6) .
6. The vault's real token balance now exceeds its recorded `assets`, permanently orphaning `inkind` worth of underlying value that is unreachable through the vault's share-accounting-based `redeem`/borrow flows.

### Citations

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L296-301)
```text
(define-private (send-underlying (amt uint) (account principal))
  (begin
    (try! (as-contract? ((with-stx amt))
      (try! (contract-call? .wstx transfer amt tx-sender account none))
      true))
    (ok true)))
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L334-346)
```text
(define-private (total-assets)
  (let ((current-assets (var-get assets))
        (debt (total-debt))
        (borrowed (var-get total-borrowed))
        (interest (if (> debt borrowed) (- debt borrowed) u0)))
    (+ current-assets interest)))

(define-private (total-assets-preview)
  (let ((current-assets (var-get assets))
        (debt (debt-preview))
        (borrowed (var-get total-borrowed))
        (interest (if (> debt borrowed) (- debt borrowed) u0)))
    (+ current-assets interest)))
```

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

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L797-821)
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

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L865-900)
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

    (var-set principal-scaled updated-scaled-principal)
    (var-set total-borrowed (+ (var-get total-borrowed) amount))
    (try! (send-underlying amount receiver))

    (print {
      action: "system-borrow",
      caller: contract-caller,
      data: {
        receiver: receiver,
        amount: amount,
        scaled-amount: scaled-amount,
        principal-scaled: updated-scaled-principal,
        total-borrowed: (var-get total-borrowed),
        index: idx
      }
    })

    (ok true)))
```
