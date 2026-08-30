### Title
`send-underlying` in the sBTC/USDC/USDH vaults omits the `as-contract?`/`with-ft` capability wrapper, causing the underlying-token `transfer` to always revert on withdrawal - (File: `mainnet/contracts/vault/v0-vault-sbtc.clar`, `mainnet/contracts/vault/v0-vault-usdc.clar`, `mainnet/contracts/vault/v0-vault-usdh.clar`)

### Summary
This is the Clarity analog of the reported Solidity bug: a contract tries to move its own custody of an asset ("self → user") through a token-transfer call that internally checks who is authorized to move funds out of the `sender` account, but the contract never grants itself that authorization before making the call. In Solidity the missing authorization is an ERC20 `allowance`; in this Clarity codebase the missing authorization is the `as-contract?`/`with-ft` asset-outflow capability that the SIP-010 `transfer` implementation checks against.

### Finding Description
The underlying SIP-010 `transfer` function used by these tokens authorizes a transfer only if the acting principal is the `sender`: [1](#0-0) 

`send-underlying` in `v0-vault-sbtc.clar` calls this `transfer` with `current-contract` (the vault itself) as the `sender`/`from` argument, but does **not** wrap the call in `as-contract?`: [2](#0-1) 

The same unguarded pattern exists in `v0-vault-usdc.clar`: [3](#0-2) 

and in `v0-vault-usdh.clar`: [4](#0-3) 

When `redeem`/`withdraw`-style entry points are called directly by an ordinary user, `tx-sender`/`contract-caller` is the user's principal, not the vault contract. Since `send-underlying` is not executed inside `as-contract?`, the token `transfer` call's `sender` argument (`current-contract`) never equals `tx-sender` or `contract-caller`, so the SIP-010 authorization check `(or (is-eq tx-sender sender) (is-eq contract-caller sender))` fails and the whole call reverts.

This is confirmed by contrast: the STX, stSTX, and stSTXbtc vaults correctly wrap the exact same operation in `as-contract? ((with-ft ...))` (or `with-stx`), which switches `tx-sender`/`contract-caller` to the vault's own principal for the duration of the call and grants the exact outflow allowance needed, matching the SIP-010 check: [5](#0-4) [6](#0-5) 

The sBTC/USDC/USDH vaults are missing this wrapper entirely, which is the direct structural analog of the reported bug: a self-transfer that requires an authorization step the contract never performs on itself.

### Impact Explanation
Any deposit into the sBTC, USDC, or USDH vault can never be withdrawn/redeemed through `send-underlying`, because every call reverts at the token-authorization check. Since the code path is unconditionally broken (not a config or liquidity issue) and cannot self-heal, user principal and any accrued interest sitting in these vaults are permanently frozen absent a contract upgrade — this lands on **Critical: permanent freezing of funds**.

### Likelihood Explanation
High. This triggers on every single call to any vault function that reaches `send-underlying` (e.g., `redeem`), for any ordinary user, with no special preconditions, and every attempt reverts deterministically.

### Recommendation
Wrap `send-underlying` in `v0-vault-sbtc.clar`, `v0-vault-usdc.clar`, and `v0-vault-usdh.clar` with `as-contract? ((with-ft UNDERLYING "<token-name>" amount))` (mirroring `v0-vault-ststx.clar`/`v0-vault-stx.clar`/`v0-vault-ststxbtc.clar`) so the vault is properly authorized as `sender`/`tx-sender` when it moves its own custody of the underlying asset to the redeeming user.

### Proof of Concept
1. User calls `deposit` on `v0-vault-sbtc.clar` (or `-usdc`/`-usdh`); this succeeds because `receive-underlying` uses `sender = account = tx-sender`, satisfying the SIP-010 check. [7](#0-6) 
2. User calls `redeem`/`withdraw`, which internally invokes `send-underlying amount account`, i.e. `contract-call? sbtc-token transfer amount current-contract account none` executed with `tx-sender = <user>` (not wrapped in `as-contract?`).
3. Inside `sbtc-token.transfer`, `sender = current-contract` (the vault), but `tx-sender`/`contract-caller` is the user — the check `(or (is-eq tx-sender sender) (is-eq contract-caller sender))` fails, `err-not-token-owner` is returned, and the whole redeem transaction reverts.
4. Funds deposited into these three vaults can never be withdrawn.

### Citations

**File:** local-testing/contracts/utility/token/sbtc.clar (L17-22)
```text
(define-public (transfer (amount uint) (sender principal) (recipient principal) (memo (optional (buff 34))))
  (begin
    (asserts! (or (is-eq tx-sender sender) (is-eq contract-caller sender)) err-not-token-owner)
    (ft-transfer? sbtc amount sender recipient)
  )
)
```

**File:** mainnet/contracts/vault/v0-vault-sbtc.clar (L291-294)
```text
(define-private (receive-underlying (amount uint) (account principal))
  (begin
    (try! (contract-call? 'SM3VDXK3WZZSA84XXFKAFAF15NNZX32CTSG82JFQ4.sbtc-token transfer amount account current-contract none))
    (ok true)))
```

**File:** mainnet/contracts/vault/v0-vault-sbtc.clar (L296-299)
```text
(define-private (send-underlying (amount uint) (account principal))
  (begin
    (try! (contract-call? 'SM3VDXK3WZZSA84XXFKAFAF15NNZX32CTSG82JFQ4.sbtc-token transfer amount current-contract account none))
    (ok true)))
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L296-299)
```text
(define-private (send-underlying (amount uint) (account principal))
  (begin
    (try! (contract-call? 'SP120SBRBQJ00MCWS7TM5R8WJNTTKD5K0HFRC2CNE.usdcx transfer amount current-contract account none))
    (ok true)))
```

**File:** mainnet/contracts/vault/v0-vault-usdh.clar (L296-299)
```text
(define-private (send-underlying (amt uint) (account principal))
  (begin
    (try! (contract-call? 'SPN5AKG35QZSK2M8GAMR4AFX45659RJHDW353HSG.usdh-token-v1 transfer amt current-contract account none))
    (ok true)))
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L296-301)
```text
(define-private (send-underlying (amt uint) (account principal))
  (begin
    (try! (as-contract? ((with-stx amt))
      (try! (contract-call? .wstx transfer amt tx-sender account none))
      true))
    (ok true)))
```

**File:** mainnet/contracts/vault/v0-vault-ststx.clar (L296-301)
```text
(define-private (send-underlying (amount uint) (account principal))
  (begin
    (try! (as-contract? ((with-ft UNDERLYING "ststx" amount))
      (try! (contract-call? 'SP4SZE494VC2YC5JYG7AYFQ44F5Q4PYV7DVMDPBG.ststx-token transfer amount tx-sender account none))
      true))
    (ok true)))
```
