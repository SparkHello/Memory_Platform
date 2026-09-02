# Memory Platform for Android

Runs both gateways inside a foreground service on the phone, bound to
`127.0.0.1`. The app itself is a status page; the Web console at
`http://127.0.0.1:2026/` is the real UI, and chat apps on the same phone use
`http://127.0.0.1:2026/v1`.

Read [docs/android.md](../../docs/android.md) before building. Short version:

1. `scripts/android/termux-verify.sh` on the phone first (no Android Studio).
2. `scripts/android/build-wheels.sh` and `scripts/android/build-rust-wheels.sh`
   to populate `wheels/`.
3. `npm run build` in `services/memory-gateway/ui`, then open this directory
   in Android Studio and build.
