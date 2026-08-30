### Title
Missing validation of SIP-010 `transfer` response payload allows silent transfer failures to be treated as success - (File: `mainnet/contracts/market/v0-market-vault.clar`, `mainnet/contracts/vault/v0-vault-*.clar`)

### Summary
The Clarity analog of the reported ERC20 "returns false instead of reverting" bug is present in Zest's SIP-010 token-movement helpers. Several core private functions call `contract-call? <token> transfer ...` and only check the outer `(response bool uint)` variant via `try!`, never asserting that the inner boolean payload of a successful response is `true`. A token that returns `(ok false)` on a failed transfer (analogous to non-compliant ERC20s like BAT that return `false`) will pass `try!` unharmed, and the protocol will record collateral/deposits as received even though no value actually moved.

### Finding Description
`market-vault`'s `receive-tokens` helper simply forwards the trait call without inspecting its result: [1](#0-0) 

`collateral-add` then does `(try! (receive-tokens ft amount account))`, which only unwraps the `Response`'s `ok`/`err` variant — the unwrapped boolean value is discarded and never checked to be `true` — before crediting the user's collateral balance: [2](#0-1) 

The same pattern (`try!` on the transfer call, discarding/ignoring the returned bool) recurs in `send-tokens` in the same file, used by `collateral-remove`: [3](#0-2) 

It is also present in every vault's `receive-underlying`/`send-underlying` helpers (used by `deposit`/`redeem`), which explicitly discard the transfer's boolean result and hard-code `(ok true)` afterward regardless of what the token reported: [4](#0-3) [5](#0-4) [6](#0-5) 

`dao-treasury`'s `withdraw` exhibits the identical pattern (out of scope here since it is DAO-privileged, but confirms the systemic nature of the missing check): [7](#0-6) 

In Clarity, a well-behaved SIP-010 `transfer` implemented with the native `ft-transfer?` primitive always signals failure via the `err` branch of the `Response`, so `try!` alone is normally sufficient. However, several of the integrated collateral/underlying tokens are third-party, externally deployed contracts (e.g. `sbtc-token`, `usdcx`, `usdh-token-v1`, `ststxbtc-token-v2`) referenced directly by principal literal, not written or controlled by Zest: [8](#0-7) [9](#0-8) 

If any such external token implementation ever returns `(ok false)` on a failed transfer instead of an `err` response (the exact class of bug the original Trail-of-Bits Frax report flags for ERC20 tokens, just expressed through Clarity's response type instead of a boolean return value), Zest's `try!`-only checks will treat the failed transfer as a full success. The root cause is architectural: none of `receive-tokens`, `send-tokens`, `receive-underlying`, or `send-underlying` assert the unwrapped boolean equals `true`.

### Impact Explanation
If exploited against a token exhibiting this behavior, `collateral-add` would credit a user with collateral they never deposited, or `deposit` would mint vault shares/zTokens backed by no real assets. The user could then borrow against phantom collateral or redeem shares for real underlying funds pulled from other depositors, directly draining protocol reserves — this is a protocol-insolvency / direct-theft-of-user-funds scenario, matching the Critical impact tier.

### Likelihood Explanation
Likelihood is contingent on one of the already-integrated third-party token contracts (sBTC, USDC-proxy, USDH, stSTXbtc) having (now or in a future upgrade) a transfer path that returns `(ok false)` rather than erroring on failure — none of these are under Zest's control, so this cannot be ruled out by code review of Zest's own contracts, but likewise cannot be proven from the Zest repo alone. No DAO action or privileged registration is required to reach the vulnerable code path (it fires on every ordinary `collateral-add`/`deposit`/`collateral-remove`/`redeem` call), so if such a token exists, exploitation is directly reachable by any user.

### Recommendation
Add explicit boolean assertions after unwrapping every SIP-010 `transfer` call, e.g. `(asserts! (unwrap! (contract-call? asset transfer amount account current-contract none) ERR-TRANSFER-FAILED) ERR-TRANSFER-FAILED)`, in `receive-tokens`/`send-tokens` (`v0-market-vault.clar`) and in `receive-underlying`/`send-underlying` in every `v0-vault-*.clar` file, rather than relying solely on the outer `Response` variant via `try!`.

### Proof of Concept
1. Assume (hypothetically, per the report's bug-class hint) that one of the externally-controlled underlying tokens' `transfer` function contains a code path that returns `(ok false)` instead of an `err` response when the sender lacks sufficient balance.
2. A user with zero balance of that token calls `market.supply-collateral-add`/`deposit`, which internally invokes `receive-underlying`/`receive-tokens`. [10](#0-9) 
3. The token's `transfer` returns `(ok false)` — no funds move — but `try!` sees `ok` and continues.
4. `deposit` mints zToken shares to the user and increments `assets` as if real underlying had been received: [11](#0-10) 
5. The user now holds shares (or market collateral) backed by no real assets and can redeem/borrow against them, draining value from genuine depositors.

### Citations

**File:** mainnet/contracts/market/v0-market-vault.clar (L256-257)
```text
(define-private (receive-tokens (asset <ft-trait>) (amount uint) (account principal))
  (contract-call? asset transfer amount account current-contract none))
```

**File:** mainnet/contracts/market/v0-market-vault.clar (L259-265)
```text
(define-private (send-tokens (asset <ft-trait>) (amount uint) (account principal))
  (let ((asset-contract (contract-of asset)))
    (if (is-eq asset-contract ZEST-STX-WRAPPER-CONTRACT)
      (as-contract? ((with-stx amount))
          (try! (contract-call? asset transfer amount tx-sender account none)))
      (as-contract? ((with-ft asset-contract "*" amount))
          (try! (contract-call? asset transfer amount tx-sender account none))))))
```

**File:** mainnet/contracts/market/v0-market-vault.clar (L374-404)
```text
(define-public (collateral-add (account principal) (amount uint) (ft <ft-trait>) (asset-id uint))
  (let ((states (var-get pause-states))
        (entry (resolve-or-create account))
        (user-id (get id entry))
        (mask (get mask entry))
        (updated-mask (mask-update mask asset-id true true)) ;; collateral, insert
        (updated-entry (merge entry (refresh updated-mask)))
        (result (add-user-collateral user-id asset-id amount)))

    (try! (check-impl-auth))
    (asserts! (not (get collateral-add states)) ERR-PAUSED)
    (asserts! (> amount u0) ERR-AMOUNT-ZERO)

    (try! (receive-tokens ft amount account))
    
    (insert updated-entry)

    (print {
      action: "collateral-add",
      caller: contract-caller,
      data: {
        account: account,
        asset-id: asset-id,
        amount: amount,
        updated-collateral-amount: result,
        mask-before: mask,
        mask-after: updated-mask
      }
    })
      
    (ok result)))
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L291-299)
```text
(define-private (receive-underlying (amount uint) (account principal))
  (begin
    (try! (contract-call? 'SP120SBRBQJ00MCWS7TM5R8WJNTTKD5K0HFRC2CNE.usdcx transfer amount account current-contract none))
    (ok true)))

(define-private (send-underlying (amount uint) (account principal))
  (begin
    (try! (contract-call? 'SP120SBRBQJ00MCWS7TM5R8WJNTTKD5K0HFRC2CNE.usdcx transfer amount current-contract account none))
    (ok true)))
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L765-797)
```text
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

(define-public (redeem (amount uint) (min-out uint) (recipient principal))
  (let (
    (states (var-get pause-states))
```

**File:** mainnet/contracts/vault/v0-vault-sbtc.clar (L291-299)
```text
(define-private (receive-underlying (amount uint) (account principal))
  (begin
    (try! (contract-call? 'SM3VDXK3WZZSA84XXFKAFAF15NNZX32CTSG82JFQ4.sbtc-token transfer amount account current-contract none))
    (ok true)))

(define-private (send-underlying (amount uint) (account principal))
  (begin
    (try! (contract-call? 'SM3VDXK3WZZSA84XXFKAFAF15NNZX32CTSG82JFQ4.sbtc-token transfer amount current-contract account none))
    (ok true)))
```

**File:** mainnet/contracts/vault/v0-vault-ststxbtc.clar (L291-301)
```text
(define-private (receive-underlying (amount uint) (account principal))
  (begin
    (try! (contract-call? 'SP4SZE494VC2YC5JYG7AYFQ44F5Q4PYV7DVMDPBG.ststxbtc-token-v2 transfer amount account current-contract none))
    (ok true)))

(define-private (send-underlying (amount uint) (account principal))
  (begin
    (try! (as-contract? ((with-ft UNDERLYING "ststxbtc" amount))
      (try! (contract-call? 'SP4SZE494VC2YC5JYG7AYFQ44F5Q4PYV7DVMDPBG.ststxbtc-token-v2 transfer amount tx-sender account none))
      true))
    (ok true)))
```

**File:** mainnet/contracts/dao/dao-treasury.clar (L33-42)
```text
(define-public (withdraw (token <ft-trait>) (amount uint) (recipient principal))
  (let ((asset-contract (contract-of token)))
    (try! (check-dao-auth))

    (try! (if (is-eq asset-contract ZEST-STX-WRAPPER-CONTRACT)
      (as-contract? ((with-stx amount))
        (try! (contract-call? token transfer amount tx-sender recipient none)))
      (as-contract? ((with-ft asset-contract "*" amount))
        (try! (contract-call? token transfer amount tx-sender recipient none)))))

```

**File:** mainnet/contracts/vault/v0-vault-usdh.clar (L291-299)
```text
(define-private (receive-underlying (amount uint) (account principal))
  (begin
    (try! (contract-call? 'SPN5AKG35QZSK2M8GAMR4AFX45659RJHDW353HSG.usdh-token-v1 transfer amount account current-contract none))
    (ok true)))

(define-private (send-underlying (amt uint) (account principal))
  (begin
    (try! (contract-call? 'SPN5AKG35QZSK2M8GAMR4AFX45659RJHDW353HSG.usdh-token-v1 transfer amt current-contract account none))
    (ok true)))
```
