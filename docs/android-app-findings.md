# Android App Decompile Findings

## APK metadata

- **Newer version (analyzed):** `com.vizio.vue.launcher` 5.0.0.rc-3.20260311.20.release (versionCode 28228, posted 2026-03-21, min API 29). Package label: "VIZIO | WatchFree+". Source: APKMirror `.apkm` bundle, `base.apk` extracted (~144 MB).
- **Older version:** `uptodown-com.vizio.vue.launcher.apk` (~9.8 MB) was **mislabeled** — its `AndroidManifest.xml` declares `package="com.uptodown"` (the Uptodown app store), not the Vizio app. Decompilation was attempted (jadx ran to completion) but no `com/vizio` namespace exists; it was discarded as not relevant. Only the newer APK provided ground truth. Recommendation: re-download an actual older Vizio APK if cross-version comparison is desired.
- **Newest version analyzed:** 5.3.0.rc-2.20260728.21.release (versionCode 28238, posted 2026-07-28). Pulled both as `.xapk` (APKPure) and `.apkm` (APKMirror) — the two `base.apk` files are **byte-identical by SHA-256**, and only `base.apk` carries dex in either; every other entry is a `split_config.*` resource or native-lib split. There is no feature split, from either mirror.
- **5.0.0 remains the authoritative decompile, because 5.3.0 is obfuscated.** Under `com/vizio`, 5.0.0 has **0 single-letter class names out of 25,196**; 5.3.0 has **3,277 out of 16,661**. Vizio enabled R8 name obfuscation for its own namespace between 2026-03-11 and 2026-07-28, closing the window that made this analysis easy. What still works in 5.3.0: **string literals are not encrypted** (`/key_command/`, `/pairing/start`, `/state/device/deviceinfo` all appear verbatim), so endpoint-path greps over the raw dex remain trustworthy even where class and method names are gone. Package names are also partly retained (e.g. `com/vizio/connectivity/domain/volume_remote/` with classes `a`, `b`, `c`).
- **Where to get APKs:** APKMirror, APKPure and apkcombo all return Cloudflare 403 to `curl`. The APKPure *app* API works headlessly and is sufficient — `api.pureapk.com/m/v3/cms/app_version?package_name=com.vizio.vue.launcher` with headers `x-cv: 3172501`, `x-sv: 29`, `x-abis`, `x-gp: 1` and UA `APKPure/3.17.25 (Aegon)`; it returns protobuf containing signed `download.pureapk.com` URLs that require the same headers. **Verify the package before decompiling** — `~/Downloads/smartcast.apk` is an Aptoide-wrapped store app (`com/aptoide/*`), the same trap as the uptodown file below.
- **Tooling:** `jadx 1.5.5` (Homebrew) for Java decompilation; `unzip` for APKM extraction; `apktool 3.0.2` available but unused (jadx output sufficient). Working directory `/tmp/vizio-apk-analysis/`. **jadx gotcha:** running it against the whole APK silently drops `V2SCPApi.java` — the single most valuable file — emitting only its `$…` inner classes, and it drops other large classes the same way. That can masquerade as "this package isn't in the build". Extract the dex files and run jadx per `classes*.dex`, and before concluding anything is absent, check the raw dex string pool with `grep -a` for type descriptors (`Lcom/vizio/…;`) and string literals — that is the ground truth, not jadx's output tree.
- **Decompiled tree:** ~49,000 `.java` files for the newer APK. Heavy obfuscation in third-party code, but the entire `com/vizio/*` namespace is **un-obfuscated** (Kotlin metadata strings preserve original class/property names). This made the analysis straightforward.

High-value packages: `com.vizio.connectivity.data.{network.impl.DeviceApiRoutes, network.models.ResponseResult, network.responses.*, scpl.SCPLCommandsManager, discovery.*}`, `com.vizio.connectivity.domain.commands.KeyCommandItem`, `com.vizio.vnf.swagger.apis.V2SCPApi` (the 4230-line generated Swagger client containing every endpoint), `com.vizio.vnf.network.message.{SmartCastHeaders, network.EmbeddedConnectionConfig, Retry}`, `com.vizio.vdf.services.control.command.*`, and `com.vizio.smartcast.menutree.models.enums.VZRestEndpoint` (the 177-entry legacy menu-tree taxonomy).

## Question-by-question findings

### 1. HASHVAL algorithm

**Verdict:** **CONFIRMED OPAQUE — server-assigned, client just echoes.**

**Evidence:**

`com/vizio/connectivity/data/network/impl/Body.java` (lines 19–47):

```java
@Serializable
public final /* data */ class Body {
    private final Long hashval;
    private final String request;
    private final String value;
    ...
    @SerialName("HASHVAL")
    public static /* synthetic */ void getHashval$annotations() {}
```

`hashval` is a plain `Long` data field on the request body — there is no constructor that derives it, no helper, no transform. The only way the field gets populated is by reading it from a prior GET response and passing it through.

The smoking gun is `com/vizio/vdf/services/control/command/SettingsCommandStrategy.java` (lines 99–116):

```java
@Override // CombinedCommand
public Command<?> getGetCommand() {
    return DeviceCommandBuilder.getMenuSettingCommand$default(
        CommandStrategyKt.getBuilder(),
        this.authToken, this.settingsRoot, this.endpoint, null, 8, null);
}

@Override
public Command<PutBasicResponse> processResponse(CommandResult<? extends Object> statusResponse) {
    SettingResponseItem[] items;
    SettingResponseItem settingResponseItem;
    Long hashval;
    Object result = statusResponse.getResult();
    SettingResponse settingResponse = result instanceof SettingResponse ? (SettingResponse) result : null;
    if (settingResponse == null
            || (items = settingResponse.getITEMS()) == null
            || (settingResponseItem = (SettingResponseItem) ArraysKt.getOrNull(items, 0)) == null
            || (hashval = settingResponseItem.getHASHVAL()) == null) {
        return null;
    }
    return DeviceCommandBuilder.updateMenuSettingCommand$default(
        CommandStrategyKt.getBuilder(), this.authToken, this.settingsRoot, this.endpoint,
        new Body("MODIFY", this.value, Long.valueOf(hashval.longValue())), null, 16, null);
}
```

The pattern is universal across `SetDeviceNameCommand`, `MuteToggleCommand`, `SetVolumeCommand`, `InputCommandStrategy`, `AudioSettingsCommandStrategy`, `PowerCommand` — every "modify" is a `CombinedCommand` doing GET → extract `HASHVAL` from `ITEMS[0]` → echo in PUT.

There is one general-purpose hashing utility `com/vizio/redwolf/utils/hashing/HashingUtil.java` (SHA-1/SHA-256/MD5/HMAC-SHA256) — but it is used for telemetry IDs only, not for HASHVAL. Searching the entire `com/vizio` tree for any function returning a value that flows into a `HASHVAL` field finds zero matches. The device assigns it.

The enum `com/vizio/connectivity/data/network/models/ResponseResult.java` line 35 also includes `HASHVAL_ERROR = "HASHVAL_ERROR"` — confirming the server-side semantics: HASHVAL is an optimistic-concurrency token, and stale values yield this error.

**Implication for vizaio:** existing implementation (always GET-then-PUT) is exactly what the official app does. Document HASHVAL as "opaque, server-assigned, mandatory echo on MODIFY for menu items, returns HASHVAL_ERROR if stale" with full confidence — no algorithm to mine. There is no HASHVAL on `pairing/*`, `key_command/`, `app/launch`, `state/device/*` — only on settings under `menu_native/...`.

#### Hardware verification (VHD24M-0810, fw 3.720.9.1-1)

Live probing the TV confirmed and sharpened the above. HASHVAL is a
**deterministic, per-item content tag** the device mints **locally** (it is the
LAN HTTP server — `lighttpd/1.4.67`; no internet involved), used as an
`If-Match`/ETag-style optimistic-concurrency precondition:

- **Deterministic & stable.** Repeated reads of an unchanged item return the
  identical HASHVAL; live `name_input` values matched fixtures captured months
  earlier. Not a per-request nonce.
- **A pure, reversible function of the item's content.** Holding the `volume`
  item's identity fixed and sweeping only `VALUE`: `7↔2904664803`,
  `10↔1731828541`, `15↔545576040` — every time; setting the value back restores
  the exact prior HASHVAL.
- **Identity-dependent, not value-only.** The same value string yields different
  HASHVALs in different items (`"SMARTCAST"` → `2023834057` as `current_input`
  vs `1541296275` as the `cast` input), so item identity (cname/name/type/path)
  is folded in alongside the value.
- **Not reproducible from the API surface, and not linear in the value.** No
  match across CRC32 (4 polynomials), Adler32, FNV-1/1a-32, Java `hashCode`, or
  md5/sha truncations over 15k+ serializations of the visible fields (±URI); a
  GF(2) affine test (measuring the `H(0), H(2^i)` basis) ruled out CRC32 over a
  fixed-offset binary value field. So it hashes the device's internal
  representation (hidden bytes), with the value mixed in as decimal-ASCII and/or
  via a non-linear hash. Effectively opaque, as the APK search already implied.

**On a write you echo the *current* state's HASHVAL, not the target's** —
verified directly on `volume` (current value 7 → `H7`, target 10 → `H10`):

| HASHVAL sent on `PUT VALUE=10` | Result |
| --- | --- |
| `H7` (current/previous state) | `SUCCESS` → value becomes 10 |
| `H10` (the target value's hash) | `HASHVAL_ERROR` → unchanged |
| garbage (`999999`) | `HASHVAL_ERROR` |
| omitted | `INVALID_PARAMETER` |

This is the deep reason the GET cannot be skipped: the write must prove it has
seen current state by echoing *its* tag. There is no offline limitation and no
client-side calculation worth adding — GET → echo → PUT is correct.

### 2. Auth token semantics

**Verdict:** **CONFIRMED — opaque random string assigned at pairing, no expiry, no refresh, no JWT, simple plain header.**

**Evidence:**

`com/vizio/vnf/network/message/SmartCastHeaders.java`:

```java
public static final String HEADER_KEY_AUTH = "AUTH";                // line 9
public static final String HEADER_KEY_CLIENT = "User-Agent";        // line 10
public static final String HEADER_KEY_SMARTCAST_SOURCE = "VIZIO-SmartCast-Source";
public static final String HEADER_KEY_VOICE_SOURCE = "VIZIO-Voice-Source";
public static final String SMARTCAST_SOURCE_ANDROID = "SMARTCAST_ANDROID";
public static final String KTOR_CLIENT = "Ktor client";
```

Every authenticated request in `com/vizio/vnf/swagger/apis/V2SCPApi.java` (4230 lines) builds the request map exactly as `MapsKt.mapOf(TuplesKt.to("AUTH", AUTH))` — a single header with literal key `AUTH` and the token string as value. Not `Authorization`, not `Bearer`, no encoding. (One vestigial line in `EmbeddedConnectionConfig.java:34` calls `MapsKt.plus(headers, TuplesKt.to("Authorization", str))` but the result is discarded; the `AUTH` header is the canonical one.)

Searches across the whole tree for `refreshToken`, `tokenExpiry`, `jwt`, `JWT`, `Bearer` returned no matches inside `com.vizio` — there is no token refresh mechanism. Storage is via Room (`com/vizio/connectivity/data/room/PairedWifiDeviceDao_Impl.java` etc.) — plain SQLite, no `EncryptedSharedPreferences`.

**Implication:** pyvizio's "auth token never expires, persist verbatim" assumption is correct. No refresh logic to add. Device-id binding: the auth token is bound on the device side to whatever client identity was sent in `pairing/start` (DEVICE_ID + DEVICE_NAME) — the app keeps a stable per-installation device-id in the Room DB and reuses it.

### 3. KEYLIST max length and ACTION values

**Verdict:** **LIKELY UNLIMITED — the app does not chunk; ACTION value is exclusively `"KEYPRESS"`.**

**Evidence:**

`com/vizio/connectivity/data/network/requests/KeyCommandRequest.java` lines 24, 42:

```java
public final class KeyCommandRequest {
    private final List<KeyListItem> keyListItem;
    ...
    @SerialName("KEYLIST")
    public static /* synthetic */ void getKeyListItem$annotations() {}
```

The whole `KEYLIST` is a single Kotlin `List<KeyListItem>` with no pre-send transform.

`com/vizio/connectivity/domain/commands/KeyCommandItem.java` defines 156 keys total (12 transport + 5 dpad + 7 audio + 17 navigation + 105 ASCII + 4 channel + 3 power/picture + 3 input). Every constructor uses the literal third argument `"KEYPRESS"` — no `KEYDOWN`, `KEYUP`, `KEYHOLD`, or other ACTION values exist anywhere in the decompiled source. Sample (line 24):

```java
public static final KeyCommandItem TRANSPORT_FAST_REVERSE
    = new KeyCommandItem("TRANSPORT_FAST_REVERSE", 0, new KeyListItem(2, 1, "KEYPRESS"));
```

A grep for `chunked|MAX_KEY|maxKey|keyList\.size` over all of `com/vizio` finds chunking only in `VoiceSearch` (audio bytes, 1024-byte chunks) and `HexEncodeDecodeUtils` (string hex pairs) — never the KEYLIST. The app sends however many items the caller passes in one PUT.

`com/vizio/smartcast/analytics/FirebaseConsumer.java:22` defines `private static final int MAX_KEY_LENGTH = 40;` but that constant is for analytics event-name truncation, unrelated to key commands.

**Implication:** No client-side cap; pyvizio can keep sending arbitrarily long key lists. If users hit a device-side limit, it would manifest server-side, but the official app makes no effort to prevent it. Probable practical answer: the device buffer is large because the Vizio app sends multi-character strings (keyboard search) without splitting. Recommend pyvizio document "no client-side chunking required" but cap defensively (e.g., 50) to avoid worst-case device buffer overruns.

### 4. Concurrent request handling

**Verdict:** **NOT-FOUND — no semaphore, no max-connection limit, no global mutex on HTTP layer.**

**Evidence:**

The HTTP transport is Ktor on Android. `com/vizio/vnf/network/agent/http/HttpClient.java` builds `HttpClientKt.HttpClient(HttpClientEngineFactory.buildAndroidEngine$default(...))` and never installs a request-throttling plugin; nothing references `Semaphore`, `maxRequests`, `maxRequestsPerHost`, or `connectionPool` anywhere inside `com/vizio`. The `kotlinx.coroutines.sync.Mutex` references are all in repository/data layers (`AppRepository`, `ProtoDataStoreManager`, `TrackEventsUtils`, `WebsocketDiscoveryPipeline`), guarding their own state — never request issuance.

`EmbeddedConnectionConfig.java` (defaults): `readTimeoutMillis = 10000L`, `writeTimeoutMillis = 3000L`, `connectTimeoutMillis = 2000L`. Per-request timeouts only — no concurrency cap.

**Implication:** pyvizio's recently-added semaphore is **stricter than the official app** — the app fires concurrent requests freely (each ViewModel/Service in its own coroutine) and relies on Ktor's default OkHttp pool. The fact that pyvizio's semaphore solved real-world problems means devices throttle/misbehave on concurrency, and pyvizio's defensive throttling is justified. The official app likely benefits from being on the same LAN with sub-millisecond RTT and lower overall throughput than a polling integration. Keep the semaphore.

### 5. Pairing cooldowns and re-pair behavior

**Verdict:** **CONFIRMED — no cooldown, no programmatic retry; errors are surfaced to UI for human action.**

**Evidence:**

`com/vizio/connectivity/data/network/models/ResponseResult.java` enumerates the full pairing-result vocabulary:

```
SUCCESS, FAILURE, URI_NOT_FOUND, ABORTED, BUSY, BLOCKED,
REQUIRES_PAIRING, REQUIRES_SYSTEM_PIN, REQUIRES_NEW_SYSTEM_PIN,
PAIRING_DENIED, CHALLENGE_INCORRECT, VALUE_OUT_OF_RANGE,
MAX_CHALLENGES_EXCEEDED, TOO_MANY_PAIRED_DEVICES,
INVALID_PARAMETER, READ_ONLY_ERROR, HASHVAL_ERROR
```

`com/vizio/connectivity/data/utils/ConnectivityConstants.java:42` adds `public static final int SCPL_ERROR_CODE_PAIRING_DENIED = 13;` — there is a numeric pairing-denied code at the SCPL layer.

`com/vizio/connectivity/ui/main_flow/viewmodel/DevicePairingViewModel.java:354–372` shows how each error is handled:

```java
private final PairingSessionState.Error getPairingError(DevicePairingResult.Error devicePairingResult) {
    String message = devicePairingResult.getMessage();
    if (Intrinsics.areEqual(message, ResponseResult.MAX_CHALLENGES_EXCEEDED.getResult())) {
        return new PairingSessionState.Error(PairingSessionError.MaxChallenges.INSTANCE);
    }
    if (Intrinsics.areEqual(message, ResponseResult.PAIRING_DENIED.getResult())) {
        return new PairingSessionState.Error(PairingSessionError.IncorrectPin.INSTANCE);
    }
    if (Intrinsics.areEqual(message, ResponseResult.CHALLENGE_INCORRECT.getResult())) {
        return new PairingSessionState.Error(PairingSessionError.OnInvalidChallenge.INSTANCE);
    }
    if (Intrinsics.areEqual(message, ResponseResult.TOO_MANY_PAIRED_DEVICES.getResult())) {
        return new PairingSessionState.Error(PairingSessionError.TooManyPairedDevices.INSTANCE);
    }
    if (Intrinsics.areEqual(message, ResponseResult.BLOCKED.getResult())) {
        return new PairingSessionState.Error(PairingSessionError.PairingBlocked.INSTANCE);
    }
    return new PairingSessionState.Error(PairingSessionError.PairingFailed.INSTANCE);
}
```

Every branch maps to a UI dialog state — there is no automatic retry, no exponential backoff, no sleep. `MAX_CHALLENGES_EXCEEDED` and `BLOCKED` both surface to a separate "wait" dialog — the device is the cooldown authority, not the app.

The `Retry` class (`com/vizio/vnf/network/message/Retry.java`) supports exponential backoff (`multiplier`, `maxInterval`) but is **never instantiated with retries enabled** for pairing. Every grep hit for `new Retry(...)` shows `count=1` (single attempt) or `count=2` for volume — never for pairing endpoints, which always pass `MapsKt.emptyMap()` and don't even include the AUTH header (correctly — the pairing endpoints predate auth).

Re-pair behavior: there is no special handling. `PAIRING_PAIR = "pairing/pair"` is hit blindly; if a session exists already the device returns an error and the app surfaces it to the user.

**Implication:** pyvizio should treat all five error codes as terminal (no retry) and require human intervention. Ratelimit/cooldown is enforced server-side (TV blocks pin attempts after MAX_CHALLENGES_EXCEEDED).

### 6. Endpoint discovery (full URL catalog)

**Verdict:** **CONFIRMED — comprehensive; one new endpoint (`/state_extended`) and a soundbar wifi config flow that pyvizio may not have.**

**Evidence:**

Two authoritative sources ranked from concrete to abstract:

1. `com/vizio/connectivity/data/network/impl/DeviceApiRoutes.java` — the modern, hand-curated route list (24 entries):

```java
public static final String DEVICE_INFO        = "state/device/deviceinfo";
public static final String NETWORK_INFO_2020  = "menu_native/dynamic/tv_settings/admin_and_privacy/system_information/network_information";
public static final String NETWORK_INFO       = "/menu_native/dynamic/tv_settings/system/system_information/network_information";
public static final String STATE_EXTENDED     = "/state_extended";
public static final String PAIRING_START      = "pairing/start";
public static final String PAIRING_CANCEL     = "pairing/cancel";
public static final String PAIRING_PAIR       = "pairing/pair";
public static final String PAIRING_UNPAIR     = "pairing/unpair";
public static final String PAIRING_CHALLENGE_COMPLETE = "pairing/challenge_complete";
public static final String KEY_COMMAND        = "key_command/";
public static final String LAUNCH_APP         = "app/launch";
public static final String GET_SET_TV_POWER_MODE      = "menu_native/dynamic/tv_settings/system/power_mode";
public static final String GET_SET_AUDIO_POWER_MODE   = "menu_native/dynamic/audio_settings/system/eco_power";
public static final String GET_POWER_STATE            = "state/device/power_mode";
public static final String GET_CURRENT_INPUTS         = "menu_native/dynamic/tv_settings/devices/current_inputs";
public static final String SET_CURRENT_INPUTS         = "system/input/current_input";
public static final String GET_LEGACY_INPUTS          = "menu_native/dynamic/tv_settings/devices/name_input";
public static final String GET_CURRENT_INPUTS_AUDIO   = "menu_native/dynamic/audio_settings/input";
public static final String CURRENT_INPUT_AUDIO        = "menu_native/dynamic/audio_settings/input/current_input";
public static final String GET_CURRENT_APP            = "app/current";
public static final String GET_TOS_STATUS             = "menu_native/dynamic/tv_settings/cast/tos_accepted";
public static final String GET_SET_AUDIO_TOS_STATUS   = "menu_native/dynamic/audio_settings/cast/tos_accepted";
```

1. `com/vizio/vnf/swagger/apis/V2SCPApi.java` — the generated swagger client (4230 lines). All `RequestMethod, "<path>"` literals extracted, deduped, and grouped below in **Endpoints catalogued**.

2. `com/vizio/smartcast/menutree/models/enums/VZRestEndpoint.java` enumerates the full **177-entry** legacy menu-tree taxonomy with parent-child relationships and per-year mapping (`VZRestEndPoint2020` overrides). Notable entries pyvizio may not handle: `OOBE`, `pin/set_pin`, `pin/confirm_pin`, `dlm`/`uli` firmware update flows, `client/enable`/`client/disable`/`client/status`, network configuration flows (`set_wifi_password`, `start_ap_search`, `stop_ap_search`, `current_ssid_name`, `manual_setup`, `hidden_network_info`, `wireless_access_points`, `current_access_point`, `test_connection*`, `ip_address`/`subnet_mask`/`pref_dns_server`/`alt_dns_server`/`default_gateway`, `wireless_mac_address`).

**`/state_extended` (the unknown one):** `V2SCPApi.java:2992` defines `extendedState(String authToken)`. The capability is advertised by the device in `state/device/deviceinfo` under `SCPL_CAPABILITIES.state_extended` (`com/vizio/connectivity/data/network/models/ScplCapabilities.java`, line 38: `@SerialName("state_extended")`). When supported, it returns one combined snapshot of multiple state values — useful as a polling optimization.

**Soundbar WiFi configuration (issue #160):** `com/vizio/connectivity/ui/main_flow/viewmodel/SoftApPairingViewModel.java` exists for the soundbar SoftAp setup flow, and the `VZRestEndpoint` enum exposes the underlying endpoints: `/menu_native/.../network/wireless_access_points`, `/menu_native/.../network/set_wifi_password`, `/menu_native/.../network/hidden_network`, `/menu_native/.../network/start_ap_search`, `/menu_native/.../network/test_connection`, etc. The 2020 mappings (`VZRestEndPoint2020.NETWORK_WIFI_NETWORKS`, `NETWORK_WIFI_PASSWORD_ENTRY`) provide the modern paths. The flow is **GET wireless_access_points → PUT set_wifi_password → poll test_connection_results**.

**Implication:** pyvizio should add `/state_extended` (gated on `SCPL_CAPABILITIES.state_extended`), `/state/device/battery_level`, `/state/device/charging_status` (Crave-only), and document the soundbar wifi-config endpoints to address issue #160.

### 7. Device-type detection

**Verdict:** **CONFIRMED — discovery via mDNS (`_viziocast._tcp`), then device-type comes from `/state/device/deviceinfo` payload (`DEVICE_TYPE`); model-string sniffing for sub-types like Crave.**

**Evidence:**

`com/vizio/connectivity/data/discovery/VizioServiceDiscoveryClientKt.java`:

```java
private static final String SERVICE_TYPE = "_viziocast._tcp";
private static final long SERVICE_RESOLVE_DELAY_MS = 325;
```

The app uses Android NSD: `nsdManager.discoverServices("_viziocast._tcp", 1, discoveryListener)`. There is also a parallel `GoogleCastServiceDiscoveryClient` that uses Google Cast SDK (so the app can detect cast-only devices). No SSDP. Multicast lock tag `multicastLock`.

After resolution, the app calls `state/device/deviceinfo` (no AUTH required) and reads the response into `DeviceInfoResponse`. The `com.vizio.vdf.clientapi.entities.DeviceType` enum has four members: `VIZIO_TV` (id 0), `VIZIO_AUDIO`, `BLE`, `CAST`. Disambiguation between Crave variants is done with model-name string match (`com/vizio/vdf/clientapi/entities/device/DeviceUtilsKt.java`):

```java
if (device.getDeviceType() == DeviceType.VIZIO_AUDIO) {
    if (Intrinsics.areEqual(device.getModelName(), VZDeviceType.VZTypeName.NAME_CRAVE_GO)
            || Intrinsics.areEqual(device.getModelName(), "SP30-E0")) { ... }
    if (Intrinsics.areEqual(device.getModelName(), VZDeviceType.VZTypeName.NAME_CRAVE_360)
            || Intrinsics.areEqual(device.getModelName(), "SP50-D5")) { ... }
    if (Intrinsics.areEqual(device.getModelName(), VZDeviceType.VZTypeName.NAME_CRAVE_PRO)
            || Intrinsics.areEqual(device.getModelName(), "SP70-D5")) { ... }
}
```

So Crave model codes are: **SP30-E0 (Crave Go), SP50-D5 (Crave 360), SP70-D5 (Crave Pro)**.

**Implication:** pyvizio should keep `_viziocast._tcp` as the discovery service. Drop SSDP if still present (the official app uses zeroconf only). For typing, keep the `state/device/deviceinfo` round-trip and parse `DEVICE_TYPE`.

### 8. Other quirks

**Settings type filter (cast/input/devices/network):** the app does **not** filter by setting type at the application layer. All settings are surfaced via the menu-tree endpoints under `menu_native/dynamic/<root>/<segment>/...` and the device returns its native shape. pyvizio's filter for `cast`/`input`/`devices`/`network` is a synthetic abstraction. Verdict: **NOT-FOUND** — no equivalent in app.

**"No app running" handling:** `com/vizio/connectivity/data/network/responses/CurrentApplicationItemValue.java` defines fields `MESSAGE`, `NAME_SPACE` (note: with underscore), `APP_ID`, all nullable (`String?` / `Integer?`). The Kotlin response type allows the entire `VALUE` to be `null` and the parser accepts `MESSAGE` missing, `NAME_SPACE` missing, `APP_ID` missing in any combination. Verdict: **CONFIRMED** — pyvizio's "VALUE may be null OR fields may be missing" handling is correct.

**Synthetic `current_input`:** the official app does **not** filter `current_input` from inputs. `SCPLCommandsManager.sortedAvailableInputs()` (line 647–671):

```java
private final List<DeviceInput> sortedAvailableInputs(List<DeviceInput> latestInputs) {
    ArrayList arrayList = new ArrayList();
    for (Object obj : latestInputs) {
        DeviceInput deviceInput = (DeviceInput) obj;
        if (deviceInput.getEnabled() || deviceInput.getVisible()) {
            arrayList.add(obj);
        }
    }
    // ...rotate so CAST is first...
    if (i > 0) Collections.rotate(arrayList2, arrayList2.size() - i);
    return arrayList2;
}
```

The filter is `enabled OR visible` (not name-based), and CAST is rotated to the top of the list. Verdict: **CONFIRMED** — pyvizio's name-based `current_input` filter is unique to pyvizio. The app handles current-input separately via `devices/current_input` and never confuses it with the inputs list.

**Retry/backoff on transient errors:** the `Retry` class (`com/vizio/vnf/network/message/Retry.java`) supports exponential backoff but is essentially never enabled. Default constructor is `Retry(count, interval=500ms, multiplier=1.0, maxInterval=Long.MAX_VALUE, timeout=10000ms)`, and **all** call sites pass `count=1, interval=0L, multiplier=0.0` (the unused-fields default for `30` mask flag) — a single attempt, no retry. Verdict: **NOT-FOUND** — official app does not retry on transient errors; it surfaces failure to the UI.

**Discovery — zeroconf vs SSDP:** **zeroconf only**, service type `_viziocast._tcp`. SSDP is not used.

**Default per-request timeouts:** read 10s, write 3s, connect 2s (`EmbeddedConnectionConfig`). pyvizio's defaults should align with these.

### 9. Anything surprising

- **`/voice/command`** — `V2SCPApi.java` line ~3xxx defines a `voice/command` PUT endpoint plus a websocket audio stream pipeline (`com/vizio/smartcast/voice/VoiceSearch.java`, chunks audio in 1024-byte units, header `VIZIO-Voice-Source: MOBILE`). pyvizio does not need to add this but it exists as a documented surface.
- **`/scpl/log`** — `DeviceCommandBuilder.java:107` calls `RequestMethod.GET, "/scpl/log"` — a debug/log retrieval endpoint not in the modern routes file. May be useful for diagnostics. Returns `byte[]`.
- **WebSocket SCPL** — `V2SCPWebsocketApi` (referenced in `DeviceCommandBuilder.java:360`) supports event subscription via `eventRegister` with body `{"REQUEST":"MODIFY"}`. The TV exposes a websocket interface in addition to REST. This could replace polling for state.
- **Salesforce, Adobe, AppsFlyer, Braze, Firebase, Inmobi** — all bundled. The app reports usage extensively. Manifest also declares `RECORD_AUDIO`, `READ_CONTACTS`, `ACCESS_FINE_LOCATION`. None of this affects the SmartCast protocol but is worth knowing for privacy discussions.
- **`/system/consent/adp`** — fetches the device's "Activity Data Privacy" consent state. Could be relevant for documenting privacy posture.
- **2020 path migration is real and divergent.** `VZRestEndpoint` carries a parallel `VZRestEndPoint2020` mapping for almost every legacy endpoint. The app picks based on `URIYearOptions.from(modelYear)` — pre-2020 vs YEAR_2020 paths differ for `system_information`, `power_indicator`, `factory_reset`, `country`, `time` etc. pyvizio likely already handles this via the `is2018+` flag, but the full mapping is in `VZRestEndpoint.java` lines 96–177 if any endpoint mismatch is suspected.
- **`SoftApPairingViewModel`** — soundbar onboarding does in-app SoftAp connection (joining the soundbar's hotspot, calling its API while disconnected from internet, then handing off WiFi creds). pyvizio could re-implement this for headless soundbar provisioning.
- **Pairing cancellation with `pairing/cancel`** is mandatory before re-pairing — `DevicePairingViewModel.updateStateForCancellation()` always sends it. pyvizio should ensure it does the same after a failed `pair` step.
- **`Authorization` header is half-implemented** — `EmbeddedConnectionConfig.java:34` constructs `Authorization`, but the side-effect is discarded (no `=` on the `MapsKt.plus` result). Looks like dead code from an earlier design; the live header is `AUTH`.

### 10. Volume API V2 — how the app picks a volume strategy

Investigated for [home-assistant/core#179254](https://github.com/home-assistant/core/issues/179254).
This is the answer to "how does one binary drive many firmware
generations": the app resolves the device's **API spec version** and
branches. There is no server-side negotiation and no capability flag
involved, which is why the logic is portable to any client.

`com/vizio/vnf/network/message/device/DeviceInfoAnalyzer.java`:

```java
public final boolean isVolumeAPIV2Supported() {
    ApiMinimumSpecVersion minimumApiSpecVersion;
    return isTvDevice()
        && (minimumApiSpecVersion = getMinimumApiSpecVersion()) != null
        && minimumApiSpecVersion.getId()
             >= ApiMinimumSpecVersion.VER_2_0_0_2031_0014_FUR_SUPPORTED.getId();
}
```

The ladder (`ApiMinimumSpecVersion`, id → apiName):

```
-1 VER_UNKNOWN                        ""
 0 VER_1_0_0_0                        "1.0.0.0"
 1 VER_1_0_12_11                      "1.0.12.11"
 2 VER_1_0_13_25                      "1.0.13.25"
 3 VER_2_0_0_0000_000                 "2.0.0-2000.0000"
 4 VER_2_0_0_2031_0014_FUR_SUPPORTED  "2.0.0-2031.0014"   ← the threshold
```

`RestApiUtil` resolves a raw string with **two different comparison
algorithms**, chosen by regex (`V2_PATTERN_REGEX = \d.\d.\d-\d{4}.\d{4}`,
note the unescaped dots):

- `computeSpecVersionV2` — walks the V2 rungs descending, using
  `compareVersion`, which strips `\D+` from both strings and compares
  **character by character** over `max(len)` positions with missing
  characters treated as NUL.
- `computeSpecVersion` (legacy) — strips `[^0-9.]`, splits on `.`, walks
  the V1 rungs descending comparing `min(len)` components **numerically**;
  a target with fewer components than the candidate fails it.

Recomputed whenever the merged `apiVersion` field changes, in
`InternalDevice$apiVersionMerger$1.allowMerge`.

**The branch it feeds** — `SetVolumeCommand.retryStrategy()` and
`MuteToggleCommand.retryStrategy()`. Both build an ordered fallback list,
and in both the **key command comes first on every device**; the V1/V2
choice only affects the second rung, with Cast last:

```java
arrayList.add(to(Protocol.HTTP, volumeKeyCommand(str)));            // always first
if (settingsRoot == SETTINGS_ROOT_TV) {
    arrayList.add(this.volumeV2Supported
        ? to(Protocol.HTTP, setVolumeCommandV2(str))                 // increase/decrease?STEP=
        : to(Protocol.HTTP, new SetVolumeHTTPCommand(this, str, settingsRoot)));
}
arrayList.add(to(Protocol.CAST, new SetVolumeCastCommand()));
```

`setVolumeCommandV2` picks `increaseVolumeCommandV2` or
`decreaseVolumeCommandV2` by sign. The V1 `SetVolumeHTTPCommand` is a
GET-then-PUT that reads `VolumeItemDetail.VALUE` + `HASHVAL` from the
menu_native leaf and writes `min(max(0, value + delta), 100)` — an
independent confirmation that **100 is the TV volume ceiling**.

Request bodies: `VolumeLevelBody` is a lone `Integer LEVEL`;
`VolumeMuteBody` is a lone `Boolean MUTE`.

**Reads are unaffected by the version gate.**
`GetCurrentVolumeCommandStrategy` uses `volumeStatusCommand` —
`GET /menu_native/dynamic/{root}/audio/volume`, the **leaf** — on both
V1 and V2. It never reads the `audio` collection. That is precisely why
`vizaio.get_volume()` works on the firmware in #179254 while pyvizio's
`audio_settings["volume"]` raises `KeyError`.

**Caveat on the flat reads — since resolved on hardware.** `V2SCPApi`
also generates `GET /audio/volume/level` (`volumeLevelStatus`) and
`GET /audio/volume/mute` (`volumeMuteStatus`), but nothing in
`com/vizio/**` calls them, and the generated model
`VolumeLevelStatusResponse_ITEM` has only `Object`-typed `type` / `ref`
fields with no value member — so the decompile could not tell us the wire
shape. A live probe (VHD24M-0810, 2026-08-16) settled it: both use a
singular `ITEM`, carry no `HASHVAL`, and `level` returns **both** values
at once — `{"ITEM":{"TYPE":"T_JSON_OBJECT_V1","VALUE":{"LEVEL":9,"MUTE":false}}}`
and `{"ITEM":{"TYPE":"T_BOOLEAN_V1","VALUE":false}}`. Captured as
`tests/captured/audio_volume_{level,mute}.json`. See protocol-notes #31.

**The APK's `?STEP={STEP}` query form is wrong on real firmware.** The
generated config is
`RequestMethod.PUT, "/audio/volume/increase?STEP={STEP}"`, but live that
returns `SUCCESS` and moves the volume by exactly 1 for any value.
`{"STEP": n}` in the **body** applies the requested delta. Verified both
directions at several values; empty body and no body also mean 1. Either
the generated client was always wrong here or the firmware changed —
worth remembering that a generated Swagger client is a description of a
spec, not proof of device behavior.

**`AUDIO_2.0_API` is not this flag.** The string appears nowhere in the
5.0.0 or 5.3.0 dex; `DeviceInfoCapabilities` models 22 keys and that is
not among them, so the app never reads it. It is real on the wire
(`tests/captured/device_info.json` has it `true`) but does not predict
the missing-`volume` shape — that same TV lists `volume` in its `audio`
collection. See protocol-notes #30.

### 11. App 5.3.0 retired the flat volume family client-side

Four months after 5.0.0, the entire HTTP volume surface is gone from the
client and volume is driven purely by key commands. Established from raw
dex (names are obfuscated in 5.3.0, string literals are not):

- **No `/audio/volume/*` path literal anywhere** in the 5.3.0 dex, while
  `/key_command/`, `/pairing/start`, `/app/launch` and
  `/state/device/deviceinfo` all survive verbatim — so this is real
  absence, not string encryption. `/menu_native/dynamic/{deviceEndpoint}/audio/volume`
  and `/menu_native/static/{dynamicSettings}/audio/volume` are gone too.
- **The command layer was deleted, not renamed.**
  `vdf/services/control/command/volume/{SetVolumeCommand,MuteToggleCommand,GetCurrentVolumeCommandStrategy}`
  and `VolumeCapabilityKt` are absent; the surviving
  `vdf/services/control/` classes gained only obfuscated `pinpair/a…x`.
- **The replacement is** `com/vizio/connectivity/domain/volume_remote/`
  (kept package name, classes obfuscated to `a`/`b`/`c` plus
  `WiFiDeviceHardwareVolumeHandler`). That handler dispatches
  `KeyCommandItem.AUDIO_VOLUME_PLUS` / `AUDIO_VOLUME_MINUS`, and
  `TVRemoteViewModel.toggleMute` dispatches a mute key.
- **The new device-API interface has no volume method.** `uh.a` (~27
  methods: inputs, power, pairing, apps, system info, extended state)
  exposes only a generic
  `c(KeyCommandItem, String, …)` for this.
- `VolumeMuteBody` and `V2SCPApi$muteVolume$1$decode$…` linger in
  `classes8.dex` as **orphaned dead generated code** — the outer method
  and its path constant were tree-shaken.

Note the 5.3.0 `V2SCPApi` also shrank from 59 endpoint paths to 31 with
0 added. Most of that is ordinary tree-shaking as call sites moved to
the newer `connectivity/domain` layer (`ScplRequestManager`), which
coexists with the legacy Swagger client — `/state/device/deviceinfo`, for
instance, drops out of `V2SCPApi` while remaining in the dex. Do not read
the shrunken path list as a protocol deprecation list; the volume finding
above is separate and rests on whole-dex absence.

**Implication for a library:** keypresses are the path Vizio still
exercises against new firmware, so they belong as the default. Flat
endpoints that the vendor's own client abandoned should be opt-in — no
one is regression-testing them against new firmware releases. The
exception worth making is an endpoint we have **hardware-verified
ourselves**, e.g. `PUT /audio/volume/level`, where direct evidence beats
the app's changing preferences.

## Endpoints catalogued

Full list extracted from `V2SCPApi.java` (~55 unique paths after dedup; `{deviceEndpoint}` is one of `tv_settings`/`audio_settings`):

**State (no AUTH header):** `GET /state/device/deviceinfo`, `GET /state/device/power_mode`
**State (with AUTH):** `GET /state_extended`, `GET /state/device/battery_level`, `GET /state/device/charging_status`, `GET /system/versions`, `GET /system/country`, `GET /system/consent/adp`, `GET /config/activity_data_accepted`
**Pairing (no AUTH):** `PUT /pairing/start`, `PUT /pairing/pair`, `PUT /pairing/cancel`, `PUT /pairing/unpair`, `PUT /pairing/challenge_complete`
**Control:** `PUT /key_command/` (KEYLIST), `PUT /app/launch`, `GET /app/current`, `PUT /app/config/get_property`, `PUT /app/store/get`, `PUT /app/store/set`, `PUT /client/enable`, `PUT /client/disable`, `PUT /client/status`
**Menu — dynamic (GET + PUT pairs):** `menu_native/dynamic/{deviceEndpoint}/{settingEndpoint}` (catch-all), plus typed paths under `audio/{volume,mute}`, `cast/{device_name,serial_device_id}`, `channels/current_channel`, `devices/{current_input,current_inputs,name_input}`, `system/{cast_name,country,menu_language,power_mode,eco_power,local_time_settings/country}`, `system/system_information/{network_information,speaker_information/audio_type}`, `admin_and_privacy/system_information/{network_information,*}` (2020+), `picture/picture_mode`, `audio_settings/input{,/current_input}`, `audio_settings/audio/{settingEndpoint}`
**Menu — static:** `GET /menu_native/static/{dynamicSettings}/audio/volume`
**Flat-form (legacy):** `PUT/GET /audio/volume/{level,mute,increase,decrease}`, `PUT /system/input/current_input`
**OOBE / firmware:** `GET /pin/is_pin_default`, `PUT /pin/set_pin`, `PUT /pin/confirm_pin`, plus the `dlm/*` and `uli/*` enum entries (firmware update flows)
**Voice:** `PUT /voice/command` (plus websocket audio stream)
**Diagnostics:** `GET /scpl/log`

## Recommendations

1. **HASHVAL doc:** finalize as opaque server-side optimistic-concurrency token. Provide reference to `HASHVAL_ERROR` response to explain stale-write failures.
2. **Endpoints:** add `/state_extended` (capability-gated on `SCPL_CAPABILITIES.state_extended`) for bulk polling. Document `/state/device/battery_level` and `/state/device/charging_status` for Crave devices.
3. **Soundbar Wi-Fi config (issue #160):** the endpoints exist (`network/wireless_access_points`, `network/set_wifi_password`, `network/hidden_network`, `network/start_ap_search`, `network/stop_ap_search`, `network/test_connection*`). The flow is GET → PUT → poll. Implementation is non-trivial because it requires the client to be connected to the soundbar's SoftAp network during setup, but the API surface is fully documented in `VZRestEndpoint.java`.
4. **Discovery:** keep `_viziocast._tcp` zeroconf. Drop SSDP if it's still present (official app does not use it).
5. **Default timeouts:** align with official app — read 10s, write 3s, connect 2s.
6. **Concurrency:** keep pyvizio's semaphore. The official app does not throttle but it benefits from being on the same LAN with sub-100ms RTTs and short-lived bursts; integrations that poll continuously need throttling.
7. **Retry policy:** match official app — retry **count=1** by default (i.e., do not retry transient errors by default), let callers opt in. Pairing endpoints should never retry.
8. **Auth header:** confirm pyvizio uses `AUTH` (not `Authorization`) and does not URL-encode/Bearer-wrap. Send `VIZIO-SmartCast-Source: SMARTCAST_ANDROID` to look like the official app if mimicking is desired (or `pyvizio` for honesty).
9. **Key commands:** ACTION value is exclusively `KEYPRESS`. KEYLIST has no client-side cap — pyvizio can stop chunking aggressively but a defensive cap of ~50 keeps long inputs safe.
10. **Re-pair:** always `pairing/cancel` before re-trying `pairing/start`. After `MAX_CHALLENGES_EXCEEDED` or `BLOCKED`, there is no programmatic recovery — surface to user.
11. **Inputs:** the official app shows `enabled OR visible` inputs (not name-based filtering). The `current_input` synthetic name is **not filtered** in the official app — pyvizio's filtering may hide an input some users want to see. Consider exposing the raw list with an `include_synthetic=True` toggle.
12. **Crave model strings:** SP30-E0 (Go), SP50-D5 (360), SP70-D5 (Pro). Useful for sub-typing audio devices.

End of findings.
