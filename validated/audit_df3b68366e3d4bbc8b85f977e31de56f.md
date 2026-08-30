### Title
`transfer` in vault contracts trusts `tx-sender` as an authorization fallback, enabling tx.origin-style phishing theft of zToken shares - ([File: mainnet/contracts/vault/v0-vault-usdc.clar])

### Summary
Clarity's `tx-sender` is the analog of EVM's `tx.origin`: it is the original transaction signer and persists unchanged through the entire chain of nested `contract-call?`s, while `contract-caller` is the analog of `msg.sender` (the immediate caller). Every one of the six vault contracts (`v0-vault-usdc.clar`, `v0-vault-usdh.clar`, `v0-vault-stx.clar`, `v0-vault-sbtc.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`) implements the SIP‑10 `transfer` function with an authorization check that accepts *either* `tx-sender` or `contract-caller` matching the `from` parameter: [1](#0-0) 

This is precisely the `tx.origin`-as-authorization pattern the external report warns about: any user who is induced to call ("interact with") an unrelated malicious/upgradeable contract can have that contract silently drain their zToken vault-share balance, because the vault accepts `tx-sender == from` as sufficient authorization even when the direct caller (`contract-caller`) is not the victim at all.

### Finding Description
The vault `transfer` function is:
```
(define-public (transfer (amount uint) (from principal) (to principal) (memo (optional (buff 34))))
  (begin
    (try! (accrue))
    (asserts! (or (is-eq tx-sender from) (is-eq contract-caller from)) (err u4))
    (asserts! (not (is-eq current-contract to)) ERR-TOKENIZED-VAULT-PRECONDITIONS)
    (try! (ft-transfer? zft amount from to))
    ...
``` [2](#0-1) 

Here `from` and `to` are fully attacker-supplied arguments (not `contract-caller`, unlike `deposit`/`redeem`, which correctly bind `account` to `contract-caller`) [3](#0-2) . The `or` clause means the check is satisfied whenever `tx-sender` equals `from`, regardless of who the immediate caller (`contract-caller`) is. Since `tx-sender` in Clarity does not change across nested `contract-call?`s (it only changes inside `as-contract`), any intermediate contract in the call stack that ultimately calls the vault's `transfer` will see `tx-sender` still equal to the original human signer. A malicious contract can therefore pass `from = tx-sender` (the victim) and `to = attacker` and pass the authorization check even though the victim never directly called the vault and never intended this transfer.

This is functionally identical to the reported Vyper `tx.origin` bug: "any time a user calls/interacts with an unverified or upgradeable contract, they're put at risk, as the contract can act on the user's behalf." Here, the "acting on behalf" is moving the victim's zToken vault shares (which represent principal + accrued interest, i.e. real deposited value) to an address of the attacker's choosing.

### Impact Explanation
zToken vault shares represent a direct claim on deposited principal and accrued interest in the vault. An attacker who lures any victim into signing one transaction to an attacker-controlled contract (a common social-engineering vector — fake airdrop claim, fake mint, etc.) can have that contract call `transfer` on every Zest vault the victim holds a balance in, draining the victim's `from` position to the attacker's `to` address in the same transaction, with no allowance or explicit consent needed. This is a direct theft of user funds at rest, matching the Critical impact class.

### Likelihood Explanation
Likelihood is high in practice: exploitation requires no privileged access, no oracle manipulation, and no protocol misconfiguration — only a single victim-signed transaction to a contract the attacker controls, a standard phishing/social-engineering vector that is routinely observed on-chain. The vulnerable code path is reachable directly by any unprivileged principal calling the public `transfer` entry point on any of the six vaults.

### Recommendation
Remove the `tx-sender` branch from the authorization check and require `is-eq contract-caller from` only (i.e., require the immediate caller be `from`, matching the pattern already correctly used for `deposit`/`redeem`/`market.clar`'s `contract-caller == tx-sender` guard for direct-EOA calls, or by adding an explicit allowance/approval mechanism for delegated transfers).

### Proof of Concept
1. Victim holds zUSDC (or any z-token) balance in `v0-vault-usdc`.
2. Attacker deploys `Malicious.clar` with a public function, e.g. `claim-reward`, that internally does:
   `(contract-call? .v0-vault-usdc transfer <victim-balance> victim-principal attacker-principal none)`
3. Attacker convinces victim to call `claim-reward` on `Malicious.clar` (tx-sender = victim).
4. Inside the nested call to `v0-vault-usdc transfer`, `contract-caller` = `Malicious.clar`, but `tx-sender` is still the victim (Clarity preserves `tx-sender` across the call chain unless `as-contract` is used).
5. The check `(or (is-eq tx-sender from) (is-eq contract-caller from))` evaluates `is-eq tx-sender from` → `victim == victim` → true, bypassing the intended `contract-caller`-based authorization.
6. `ft-transfer? zft amount victim-principal attacker-principal` executes, moving the victim's zUSDC shares to the attacker without the victim's direct authorization of that specific transfer call.

### Citations

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L751-759)
```text
  (begin
    (try! (accrue))
    (asserts! (or (is-eq tx-sender from) (is-eq contract-caller from)) (err u4))
    (asserts! (not (is-eq current-contract to)) ERR-TOKENIZED-VAULT-PRECONDITIONS)
    (try! (ft-transfer? zft amount from to))
    (match memo to-print (print to-print) 0x)
    (ok true)))

;; -- Vault operations -------------------------------------------------------
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L765-769)
```text
      (account contract-caller)
      (CAP-SUPPLY (var-get cap-supply))
      (current-assets (var-get assets))
      (inkind (convert-to-shares-preview amount)))

```
