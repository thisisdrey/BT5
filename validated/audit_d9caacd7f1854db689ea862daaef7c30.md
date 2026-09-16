### Title
Unrestricted TRC10 asset-name squatting lets any account hijack name-based asset lookups (`Wallet.getAssetIssueByName` / `getAssetIssueListByName`) — ([File: framework/src/main/java/org/tron/core/Wallet.java])

### Summary
Once the `AllowSameTokenName` proposal is active (the state of current mainnet), `AssetIssueActuator.validate()` no longer enforces asset-name uniqueness — only the literal string `"trx"` is blocked. Any unprivileged account can pay the asset-issue fee and create a TRC10 token whose `name` field exactly matches an already-existing, legitimate token. Every wallet/exchange/dApp integration that resolves TRC10 tokens by unqualified name via the public `getassetissuebyname`/`getassetissuelistbyname` HTTP, gRPC, and Solidity/PBFT endpoints is then handed either a `NonUniqueObjectException` (silently converted to `null`) or, via the list variant, an unordered list containing both the legitimate asset and the attacker's spoofed asset with identical names, allowing the attacker's asset to be selected instead of the intended one. This is analogous to the reported PostgreSQL `CREATE TYPE`/`search_path` bug class: an unprivileged object creator can hijack another party's unqualified-name resolution to substitute an attacker-controlled object.

### Finding Description
`AssetIssueActuator.validate()` only checks for duplicate names when `getAllowSameTokenName() == 0` (legacy mode): [1](#0-0) 
Once same-token-name is allowed (current mainnet default), the sole remaining guard rejects only the literal name `"trx"`: [2](#0-1) 
Any address can therefore issue an arbitrary number of TRC10 tokens sharing the exact `name` (as `bytes`, case-sensitive) of any other already-issued, legitimate token, each getting its own unique numeric `id`.

`Wallet.getAssetIssueByName` performs an unqualified scan of `AssetIssueV2Store` filtered by `name`, and explicitly throws when it detects more than one match: [3](#0-2) 
This exception is caught by every exposed transport and silently turned into a `null`/empty response, making the legitimate, previously-working name resolution permanently fail for every caller from the moment the attacker's duplicate is created: [4](#0-3) [5](#0-4) [6](#0-5) 

The companion API, `Wallet.getAssetIssueListByName`, does no uniqueness check at all — it returns every asset matching the given name in an unordered list, so a caller that (as is common) reads the first entry can be silently handed the attacker's spoofed token instead of the legitimate one: [7](#0-6) 

Both APIs are reachable by any anonymous HTTP/gRPC client with no authorization, and the poisoning transaction (`AssetIssueContract`) is broadcastable by any funded account with no special privilege — exactly the "unprivileged object creator" precondition of the reported bug class.

### Impact Explanation
- Permanent denial of service for a specific, attacker-chosen asset name across `wallet/getassetissuebyname`, `walletsolidity/getassetissuebyname`, `walletpbft/getassetissuebyname`, and their gRPC equivalents: once a second same-named asset exists, `NonUniqueObjectException` is thrown forever (asset issuance is irreversible; there is no delete/rename path), so the API can no longer serve name-based lookups for that token.
- Hijack of name-based resolution via `getAssetIssueListByName`: a client trusting the returned list (or the first element) can be made to operate on the attacker's asset ID instead of the legitimate one, mirroring the "victim executes/uses the attacker's object instead of the intended one" impact of the source CVE. Depending on downstream handling (e.g. an exchange or wallet auto-selecting "the asset named X" to construct a `TransferAssetContract`/`ParticipateAssetIssueContract`), this can lead to funds being sent to or associated with the wrong (attacker-controlled) token/ID.
- Cost to the attacker is only the standard `AssetIssueFee` (paid to the blackhole), making the attack cheap and repeatable against any token name a victim integration might query.

### Likelihood Explanation
High reachability: any account can broadcast an `AssetIssueContract` transaction; no admin/witness/committee privilege is needed. The name-uniqueness gap is unconditional in the current (same-token-name-active) protocol state, which is the real-world mainnet configuration, so the vulnerable code path is always live. The only required precondition is paying the ordinary asset-issue fee.

### Recommendation
Enforce name-level uniqueness (or at minimum uniqueness scoped to "one asset name per resolvable lookup") even after `AllowSameTokenName` is active, e.g. reject `AssetIssueContract` transactions whose `name` collides with any existing asset in `AssetIssueV2Store`, or change `getAssetIssueByName`/`getAssetIssueListByName` callers to always require callers to disambiguate by `id` and deprecate/remove the ambiguous name-only public lookup, and stop silently swallowing `NonUniqueObjectException` into `null` (which masks the attack) — instead surface an explicit "ambiguous name" error so integrators do not treat a spoofed name-collision the same as "asset not found."

### Proof of Concept
1. Confirm mainnet is in `AllowSameTokenName == 1` state (current production state).
2. Attacker account (any funded account) queries `wallet/getassetissuebyname` for a well-known token name, e.g. `"WIN"`, confirming resolution to the legitimate asset's `id`.
3. Attacker broadcasts `AssetIssueContract` with `name = "WIN"` (any other required fields valid), paying the standard `AssetIssueFee`; `AssetIssueActuator.validate()` at `actuator/src/main/java/org/tron/core/actuator/AssetIssueActuator.java:169-214` allows it because only `"trx"` is blocked and no duplicate-name check applies when `AllowSameTokenName == 1`.
4. Re-query `wallet/getassetissuebyname` (or gRPC `WalletApi.getAssetIssueByName`) for `"WIN"`: it now returns `null`/empty forever due to `NonUniqueObjectException` at `framework/src/main/java/org/tron/core/Wallet.java:1739-1742`.
5. Query `wallet/getassetissuelistbyname` for `"WIN"`: the response list contains both the legitimate and the attacker's spoofed asset, with no indication which is authoritative, demonstrating the hijack of name-based resolution.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/AssetIssueActuator.java (L169-214)
```java
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

    if (!TransactionUtil
        .validAssetDescription(assetIssueContract.getDescription().toByteArray())) {
      throw new ContractValidateException("Invalid description");
    }

    if (assetIssueContract.getStartTime() == 0) {
      throw new ContractValidateException("Start time should be not empty");
    }
    if (assetIssueContract.getEndTime() == 0) {
      throw new ContractValidateException("End time should be not empty");
    }
    if (assetIssueContract.getEndTime() <= assetIssueContract.getStartTime()) {
      throw new ContractValidateException("End time should be greater than start time");
    }
    if (assetIssueContract.getStartTime() <= dynamicStore.getLatestBlockHeaderTimestamp()) {
      throw new ContractValidateException("Start time should be greater than HeadBlockTime");
    }

    if (dynamicStore.getAllowSameTokenName() == 0
        && assetIssueStore.get(assetIssueContract.getName().toByteArray())
        != null) {
      throw new ContractValidateException("Token exists");
    }
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L1707-1742)
```java
  public AssetIssueContract getAssetIssueByName(ByteString assetName)
      throws NonUniqueObjectException {
    if (assetName == null || assetName.isEmpty()) {
      return null;
    }

    BandwidthProcessor processor = new BandwidthProcessor(chainBaseManager);
    if (chainBaseManager.getDynamicPropertiesStore().getAllowSameTokenName() == 0) {
      // fetch from old DB, same as old logic ops
      AssetIssueCapsule assetIssueCapsule =
          chainBaseManager.getAssetIssueStore().get(assetName.toByteArray());
      if (assetIssueCapsule != null) {
        processor.updateUsage(assetIssueCapsule);
        return assetIssueCapsule.getInstance();
      } else {
        return null;
      }
    } else {
      // get asset issue by name from new DB
      List<AssetIssueCapsule> assetIssueCapsuleList =
          chainBaseManager.getAssetIssueV2Store().getAllAssetIssues();
      AssetIssueList.Builder builder = AssetIssueList.newBuilder();
      assetIssueCapsuleList
          .stream()
          .filter(assetIssueCapsule -> assetIssueCapsule.getName().equals(assetName))
          .forEach(
              issueCapsule -> {
                processor.updateUsage(issueCapsule);
                builder.addAssetIssue(issueCapsule.getInstance());
              });

      // check count
      if (builder.getAssetIssueCount() > 1) {
        throw new NonUniqueObjectException(
            "To get more than one asset, please use getAssetIssueById syntax");
      } else {
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L1774-1795)
```java
  public AssetIssueList getAssetIssueListByName(ByteString assetName) {
    if (assetName == null || assetName.isEmpty()) {
      return null;
    }

    List<AssetIssueCapsule> assetIssueCapsuleList =
        getAssetIssueStoreFinal(chainBaseManager.getDynamicPropertiesStore(),
            chainBaseManager.getAssetIssueStore(),
            chainBaseManager.getAssetIssueV2Store()).getAllAssetIssues();

    BandwidthProcessor processor = new BandwidthProcessor(chainBaseManager);
    AssetIssueList.Builder builder = AssetIssueList.newBuilder();
    assetIssueCapsuleList.stream()
        .filter(assetIssueCapsule -> assetIssueCapsule.getName().equals(assetName))
        .forEach(
            issueCapsule -> {
              processor.updateUsage(issueCapsule);
              builder.addAssetIssue(issueCapsule.getInstance());
            });

    return builder.build();
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/GetAssetIssueByNameServlet.java (L51-60)
```java
  private void fillResponse(boolean visible, ByteString address, HttpServletResponse response)
      throws Exception {
    AssetIssueContract reply =
        wallet.getAssetIssueByName(address);
    if (reply != null) {
      response.getWriter().println(JsonFormat.printToString(reply, visible));
    } else {
      response.getWriter().println("{}");
    }
  }
```

**File:** framework/src/main/java/org/tron/core/services/RpcApiService.java (L425-440)
```java
    @Override
    public void getAssetIssueByName(BytesMessage request,
        StreamObserver<AssetIssueContract> responseObserver) {
      ByteString assetName = request.getValue();
      if (assetName != null) {
        try {
          responseObserver.onNext(wallet.getAssetIssueByName(assetName));
        } catch (NonUniqueObjectException e) {
          responseObserver.onNext(null);
          logger.debug("Solidity NonUniqueObjectException: {}", e.getMessage());
        }
      } else {
        responseObserver.onNext(null);
      }
      responseObserver.onCompleted();
    }
```

**File:** framework/src/main/java/org/tron/core/services/RpcApiService.java (L1588-1603)
```java
    @Override
    public void getAssetIssueByName(BytesMessage request,
        StreamObserver<AssetIssueContract> responseObserver) {
      ByteString assetName = request.getValue();
      if (assetName != null) {
        try {
          responseObserver.onNext(wallet.getAssetIssueByName(assetName));
        } catch (NonUniqueObjectException e) {
          responseObserver.onNext(null);
          logger.debug("FullNode NonUniqueObjectException: {}", e.getMessage());
        }
      } else {
        responseObserver.onNext(null);
      }
      responseObserver.onCompleted();
    }
```
