package app.memoryplatform.android

import android.content.Context
import java.io.File

/**
 * The Web console is shipped in assets/ui and copied to filesDir/ui once per
 * app version, because FastAPI's StaticFiles needs real files on disk.
 */
object ConsoleAssets {
    fun ensureExtracted(context: Context): File {
        val target = File(context.filesDir, "ui")
        val stamp = File(target, ".version")
        // versionCode alone is not enough: every reinstall (even with the same
        // versionCode, e.g. debug builds) must refresh the console, otherwise the
        // phone keeps serving the first build's HTML/CSS/JS forever.
        val info = context.packageManager.getPackageInfo(context.packageName, 0)
        val version = "${info.longVersionCode}:${info.lastUpdateTime}"
        if (stamp.isFile && stamp.readText() == version && File(target, "index.html").isFile) {
            return target
        }
        target.deleteRecursively()
        copyAssetDir(context, "ui", target)
        stamp.writeText(version)
        return target
    }

    private fun copyAssetDir(context: Context, assetPath: String, target: File) {
        val assets = context.assets
        val children = assets.list(assetPath) ?: emptyArray()
        if (children.isEmpty()) {
            target.parentFile?.mkdirs()
            assets.open(assetPath).use { input -> target.outputStream().use { input.copyTo(it) } }
            return
        }
        target.mkdirs()
        for (child in children) copyAssetDir(context, "$assetPath/$child", File(target, child))
    }
}
