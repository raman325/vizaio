Bundled SmartCast app data lives here.

- `apps.json` — catalog metadata (name, id, country, mobileAppInfo).
  Source: <https://scfs.vizio.com/appservice/vizio_apps_prod.json>.
  Kept in version control as the offline fallback for the remote
  catalog. The previous host
  (`hometest.buddytv.netdna-cdn.com`) is dead — discovered via APK
  decompilation that the SmartCast Android app fetches from
  `scfs.vizio.com`. The new endpoint returns metadata only; per-app
  launch payloads (`NAME_SPACE`/`APP_ID`/`MESSAGE`) live in the
  availability data below.
- `app_availability.json` — per-chipset/firmware launch payloads and
  gating data. Source:
  <https://scfs.vizio.com/appservice/app_availability_prod.json>.
  Bundled as an offline fallback only; the library prefers a fresh
  fetch at runtime because availability rolls forward with firmware
  updates.
