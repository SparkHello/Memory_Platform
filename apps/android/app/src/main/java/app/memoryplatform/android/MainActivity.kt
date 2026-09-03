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
import android.view.View
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

/**
 * Status page with a four-step first-run checklist. The real UI is the Web
 * console served on 127.0.0.1; "打开控制台" opens it through a one-time login
 * link so the user never has to paste the login key or the admin key.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var state: TextView
    private lateinit var detail: TextView
    private lateinit var stepService: TextView
    private lateinit var stepModel: TextView
    private lateinit var stepChatKey: TextView
    private lateinit var stepBattery: TextView
    private lateinit var checklistTitle: TextView
    private lateinit var primaryAction: Button
    private lateinit var primaryHint: TextView
    private lateinit var stopAction: Button
    private lateinit var batteryCard: View
    private val handler = Handler(Looper.getMainLooper())

    /** Result of the last background probe against the running stack. */
    private data class Readiness(
        val health: String,
        val healthOk: Boolean,
        val modelReady: Boolean?,   // null = unknown (service down or probe failed)
        val chatKeyExists: Boolean?, // null = unknown
    )

    private var readiness = Readiness(health = "", healthOk = false, modelReady = null, chatKeyExists = null)
    private var openingConsole = false

    private val poll = object : Runnable {
        override fun run() {
            thread {
                val next = probe()
                handler.post {
                    readiness = next
                    render(StackState.status.value)
                }
            }
            handler.postDelayed(this, 5_000)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        state = findViewById(R.id.state)
        detail = findViewById(R.id.detail)
        stepService = findViewById(R.id.stepService)
        stepModel = findViewById(R.id.stepModel)
        stepChatKey = findViewById(R.id.stepChatKey)
        stepBattery = findViewById(R.id.stepBattery)
        checklistTitle = findViewById(R.id.checklistTitle)
        primaryAction = findViewById(R.id.primaryAction)
        primaryHint = findViewById(R.id.primaryHint)
        stopAction = findViewById(R.id.stopAction)
        batteryCard = findViewById(R.id.batteryCard)
        readiness = readiness.copy(health = getString(R.string.health_unknown))

        primaryAction.setOnClickListener {
            if (StackState.state() == "running") openConsole() else startService()
        }
        stopAction.setOnClickListener {
            prefs().edit().putBoolean(PREF_AUTOSTART, false).apply()
            GatewayService.stop(this)
        }

        val baseUrl = "http://127.0.0.1:${StackState.MEMORY_PORT}"
        findViewById<TextView>(R.id.consoleUrl).text = "$baseUrl/"
        findViewById<TextView>(R.id.openaiUrl).text = "$baseUrl/v1"
        findViewById<TextView>(R.id.mcpUrl).text = "$baseUrl/mcp"
        findViewById<Button>(R.id.copyConsoleUrl).setOnClickListener { copyPlain("$baseUrl/") }
        findViewById<Button>(R.id.copyOpenaiUrl).setOnClickListener { copyPlain("$baseUrl/v1") }
        findViewById<Button>(R.id.copyMcpUrl).setOnClickListener { copyPlain("$baseUrl/mcp") }
        findViewById<Button>(R.id.copyModelName).setOnClickListener { copyPlain(getString(R.string.model_name_value)) }
        findViewById<Button>(R.id.copyToken).setOnClickListener {
            copyCredential("gateway.txt", "memory-platform-login-key", R.string.token_copied)
        }
        findViewById<Button>(R.id.copyAdminKey).setOnClickListener {
            copyCredential("admin.txt", "memory-platform-admin-key", R.string.admin_key_copied)
        }
        findViewById<Button>(R.id.battery).setOnClickListener { requestIgnoreBatteryOptimizations() }
        findViewById<Button>(R.id.exportDiagnostics).setOnClickListener { exportDiagnostics() }

        val advancedPanel = findViewById<View>(R.id.advancedPanel)
        val advancedState = findViewById<TextView>(R.id.advancedState)
        findViewById<View>(R.id.advancedToggle).setOnClickListener {
            val open = advancedPanel.visibility != View.VISIBLE
            advancedPanel.visibility = if (open) View.VISIBLE else View.GONE
            advancedState.text = getString(if (open) R.string.advanced_collapse else R.string.advanced_expand)
        }

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
        prefs().edit().putBoolean(PREF_AUTOSTART, true).apply()
        GatewayService.start(this)
    }

    private fun render(status: JSONObject?) {
        val current = status?.optString("state", "stopped") ?: "stopped"
        val running = current == "running"
        val busy = current == "starting"
        state.text = getString(
            when (current) {
                "running" -> R.string.state_running
                "starting" -> R.string.state_starting
                "failed" -> R.string.state_failed
                else -> R.string.state_stopped
            },
        )
        val lines = mutableListOf(readiness.health)
        status?.optString("error", "")?.takeIf { it.isNotEmpty() && it != "null" }?.let {
            lines += getString(R.string.error_prefix, it)
        }
        detail.text = lines.joinToString("\n")

        val batteryOk = isIgnoringBatteryOptimizations()
        batteryCard.visibility = if (batteryOk) View.GONE else View.VISIBLE

        setStep(stepService, done = running, checking = busy)
        setStep(stepModel, done = running && readiness.modelReady == true, checking = running && readiness.modelReady == null)
        setStep(stepChatKey, done = running && readiness.chatKeyExists == true, checking = running && readiness.chatKeyExists == null)
        setStep(stepBattery, done = batteryOk, checking = false)

        val allDone = running && readiness.modelReady == true && readiness.chatKeyExists == true && batteryOk
        checklistTitle.text = getString(if (allDone) R.string.checklist_all_done else R.string.checklist_title)

        primaryAction.isEnabled = !busy && !openingConsole
        primaryAction.text = getString(
            when {
                !running -> R.string.action_start
                readiness.modelReady == false -> R.string.action_open_console_model
                readiness.modelReady == true && readiness.chatKeyExists == false -> R.string.action_open_console_key
                else -> R.string.action_open_console
            },
        )
        primaryHint.visibility = if (running) View.VISIBLE else View.GONE
        stopAction.visibility = if (running || busy) View.VISIBLE else View.GONE
    }

    private fun setStep(view: TextView, done: Boolean, checking: Boolean) {
        view.setCompoundDrawablesRelativeWithIntrinsicBounds(
            if (done) R.drawable.ic_step_done else R.drawable.ic_step_todo, 0, 0, 0,
        )
        view.alpha = if (done || !checking) 1f else 0.7f
    }

    /**
     * One background round trip per poll: liveness, readiness (503 until a model
     * channel is configured) and whether any chat key exists (needs the login
     * key from gateway.txt; unknown when that file is missing or revoked).
     */
    private fun probe(): Readiness {
        val base = "http://127.0.0.1:${StackState.MEMORY_PORT}"
        val healthCode = httpStatus("$base/health", null)
        if (healthCode != 200) {
            val text = if (healthCode == null) getString(R.string.health_unreachable) else getString(R.string.health_http, healthCode)
            return Readiness(health = text, healthOk = false, modelReady = null, chatKeyExists = null)
        }
        val ready = when (httpStatus("$base/readyz", null)) {
            200 -> true
            503 -> false
            else -> null
        }
        val loginKey = File(filesDir, "memory-platform/credentials/gateway.txt")
            .takeIf { it.isFile }?.readText()?.trim().orEmpty()
        val chatKey = if (loginKey.isEmpty()) null else try {
            val connection = URL("$base/auth/tokens").openConnection() as HttpURLConnection
            connection.connectTimeout = 1_500
            connection.readTimeout = 2_500
            connection.setRequestProperty("Authorization", "Bearer $loginKey")
            try {
                if (connection.responseCode != 200) null else {
                    val body = connection.inputStream.bufferedReader().readText()
                    val data = JSONObject(body).optJSONArray("data")
                    var found = false
                    for (index in 0 until (data?.length() ?: 0)) {
                        val record = data!!.getJSONObject(index)
                        if (record.optString("role") == "chat" && record.isNull("revoked_at")) { found = true; break }
                    }
                    found
                }
            } finally {
                connection.disconnect()
            }
        } catch (e: Exception) {
            null
        }
        return Readiness(health = getString(R.string.health_ok), healthOk = true, modelReady = ready, chatKeyExists = chatKey)
    }

    private fun httpStatus(url: String, bearer: String?): Int? {
        return try {
            val connection = URL(url).openConnection() as HttpURLConnection
            connection.connectTimeout = 1_500
            connection.readTimeout = 2_500
            if (bearer != null) connection.setRequestProperty("Authorization", "Bearer $bearer")
            val code = connection.responseCode
            connection.disconnect()
            code
        } catch (e: Exception) {
            null
        }
    }

    /**
     * Ask the embedded runtime for a one-time login link (login key + admin key
     * delivered to this device's browser only) and open it. Falls back to the
     * plain console URL if the link cannot be minted.
     */
    private fun openConsole() {
        if (openingConsole) return
        openingConsole = true
        render(StackState.status.value)
        thread(name = "console-login-link") {
            var url = StackState.CONSOLE_URL
            var failure: String? = null
            try {
                val json = Python.getInstance().getModule("embedded_stack").callAttr("console_login_link").toString()
                val payload = JSONObject(json)
                val minted = payload.optString("url", "")
                if (minted.isNotEmpty()) url = minted else failure = payload.optString("error", "unknown")
            } catch (e: Exception) {
                failure = e.message ?: e.toString()
            }
            handler.post {
                openingConsole = false
                render(StackState.status.value)
                failure?.let { Toast.makeText(this, getString(R.string.login_link_failed, it), Toast.LENGTH_LONG).show() }
                startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
            }
        }
    }

    private fun copyPlain(value: String) {
        (getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager)
            .setPrimaryClip(ClipData.newPlainText("memory-platform-url", value))
        Toast.makeText(this, R.string.copied, Toast.LENGTH_SHORT).show()
    }

    /** Credentials live as 0600 files under filesDir; the clipboard is the manual hand-off to the user. */
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

    private fun isIgnoringBatteryOptimizations(): Boolean =
        (getSystemService(Context.POWER_SERVICE) as PowerManager).isIgnoringBatteryOptimizations(packageName)

    private fun requestIgnoreBatteryOptimizations() {
        if (isIgnoringBatteryOptimizations()) {
            Toast.makeText(this, R.string.battery_already_ignored, Toast.LENGTH_SHORT).show()
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
