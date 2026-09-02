package app.memoryplatform.android

import android.Manifest
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PersistableBundle
import android.os.PowerManager
import android.provider.Settings
import android.widget.Button
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import com.chaquo.python.Python
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import kotlin.concurrent.thread

/** Status page only: the real UI is the Web console served on 127.0.0.1. */
class MainActivity : AppCompatActivity() {

    private lateinit var state: TextView
    private lateinit var detail: TextView
    private lateinit var toggle: Button
    private val handler = Handler(Looper.getMainLooper())
    private var lastHealth = "未检测"

    private val poll = object : Runnable {
        override fun run() {
            thread { lastHealth = probe(); handler.post { render(StackState.status.value) } }
            handler.postDelayed(this, 5_000)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        state = findViewById(R.id.state)
        detail = findViewById(R.id.detail)
        toggle = findViewById(R.id.toggle)

        toggle.setOnClickListener {
            val running = StackState.state() == "running" || StackState.state() == "starting"
            prefs().edit().putBoolean(PREF_AUTOSTART, !running).apply()
            if (running) GatewayService.stop(this) else startService()
        }
        findViewById<Button>(R.id.openConsole).setOnClickListener {
            startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(StackState.CONSOLE_URL)))
        }
        val baseUrl = "http://127.0.0.1:${StackState.MEMORY_PORT}"
        findViewById<TextView>(R.id.consoleUrl).text = "$baseUrl/"
        findViewById<TextView>(R.id.openaiUrl).text = "$baseUrl/v1"
        findViewById<TextView>(R.id.mcpUrl).text = "$baseUrl/mcp"
        findViewById<Button>(R.id.copyConsoleUrl).setOnClickListener { copyPlain("$baseUrl/") }
        findViewById<Button>(R.id.copyOpenaiUrl).setOnClickListener { copyPlain("$baseUrl/v1") }
        findViewById<Button>(R.id.copyMcpUrl).setOnClickListener { copyPlain("$baseUrl/mcp") }
        findViewById<Button>(R.id.copyToken).setOnClickListener {
            copyCredential("gateway.txt", "memory-platform-console-token", R.string.token_copied)
        }
        findViewById<Button>(R.id.copyAdminKey).setOnClickListener {
            copyCredential("admin.txt", "memory-platform-admin-key", R.string.admin_key_copied)
        }
        findViewById<Button>(R.id.battery).setOnClickListener { requestIgnoreBatteryOptimizations() }
        findViewById<Button>(R.id.exportDiagnostics).setOnClickListener { exportDiagnostics() }

        StackState.status.observe(this) { render(it) }
        if (prefs().getBoolean(PREF_AUTOSTART, false) && StackState.state() == "stopped") startService()
    }

    override fun onResume() {
        super.onResume()
        handler.post(poll)
    }

    override fun onPause() {
        handler.removeCallbacks(poll)
        super.onPause()
    }

    private fun startService() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            ActivityCompat.requestPermissions(this, arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }
        GatewayService.start(this)
    }

    private fun render(status: JSONObject?) {
        val current = status?.optString("state", "stopped") ?: "stopped"
        state.text = getString(
            when (current) {
                "running" -> R.string.state_running
                "starting" -> R.string.state_starting
                "failed" -> R.string.state_failed
                else -> R.string.state_stopped
            },
        )
        toggle.text = getString(if (current == "running" || current == "starting") R.string.action_stop else R.string.action_start)
        // Python/SQLite versions are in the diagnostics bundle; the status page stays plain.
        val lines = mutableListOf("健康检查：$lastHealth")
        status?.optString("error", "")?.takeIf { it.isNotEmpty() && it != "null" }?.let { lines += "错误：$it" }
        detail.text = lines.joinToString("\n")
    }

    private fun probe(): String {
        return try {
            val connection = URL("http://127.0.0.1:${StackState.MEMORY_PORT}/health").openConnection() as HttpURLConnection
            connection.connectTimeout = 1_500
            connection.readTimeout = 1_500
            val code = connection.responseCode
            connection.disconnect()
            if (code == 200) "OK" else "HTTP $code"
        } catch (e: Exception) {
            "不可达"
        }
    }

    private fun copyPlain(value: String) {
        (getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager)
            .setPrimaryClip(ClipData.newPlainText("memory-platform-url", value))
        Toast.makeText(this, R.string.copied, Toast.LENGTH_SHORT).show()
    }

    /** Credentials live as 0600 files under filesDir; the clipboard is the only hand-off to the user. */
    private fun copyCredential(fileName: String, label: String, copiedMessage: Int) {
        val file = File(filesDir, "memory-platform/credentials/$fileName")
        if (!file.isFile) {
            Toast.makeText(this, R.string.token_missing, Toast.LENGTH_SHORT).show()
            return
        }
        val clip = ClipData.newPlainText(label, file.readText().trim())
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            clip.description.extras = PersistableBundle().apply { putBoolean(ClipDescription_EXTRA_IS_SENSITIVE, true) }
        }
        (getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager).setPrimaryClip(clip)
        Toast.makeText(this, copiedMessage, Toast.LENGTH_LONG).show()
    }

    /** Zip logs, redacted config, a memory.db snapshot and decision reports, then hand it to the share sheet. */
    private fun exportDiagnostics() {
        val button = findViewById<Button>(R.id.exportDiagnostics)
        button.isEnabled = false
        Toast.makeText(this, R.string.export_running, Toast.LENGTH_SHORT).show()
        thread(name = "diagnostics-export") {
            val result = try {
                writeLogcat()
                val dataDir = File(filesDir, "memory-platform")
                val json = Python.getInstance().getModule("embedded_stack")
                    .callAttr("export_diagnostics", dataDir.absolutePath).toString()
                Result.success(File(JSONObject(json).getString("path")))
            } catch (e: Exception) {
                Result.failure(e)
            }
            handler.post {
                button.isEnabled = true
                result.onSuccess { shareFile(it) }
                    .onFailure { Toast.makeText(this, getString(R.string.export_failed, it.message), Toast.LENGTH_LONG).show() }
            }
        }
    }

    /** Only this process's own log lines are readable without READ_LOGS; that is exactly what we want. */
    private fun writeLogcat() {
        val logs = File(filesDir, "memory-platform/logs").apply { mkdirs() }
        try {
            val process = ProcessBuilder("logcat", "-d", "-v", "time", "--pid=${android.os.Process.myPid()}")
                .redirectErrorStream(true).start()
            File(logs, "logcat.txt").outputStream().use { process.inputStream.copyTo(it) }
            process.waitFor()
        } catch (e: Exception) {
            File(logs, "logcat.txt").writeText("logcat unavailable: $e\n")
        }
    }

    private fun shareFile(file: File) {
        val uri = FileProvider.getUriForFile(this, "$packageName.files", file)
        val intent = Intent(Intent.ACTION_SEND)
            .setType("application/zip")
            .putExtra(Intent.EXTRA_STREAM, uri)
            .putExtra(Intent.EXTRA_SUBJECT, file.name)
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        startActivity(Intent.createChooser(intent, getString(R.string.action_export)))
    }

    private fun requestIgnoreBatteryOptimizations() {
        val power = getSystemService(Context.POWER_SERVICE) as PowerManager
        if (power.isIgnoringBatteryOptimizations(packageName)) {
            Toast.makeText(this, "已经关闭电池优化", Toast.LENGTH_SHORT).show()
            return
        }
        startActivity(
            Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")),
        )
    }

    private fun prefs() = getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    companion object {
        const val PREFS = "memory-platform"
        const val PREF_AUTOSTART = "autostart"
        private const val ClipDescription_EXTRA_IS_SENSITIVE = "android.content.extra.IS_SENSITIVE"
    }
}
