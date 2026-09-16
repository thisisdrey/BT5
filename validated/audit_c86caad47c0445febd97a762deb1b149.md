### Title
Malicious users can impersonate existing TRC10 tokens/organizations by minting duplicate-name assets once `ALLOW_SAME_TOKEN_NAME` is active - (File: actuator/src/main/java/org/tron/core/actuator/AssetIssueActuator.java)

### Summary
`AssetIssueActuator.validate()` only rejects a duplicate asset `name` when the chain parameter `AllowSameTokenName` is `0` (legacy mode). Once the `ALLOW_SAME_TOKEN_NAME` proposal is activated (as it has been on mainnet for years), any account can call `AssetIssueContract` to mint a new TRC10 token whose `name`, `abbr`, `url`, and `description` are byte-for-byte identical to those of an already-existing, well-known token, with the only enforced identity being an internal auto-incrementing numeric `id`. This mirrors the OpenQ report's bug class: an unprivileged caller can freely choose a human-facing identifier ("organization"/brand) that the protocol never binds to the legitimate owner, enabling impersonation.

### Finding Description
In `AssetIssueActuator.validate()`, the "Token exists" duplicate-name check is gated behind `dynamicStore.getAllowSameTokenName() == 0`: [1](#0-0) 

When `AllowSameTokenName` is `1` (the mainnet-activated mode, introduced specifically to let multiple tokens share the same name), this uniqueness check is skipped entirely, and only `name`/`abbr` format validation is performed: [2](#0-1) 

The only "identity" the protocol enforces for a new asset is a fresh numeric `tokenIdNum`, generated purely from an internal counter with no relation to any prior claim on the `name`: [3](#0-2) 

Because `name`, `abbr`, `description`, and `url` are all attacker-controlled free-form fields with no ownership binding (no oracle, no reserved-name registry, no KYC/UUID mapping analogous to what the OpenQ report recommends), any address holding the token-issuance fee can mint a token that is visually and semantically indistinguishable from a legitimate organization's token — the exact "pretend to be another organization" bug class from the report. Downstream lookup APIs such as `Wallet.getAssetIssueByName` even acknowledge that multiple tokens can share a name and require callers to disambiguate by ID: [4](#0-3) 

This is directly reachable by any anonymous API client / transaction broadcaster: a normal user submits an `AssetIssueContract` transaction (via HTTP/gRPC/JSON-RPC wallet endpoints), pays the `AssetIssueFee`, and the actuator executes with no owner-identity check on `name`/`abbr`/`url`.

### Impact Explanation
An attacker can issue a TRC10 token that impersonates a real organization's brand/token (same name, abbreviation, description, and URL), tricking exchanges, wallets, or end users into interacting with the fraudulent token — enabling phishing, social-engineering, and asset misdirection (users sending TRX/value intending to interact with the legitimate token but engaging the attacker's clone). This matches the report's "Impact" section on organization impersonation and is a concrete, unauthorized identity/impersonation vector reachable by any funded account, not merely a resource-exhaustion or cosmetic issue.

### Likelihood Explanation
Likelihood is high: this requires only a single signed `AssetIssueContract` transaction and payment of the standard `AssetIssueFee`; no special privilege, witness/SR status, or race condition is needed. Given `ALLOW_SAME_TOKEN_NAME` is the long-standing mainnet configuration, the guard is effectively inert in production, so the vulnerable path is always reachable.

### Recommendation
Reinstate a uniqueness/reservation mechanism for asset `name`/`abbr` even when `AllowSameTokenName` is enabled — e.g., maintain a global name-registry keyed by normalized name/abbr that binds the first issuer, or require an oracle-backed proof of off-chain organizational identity (analogous to `associateExternalIdToAddress`-style mappings) before allowing a token to reuse a name/abbr/url combination that matches an existing, actively-traded asset. At minimum, surface a strong warning/flag in `AssetIssueCapsule`/query APIs when a newly issued token's name collides with an existing one, and consider disallowing `url` reuse across distinct owners.

### Proof of Concept
1. Confirm `AllowSameTokenName == 1` (standard mainnet state).
2. Attacker crafts an `AssetIssueContract` with `name = "Tether"`, `abbr = "USDT"`, `url = "https://tether.to"`, `description` copied from the legitimate issuer's token, and pays `AssetIssueFee` from their own funded account.
3. Submit via `Wallet.createTransactionCapsule(..., ContractType.AssetIssueContract)` / the corresponding HTTP `/wallet/createassetissue` or gRPC endpoint.
4. `AssetIssueActuator.validate()` passes (no "Token exists" check under `AllowSameTokenName==1`); `execute()` mints a new token with a fresh numeric `id`, storing it in `AssetIssueV2Store`: [5](#0-4) 
5. Wallets/exchanges/users who look up the asset by `name` (as historically done via `getAssetIssueByName`) now encounter two tokens named "Tether"/"USDT" with no way to distinguish authenticity apart from manually checking the numeric ID against an out-of-band trusted source — enabling impersonation-based phishing.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/AssetIssueActuator.java (L72-76)
```java
      long tokenIdNum = dynamicStore.getTokenIdNum();
      tokenIdNum++;
      assetIssueCapsule.setId(Long.toString(tokenIdNum));
      assetIssueCapsuleV2.setId(Long.toString(tokenIdNum));
      dynamicStore.saveTokenIdNum(tokenIdNum);
```

**File:** actuator/src/main/java/org/tron/core/actuator/AssetIssueActuator.java (L84-87)
```java
      } else {
        assetIssueV2Store
            .put(assetIssueCapsuleV2.createDbV2Key(), assetIssueCapsuleV2);
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AssetIssueActuator.java (L165-190)
```java
    if (!TransactionUtil.validAssetName(assetIssueContract.getName().toByteArray())) {
      throw new ContractValidateException("Invalid assetName");
    }

    if (dynamicStore.getAllowSameTokenName() != 0) {
      String name = assetIssueContract.getName().toStringUtf8().toLowerCase(Locale.ROOT);
      if (("trx").equals(name)) {
        throw new ContractValidateException("assetName can't be trx");
      }
    }

    int precision = assetIssueContract.getPrecision();
    if (precision != 0
        && dynamicStore.getAllowSameTokenName() != 0
        && (precision < 0 || precision > ActuatorConstant.PRECISION_DECIMAL)) {
      throw new ContractValidateException("precision cannot exceed 6");
    }

    if ((!assetIssueContract.getAbbr().isEmpty()) && !TransactionUtil
        .validAssetName(assetIssueContract.getAbbr().toByteArray())) {
      throw new ContractValidateException("Invalid abbreviation for token");
    }

    if (!TransactionUtil.validUrl(assetIssueContract.getUrl().toByteArray())) {
      throw new ContractValidateException("Invalid url");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AssetIssueActuator.java (L210-214)
```java
    if (dynamicStore.getAllowSameTokenName() == 0
        && assetIssueStore.get(assetIssueContract.getName().toByteArray())
        != null) {
      throw new ContractValidateException("Token exists");
    }
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L1738-1742)
```java
      // check count
      if (builder.getAssetIssueCount() > 1) {
        throw new NonUniqueObjectException(
            "To get more than one asset, please use getAssetIssueById syntax");
      } else {
```
