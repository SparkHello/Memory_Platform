package app.memoryplatform.android

import android.content.Context
import org.json.JSONArray
import java.net.HttpURLConnection
import java.net.URL

/**
 * Looks up the newest Android release on GitHub and compares it with the
 * installed versionName. Releases are tagged `android-v<semver>[-preview.N]`;
 * only the numeric triple is compared, so a `-preview` rebuild of the installed
 * version is not reported as an update. Network failures are swallowed: the
 * check is a convenience, never a gate.
 */
object UpdateChecker {
    const val REPO = "SparkHello/Memory_Platform"
    const val RELEASES_PAGE = "https://github.com/$REPO/releases"
    private const val API = "https://api.github.com/repos/$REPO/releases?per_page=20"
    private const val TAG_PREFIX = "android-v"
    private const val PREF_LAST_CHECK = "update.lastCheck"
    private const val PREF_IGNORED = "update.ignoredVersion"
    private const val PREF_CACHED = "update.cached"
    /** Automatic checks at most every 6 hours; GitHub allows 60 anonymous calls/hour. */
    private const val AUTO_INTERVAL_MS = 6L * 60 * 60 * 1000

    data class Release(
        val version: String,      // "0.5.2"
        val tag: String,          // "android-v0.5.2-preview.1"
        val name: String,
        val pageUrl: String,
        val apkUrl: String?,      // direct APK asset when the release has one
        val publishedAt: String,
    ) {
        fun toJson(): String = org.json.JSONObject()
            .put("version", version).put("tag", tag).put("name", name)
            .put("pageUrl", pageUrl).put("apkUrl", apkUrl ?: "").put("publishedAt", publishedAt)
            .toString()

        companion object {
            fun fromJson(text: String): Release? = try {
                val json = org.json.JSONObject(text)
                Release(
                    version = json.getString("version"),
                    tag = json.getString("tag"),
                    name = json.optString("name"),
                    pageUrl = json.getString("pageUrl"),
                    apkUrl = json.optString("apkUrl").takeIf { it.isNotEmpty() },
                    publishedAt = json.optString("publishedAt"),
                )
            } catch (e: Exception) {
                null
            }
        }
    }

    sealed class Result {
        data class UpdateAvailable(val release: Release) : Result()
        data class UpToDate(val latest: Release?) : Result()
        data class Failed(val reason: String) : Result()
    }

    fun installedVersion(context: Context): String =
        context.packageManager.getPackageInfo(context.packageName, 0).versionName ?: "0"

    fun shouldAutoCheck(context: Context): Boolean {
        val last = prefs(context).getLong(PREF_LAST_CHECK, 0L)
        return System.currentTimeMillis() - last > AUTO_INTERVAL_MS
    }

    /** Last successful lookup, so the card survives app restarts without another request. */
    fun cachedUpdate(context: Context): Release? {
        val cached = prefs(context).getString(PREF_CACHED, null)?.let(Release::fromJson) ?: return null
        if (!isNewer(cached.version, installedVersion(context))) return null
        if (cached.version == prefs(context).getString(PREF_IGNORED, null)) return null
        return cached
    }

    fun ignore(context: Context, release: Release) {
        prefs(context).edit().putString(PREF_IGNORED, release.version).apply()
    }

    /** Blocking; call from a worker thread. `manual` bypasses the ignored-version filter. */
    fun check(context: Context, manual: Boolean): Result {
        val installed = installedVersion(context)
        val releases = try {
            fetchReleases()
        } catch (e: Exception) {
            return Result.Failed(e.message ?: e.toString())
        }
        prefs(context).edit().putLong(PREF_LAST_CHECK, System.currentTimeMillis()).apply()
        val latest = releases.maxWithOrNull(compareBy<Release> { versionParts(it.version).let { p -> p[0] * 1_000_000 + p[1] * 1_000 + p[2] } }
            .thenBy { it.publishedAt })
        if (latest == null || !isNewer(latest.version, installed)) {
            prefs(context).edit().remove(PREF_CACHED).apply()
            return Result.UpToDate(latest)
        }
        prefs(context).edit().putString(PREF_CACHED, latest.toJson()).apply()
        if (!manual && latest.version == prefs(context).getString(PREF_IGNORED, null)) return Result.UpToDate(latest)
        return Result.UpdateAvailable(latest)
    }

    private fun fetchReleases(): List<Release> {
        val connection = URL(API).openConnection() as HttpURLConnection
        connection.connectTimeout = 8_000
        connection.readTimeout = 8_000
        connection.setRequestProperty("Accept", "application/vnd.github+json")
        connection.setRequestProperty("User-Agent", "memory-platform-android")
        try {
            if (connection.responseCode != 200) throw IllegalStateException("GitHub HTTP ${connection.responseCode}")
            val body = connection.inputStream.bufferedReader().readText()
            val array = JSONArray(body)
            val result = mutableListOf<Release>()
            for (index in 0 until array.length()) {
                val item = array.getJSONObject(index)
                if (item.optBoolean("draft")) continue
                val tag = item.optString("tag_name")
                if (!tag.startsWith(TAG_PREFIX)) continue
                val version = tag.removePrefix(TAG_PREFIX).substringBefore("-")
                if (versionParts(version).all { it == 0 }) continue
                var apk: String? = null
                val assets = item.optJSONArray("assets")
                for (a in 0 until (assets?.length() ?: 0)) {
                    val asset = assets!!.getJSONObject(a)
                    if (asset.optString("name").endsWith(".apk")) { apk = asset.optString("browser_download_url"); break }
                }
                result += Release(
                    version = version,
                    tag = tag,
                    name = item.optString("name").ifEmpty { tag },
                    pageUrl = item.optString("html_url").ifEmpty { RELEASES_PAGE },
                    apkUrl = apk,
                    publishedAt = item.optString("published_at"),
                )
            }
            return result
        } finally {
            connection.disconnect()
        }
    }

    fun isNewer(candidate: String, installed: String): Boolean {
        val a = versionParts(candidate)
        val b = versionParts(installed)
        for (index in 0 until 3) {
            if (a[index] != b[index]) return a[index] > b[index]
        }
        return false
    }

    private fun versionParts(version: String): IntArray {
        val parts = IntArray(3)
        version.substringBefore("-").split(".").take(3).forEachIndexed { index, part ->
            parts[index] = part.takeWhile { it.isDigit() }.toIntOrNull() ?: 0
        }
        return parts
    }

    private fun prefs(context: Context) = context.getSharedPreferences(MainActivity.PREFS, Context.MODE_PRIVATE)
}
